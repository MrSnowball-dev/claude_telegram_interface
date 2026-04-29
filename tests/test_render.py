from bot.render import DRAFT_TAIL_LIMIT, TG_MESSAGE_LIMIT, chunk_message, draft_view, tool_summary


def test_chunk_message_short_returns_single() -> None:
    assert chunk_message("hi") == ["hi"]


def test_chunk_message_empty_returns_empty() -> None:
    assert chunk_message("") == []


def test_chunk_message_splits_on_paragraphs() -> None:
    para = "abc def ghi" * 100  # ~1100 chars
    text = "\n\n".join([para] * 5)  # forces splits at \n\n
    chunks = chunk_message(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_chunk_message_at_limit() -> None:
    text = "a" * (TG_MESSAGE_LIMIT * 2 + 50)
    chunks = chunk_message(text)
    assert all(len(c) <= TG_MESSAGE_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_draft_view_passthrough_short() -> None:
    assert draft_view("short") == "short"


def test_draft_view_truncates_with_ellipsis() -> None:
    text = "x" * (DRAFT_TAIL_LIMIT + 200)
    out = draft_view(text)
    assert out.startswith("…")
    assert len(out) == DRAFT_TAIL_LIMIT + 1


def test_tool_summary_truncates() -> None:
    s = tool_summary("Bash", '{"command": "' + "a" * 500 + '"}', max_len=80)
    assert len(s) <= 100
    assert s.startswith("Bash:")
