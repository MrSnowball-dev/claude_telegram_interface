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
