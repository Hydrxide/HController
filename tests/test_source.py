import httpx
import pytest
import respx

from hcontroller.display.hub import FrameHub, decode_frame, encode_frame
from hcontroller.display.source import FrameSource

URL = "http://app.local/render/frame"
FRAME = bytes(range(24))  # 4x2 RGB


@pytest.fixture
async def source():
    async with httpx.AsyncClient() as client:
        yield FrameSource(FrameHub(4, 2), http=lambda: client, fps=10)


def frame_route(content=FRAME, width=4, height=2, status=200):
    return respx.get(URL).respond(status, content=content, headers={"X-Width": str(width), "X-Height": str(height)})


def test_frame_rate_sets_the_interval(source):
    assert source.interval == pytest.approx(0.1)
    source.set_fps(25)
    assert source.fps == 25 and source.interval == pytest.approx(0.04)


@respx.mock
async def test_fetches_the_target_apps_frames(source):
    frame_route()
    source.set_target("app", URL)
    await source.tick()
    assert source.hub.frame == FRAME and source.hub.module == "app" and source.error is None


@respx.mock
async def test_wrong_size_is_an_error_and_goes_black(source, monkeypatch):
    monkeypatch.setattr(FrameSource, "GRACE", 0.0)
    frame_route(bytes(64 * 32 * 3), 64, 32)
    errors = []
    source.on_error = lambda: errors.append(source.error)
    source.set_target("app", URL)
    await source.tick()
    assert "64×32" in source.error and "4×2" in source.error
    assert source.hub.frame == bytes(24)
    assert errors == [source.error]


@respx.mock
async def test_brief_failures_keep_the_last_good_frame(source):
    route = frame_route()
    source.set_target("app", URL)
    await source.tick()
    route.side_effect = httpx.ConnectError("refused")
    await source.tick()
    assert source.error.startswith("no frame") and source.hub.frame == FRAME


async def test_no_target_means_black(source):
    source.set_target("app", URL)
    source.hub.publish(FRAME)
    source.set_target(None, None)
    assert source.hub.frame == bytes(24) and source.hub.module is None


@respx.mock
async def test_unchanged_frames_are_only_resent_as_keepalive(source):
    frame_route()
    source.set_target("app", URL)
    await source.tick()
    first = source.hub.published_at
    await source.tick()
    assert source.hub.published_at == first


def test_stream_messages_round_trip_and_reject_bad_sizes():
    message = encode_frame(4, 2, 55, FRAME)
    assert decode_frame(message) == (4, 2, 55, FRAME)
    with pytest.raises(ValueError):
        decode_frame(message[:-1])
    with pytest.raises(ValueError):
        decode_frame(b"\x00")


async def test_hub_fans_out_and_drops_old_frames_for_slow_viewers():
    hub = FrameHub(4, 2)
    queue = hub.subscribe()
    assert decode_frame(queue.get_nowait())[3] == bytes(24)  # current frame on subscribe
    for value in range(5):
        hub.publish(bytes([value]) * 24)
    assert queue.qsize() == 3
    while queue.qsize() > 1:
        queue.get_nowait()
    assert decode_frame(queue.get_nowait())[3] == bytes([4]) * 24
    hub.set_brightness(20)
    assert decode_frame(queue.get_nowait())[2] == 20
