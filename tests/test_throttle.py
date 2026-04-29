import asyncio
import time

import pytest

from bot.streaming import DraftStreamer


@pytest.mark.asyncio
async def test_streamer_throttles_and_finalizes() -> None:
    drafts: list[tuple[float, str]] = []
    commits: list[str] = []

    async def send_draft(text: str) -> None:
        drafts.append((time.monotonic(), text))

    async def commit(text: str) -> None:
        commits.append(text)

    streamer = DraftStreamer(send_draft, commit, min_interval=0.05)
    started = time.monotonic()

    # 200 deltas as fast as possible.
    for i in range(200):
        streamer.append(f"chunk{i}-")
        await asyncio.sleep(0.005)

    final = "".join(f"chunk{i}-" for i in range(200))
    await streamer.finalize(final)

    elapsed = time.monotonic() - started
    # Average 50/s deltas → with 50ms throttle, we expect on the order of 20 drafts/sec at most.
    rate = len(drafts) / max(elapsed, 1e-3)
    assert rate <= 25, f"draft rate {rate} too high"

    # Drafts must grow monotonically.
    for prev, curr in zip(drafts, drafts[1:], strict=False):
        assert len(curr[1]) >= len(prev[1])

    assert commits == [final]


@pytest.mark.asyncio
async def test_streamer_skips_after_finalize() -> None:
    drafts: list[str] = []
    commits: list[str] = []

    async def send_draft(text: str) -> None:
        drafts.append(text)

    async def commit(text: str) -> None:
        commits.append(text)

    streamer = DraftStreamer(send_draft, commit, min_interval=0.01)
    streamer.append("hi")
    await asyncio.sleep(0.05)
    await streamer.finalize("hi")
    streamer.append("ignored")
    await asyncio.sleep(0.05)

    assert commits == ["hi"]
    assert all("ignored" not in d for d in drafts)


@pytest.mark.asyncio
async def test_streamer_heartbeats_during_silence() -> None:
    """During a long quiet period, the heartbeat should re-send the draft text
    (with the same content) so Telegram doesn't retire the animation."""
    drafts: list[str] = []
    commits: list[str] = []

    async def send_draft(text: str) -> None:
        drafts.append(text)

    async def commit(text: str) -> None:
        commits.append(text)

    streamer = DraftStreamer(
        send_draft, commit, min_interval=0.01, heartbeat_interval=0.05,
    )
    # Stream a small amount of content, then go silent.
    streamer.append("hello world from claude")
    await asyncio.sleep(0.02)
    initial_count = len(drafts)
    assert initial_count >= 1

    # Wait long enough for several heartbeat ticks but no real updates.
    await asyncio.sleep(0.25)

    # Heartbeats fired with the same text.
    after_silence = len(drafts)
    assert after_silence > initial_count
    assert all(d == drafts[0] for d in drafts), "heartbeat should re-send same text"

    await streamer.finalize("hello world from claude")
