# SaaS implementation status

Last updated: 2026-11-02

This file tracks delivery against `docs/saas-conversion-plan.md`. The SaaS
implementation remains on `codex/saas-billing-foundation` until it is ready
for review and merge.

## Current increment: hosted reply metering contract

Status: **in progress**

Completed in the open-source core:

- Added the optional `authorize -> commit or release` bridge in
  `backend/app/services/hosted_reply_metering.py`.
- Preserved the legacy enterprise message-limit hook for existing hosted
  installations until their private module supplies reservation metering.
- Wired direct widget and external-channel replies to pass a stable generation
  ID. Provider message IDs are used for channels; widget messages send a UUID;
  the persisted inbound chat row is the safe fallback.
- Wired `ChatAgent` to authorize before model inference, commit only after the
  reply row is stored, and release a pending reservation after failure.
- Added unit tests for the bridge contract in
  `backend/tests/services/test_hosted_reply_metering.py`.

The private enterprise hook contract is:

```python
authorize_ai_reply(db, organization_id, generation_id, *, model_name, surface)
commit_ai_reply(db, generation_id, reply_id)
release_ai_reply(db, generation_id, *, reason)
```

It must atomically reserve quota/wallet value, make all three operations
idempotent by `generation_id`, and provide reservation-expiry reconciliation.

## Next increments

1. Implement the private enterprise data model, migrations, and hook contract:
   plans, overrides, subscriptions, wallet ledger, payment transactions, and
   usage reservations.
2. Apply the same lifecycle to workflow LLM nodes after their generated reply
   row can be committed with its reservation.
3. Add SSLCommerz session/IPN/reconciliation code in the private module.
4. Add operator plan management, client billing views, and provisioning.

## Current blocker

`backend/app/enterprise` is a private Git submodule but cannot be initialized
in this checkout: its configured SSH host alias (`github.com-chattermate`) is
not available, and the standard HTTPS repository path is not accessible.
Private billing and SSLCommerz code must stay there; it will not be duplicated
in the public core. Restore the repository URL/credentials to continue the
private implementation.

## Verification

- Python source compilation and `git diff --check` pass for this increment.
- The focused pytest run is pending because this checkout has no Python virtual
  environment and no `pytest` executable installed.
