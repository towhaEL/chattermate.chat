"""Contract tests for the open-core hosted-reply metering bridge."""

import pytest

from app.core.enterprise_hooks import EnterpriseHookBroken
from app.services import hosted_reply_metering as metering


@pytest.mark.asyncio
async def test_community_installation_allows_without_metering(monkeypatch):
    monkeypatch.setattr(metering, "enterprise_hook", lambda *_: None)

    result = await metering.authorize_hosted_reply(
        db=object(),
        organization_id="org-1",
        generation_id="chat_history:1",
        model_name="gpt-4o-mini",
        surface="widget",
    )

    assert result.allowed is True
    assert result.enforced is False


@pytest.mark.asyncio
async def test_broken_reservation_hook_fails_closed(monkeypatch):
    def broken(*_):
        raise EnterpriseHookBroken("missing provider")

    monkeypatch.setattr(metering, "enterprise_hook", broken)

    result = await metering.authorize_hosted_reply(
        db=object(),
        organization_id="org-1",
        generation_id="chat_history:1",
        model_name="gpt-4o-mini",
        surface="widget",
    )

    assert result.allowed is False
    assert result.enforced is True
    assert result.reason.startswith("metering_hook_broken:")


@pytest.mark.asyncio
async def test_authorization_preserves_enterprise_denial(monkeypatch):
    calls = []

    async def authorize(**kwargs):
        calls.append(kwargs)
        return {
            "allowed": False,
            "message": "Add balance to continue.",
            "reason": "wallet_empty",
        }

    async def no_op(**_kwargs):
        return None

    monkeypatch.setattr(
        metering,
        "enterprise_hook",
        lambda _module, name: {
            "authorize_ai_reply": authorize,
            "commit_ai_reply": no_op,
            "release_ai_reply": no_op,
        }.get(name),
    )

    result = await metering.authorize_hosted_reply(
        db="db",
        organization_id="org-1",
        generation_id="channel:whatsapp:wamid-1",
        model_name="hosted-mini",
        surface="whatsapp",
    )

    assert result.allowed is False
    assert result.enforced is True
    assert result.message == "Add balance to continue."
    assert result.reason == "wallet_empty"
    assert calls == [{
        "db": "db",
        "organization_id": "org-1",
        "generation_id": "channel:whatsapp:wamid-1",
        "model_name": "hosted-mini",
        "surface": "whatsapp",
    }]


@pytest.mark.asyncio
async def test_incomplete_reservation_lifecycle_fails_closed(monkeypatch):
    async def authorize(**_kwargs):
        return {"allowed": True}

    def incomplete_hook(_module, name):
        if name == "authorize_ai_reply":
            return authorize
        raise EnterpriseHookBroken("missing lifecycle hook")

    monkeypatch.setattr(
        metering,
        "enterprise_hook",
        incomplete_hook,
    )

    result = await metering.authorize_hosted_reply(
        db=object(),
        organization_id="org-1",
        generation_id="chat_history:1",
        model_name="hosted-mini",
        surface="widget",
    )

    assert result.allowed is False
    assert result.enforced is True


@pytest.mark.asyncio
async def test_commit_and_release_use_stable_generation_id(monkeypatch):
    calls = []

    async def commit(**kwargs):
        calls.append(("commit", kwargs))

    async def release(**kwargs):
        calls.append(("release", kwargs))

    monkeypatch.setattr(
        metering,
        "enterprise_hook",
        lambda _module, name: {
            "commit_ai_reply": commit,
            "release_ai_reply": release,
        }.get(name),
    )

    await metering.commit_hosted_reply("db", "chat_history:42", reply_id=88)
    await metering.release_hosted_reply("db", "chat_history:43", reason="model_timeout")

    assert calls == [
        ("commit", {"db": "db", "generation_id": "chat_history:42", "reply_id": 88}),
        ("release", {"db": "db", "generation_id": "chat_history:43", "reason": "model_timeout"}),
    ]


@pytest.mark.asyncio
async def test_new_metering_skips_legacy_counter(monkeypatch):
    monkeypatch.setattr(
        metering,
        "enterprise_hook",
        lambda _module, name: object() if name == "authorize_ai_reply" else None,
    )

    assert await metering.legacy_message_limit_allows(object(), "org-1") is True
