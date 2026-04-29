"""Permission-mode parsing for the ``--permission-mode`` flag of the claude CLI."""

from __future__ import annotations

# claude CLI accepts: acceptEdits, auto, bypassPermissions, default, dontAsk, plan
_ALIASES: dict[str, str] = {
    "acceptedits": "acceptEdits",
    "accept": "acceptEdits",
    "auto": "auto",
    "bypass": "bypassPermissions",
    "bypasspermissions": "bypassPermissions",
    "default": "default",
    "dontask": "dontAsk",
    "plan": "plan",
}


def normalize_perm_mode(value: str) -> str | None:
    """Map a user-typed mode to a CLI-accepted value, or return None if unknown."""
    return _ALIASES.get(value.strip().lower())
