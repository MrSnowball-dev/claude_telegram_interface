"""Telegram-message text utilities: chunking long replies, formatting tool-call lines."""

from __future__ import annotations

TG_MESSAGE_LIMIT = 4096
DRAFT_TAIL_LIMIT = 3900


def chunk_message(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Split a long message at paragraph or line boundaries; never breaks mid-word
    unless a single line is itself longer than the limit."""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 4:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def draft_view(text: str) -> str:
    """Truncate to draft tail-mode if needed (Telegram caps draft text at 4096)."""
    if len(text) <= DRAFT_TAIL_LIMIT:
        return text
    return "…" + text[-DRAFT_TAIL_LIMIT:]


def tool_summary(tool: str, partial_input_json: str, max_len: int = 120) -> str:
    """Cheap one-line summary for a tool call from partial input JSON."""
    s = partial_input_json.strip().replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return f"{tool}: {s}" if s else tool
