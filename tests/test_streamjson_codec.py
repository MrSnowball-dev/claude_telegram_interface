"""Replay a captured stream-json sample through parse_event and assert event sequence."""

import json

from bot.claude_session import (
    SemanticEvent,
    TextDelta,
    TurnEnded,
    TurnStarted,
    parse_event,
)


SESSION_ID = "11111111-2222-3333-4444-555555555555"
RAW_LINES = [
    {"type": "system", "subtype": "init", "session_id": SESSION_ID, "model": "x"},
    {"type": "system", "subtype": "status", "status": "requesting"},
    {"type": "rate_limit_event", "rate_limit_info": {}},
    {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m"}}},
    {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }},
    {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "hello "},
    }},
    {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "world"},
    }},
    {"type": "assistant", "message": {"role": "assistant", "content": []}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
    {"type": "stream_event", "event": {"type": "message_stop"}},
    {
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": SESSION_ID, "result": "hello world",
    },
]


def _replay(records: list[dict]) -> list[SemanticEvent]:
    state: dict = {}
    out: list[SemanticEvent] = []
    for r in records:
        evt = parse_event(r, state)
        if evt is not None:
            out.append(evt)
    return out


def test_codec_full_text_turn() -> None:
    events = _replay(RAW_LINES)
    assert isinstance(events[0], TurnStarted) and events[0].session_id == SESSION_ID
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(d.text for d in deltas) == "hello world"
    assert isinstance(events[-1], TurnEnded)
    assert not events[-1].is_error
    assert events[-1].final_text == "hello world"


def test_codec_handles_error_result() -> None:
    state: dict = {}
    evt = parse_event(
        {"type": "result", "is_error": True, "api_error_status": "rate_limit"},
        state,
    )
    assert isinstance(evt, TurnEnded) and evt.is_error and evt.error == "rate_limit"


def test_codec_unknown_event_returns_none() -> None:
    state: dict = {}
    assert parse_event({"type": "weird"}, state) is None
    assert parse_event({"type": "stream_event", "event": {"type": "unknown"}}, state) is None


def test_codec_ignores_empty_text_delta() -> None:
    state: dict = {}
    out = parse_event(
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": ""},
        }},
        state,
    )
    assert out is None


def test_codec_json_roundtrip() -> None:
    """Lines should also parse from raw JSON strings, not just dicts."""
    state: dict = {}
    line = json.dumps(RAW_LINES[0])
    evt = parse_event(json.loads(line), state)
    assert isinstance(evt, TurnStarted)
