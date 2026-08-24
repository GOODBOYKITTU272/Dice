"""Phase 7.4: transport-neutral Apply/Skip/Confirm/Edit messaging flow.

Business logic (the Apply/Skip/Confirm/Edit state machine) lives entirely
in attention.service, never inside a provider adapter. Telegram and
iMessage (attention.providers.*) only ever translate their own
transport's shape into/out of five domain actions -- APPLY, SKIP,
CONFIRM, EDIT, ANSWER -- defined in attention.models. Neither adapter
knows what an "application" or an "intervention" is.
"""
from __future__ import annotations
