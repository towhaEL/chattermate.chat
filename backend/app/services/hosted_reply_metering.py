"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Open-core bridge for hosted AI-reply metering.

The private enterprise module owns plan data, wallet balances, reservations,
and payment integrations. This module deliberately owns none of those. It
provides a stable lifecycle around one model-backed reply:

    authorize (before inference) -> commit (after a reply is stored)
                              or release (on failure)

Community/self-hosted installations have no enterprise hook, so this is a
no-op. Older enterprise installations keep their legacy message-limit check
until they implement the reservation hook.
"""

import inspect
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.enterprise_hooks import EnterpriseHookBroken, enterprise_hook
from app.core.logger import get_logger

logger = get_logger(__name__)

USAGE_SERVICE_MODULE = "app.enterprise.services.usage_service"
LEGACY_LIMIT_MODULE = "app.enterprise.services.message_limit"

DEFAULT_LIMIT_MESSAGE = (
    "Sorry, this assistant has reached its AI reply limit. "
    "Please contact the store owner."
)
USAGE_UNAVAILABLE_MESSAGE = (
    "Sorry, the AI reply service is temporarily unavailable. "
    "Please try again shortly."
)


@dataclass(frozen=True)
class ReplyUsageAuthorization:
    """The public result of reserving one hosted AI reply.

    ``enforced`` is false only when no reservation-aware enterprise module is
    installed. Callers can therefore leave OSS/self-hosted deployments
    unrestricted without knowing anything about private billing internals.
    """

    allowed: bool
    generation_id: str
    enforced: bool
    message: str = DEFAULT_LIMIT_MESSAGE
    reason: Optional[str] = None


def _field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


async def _call(hook, **kwargs):
    result = hook(**kwargs)
    return await result if inspect.isawaitable(result) else result


def reservation_metering_enabled() -> bool:
    """Whether a reservation-aware enterprise implementation is installed.

    A broken installed hook counts as enabled. The subsequent authorize call
    fails closed instead of silently falling back to the old counter and
    potentially allowing uncharged hosted usage.
    """
    try:
        return enterprise_hook(USAGE_SERVICE_MODULE, "authorize_ai_reply") is not None
    except EnterpriseHookBroken:
        return True


async def authorize_hosted_reply(
    db: Session,
    organization_id: str,
    generation_id: str,
    *,
    model_name: Optional[str],
    surface: str,
) -> ReplyUsageAuthorization:
    """Reserve one hosted reply before inference.

    Enterprise contract::

        async authorize_ai_reply(
            db, organization_id, generation_id, *, model_name, surface
        ) -> {"allowed": bool, "message": str | None, "reason": str | None}

    The private implementation must make ``generation_id`` idempotent and
    reserve quota or wallet balance atomically. It must not charge here.
    """
    try:
        hook = enterprise_hook(USAGE_SERVICE_MODULE, "authorize_ai_reply")
        # A reservation is unsafe unless it can later be committed or
        # released. Verify the whole lifecycle before allowing inference.
        if hook is not None:
            enterprise_hook(USAGE_SERVICE_MODULE, "commit_ai_reply")
            enterprise_hook(USAGE_SERVICE_MODULE, "release_ai_reply")
    except EnterpriseHookBroken as exc:
        logger.exception("Hosted-reply metering hook is unavailable")
        return ReplyUsageAuthorization(
            allowed=False,
            generation_id=generation_id,
            enforced=True,
            message=USAGE_UNAVAILABLE_MESSAGE,
            reason=f"metering_hook_broken:{exc}",
        )

    if hook is None:
        return ReplyUsageAuthorization(
            allowed=True,
            generation_id=generation_id,
            enforced=False,
        )

    try:
        result = await _call(
            hook,
            db=db,
            organization_id=organization_id,
            generation_id=generation_id,
            model_name=model_name,
            surface=surface,
        )
    except Exception as exc:
        logger.exception("Hosted-reply authorization failed")
        return ReplyUsageAuthorization(
            allowed=False,
            generation_id=generation_id,
            enforced=True,
            message=USAGE_UNAVAILABLE_MESSAGE,
            reason=f"metering_authorization_failed:{type(exc).__name__}",
        )

    return ReplyUsageAuthorization(
        allowed=bool(_field(result, "allowed", False)),
        generation_id=generation_id,
        enforced=True,
        message=_field(result, "message", DEFAULT_LIMIT_MESSAGE) or DEFAULT_LIMIT_MESSAGE,
        reason=_field(result, "reason"),
    )


async def commit_hosted_reply(
    db: Session, generation_id: str, reply_id: Any
) -> None:
    """Commit a reservation only after the AI reply row is persisted.

    Repeated calls must be harmless; the private service is responsible for
    making this operation exactly-once for ``generation_id``.
    """
    try:
        hook = enterprise_hook(USAGE_SERVICE_MODULE, "commit_ai_reply")
    except EnterpriseHookBroken:
        logger.exception("Hosted-reply commit hook is unavailable")
        return
    if hook is None:
        return
    try:
        await _call(hook, db=db, generation_id=generation_id, reply_id=reply_id)
    except Exception:
        # The reply is already persisted. Do not surface an error that could
        # make a client retry and create another reply; reconciliation handles
        # a reservation that remains pending.
        logger.exception("Hosted-reply commit failed for generation %s", generation_id)


async def release_hosted_reply(
    db: Session, generation_id: str, *, reason: str
) -> None:
    """Release a reservation after model execution or persistence fails."""
    try:
        hook = enterprise_hook(USAGE_SERVICE_MODULE, "release_ai_reply")
    except EnterpriseHookBroken:
        logger.exception("Hosted-reply release hook is unavailable")
        return
    if hook is None:
        return
    try:
        await _call(hook, db=db, generation_id=generation_id, reason=reason)
    except Exception:
        logger.exception("Hosted-reply release failed for generation %s", generation_id)


async def legacy_message_limit_allows(
    db: Session, organization_id: str, socket_id: Optional[str] = None, sio=None
) -> bool:
    """Run the old counter only when reservation metering is not installed.

    Its permissive-on-error behavior preserves current hosted deployments;
    the reservation-aware implementation instead fails closed in
    ``authorize_hosted_reply`` because it controls money and quotas.
    """
    if reservation_metering_enabled():
        return True
    try:
        hook = enterprise_hook(LEGACY_LIMIT_MODULE, "check_message_limit")
    except EnterpriseHookBroken:
        logger.exception("Legacy message-limit hook is unavailable; allowing reply")
        return True
    if hook is None:
        return True
    try:
        return bool(await _call(
            hook,
            db=db,
            organization_id=organization_id,
            socket_id=socket_id,
            sio=sio,
        ))
    except TypeError:
        # Existing private versions used positional parameters. Keep the
        # adapter compatible while the reservation API is rolled out.
        try:
            result = hook(db, organization_id, socket_id, sio)
            return bool(await result if inspect.isawaitable(result) else result)
        except Exception:
            logger.exception("Legacy message-limit check failed; allowing reply")
            return True
    except Exception:
        logger.exception("Legacy message-limit check failed; allowing reply")
        return True
