"""End to end for Watch live: an aiortc viewer -> PorchLink's /api/live proxy -> the fake Ring WHEP camera.

Needs .venv (aiortc, aiohttp, numpy). Run: .venv/bin/python -m unittest test_live_e2e -v
Skipped under plain python3.
"""
import asyncio, json, threading, unittest, urllib.error, urllib.request
from zoneinfo import ZoneInfo

try:
    from aiohttp import web as aioweb
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from sim.fake_whep import make_app, SESSIONS
except ImportError:
    make_app = None

import web
from live import LiveViews
from store import Store


@unittest.skipIf(make_app is None, "needs .venv with aiortc")
class WatchLiveEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_viewer_gets_porch_video_through_porchlink(self):
        fake = make_app(hold_s=1)
        runner = aioweb.AppRunner(fake)
        await runner.setup()
        await aioweb.TCPSite(runner, "127.0.0.1", 0).start()
        self.addAsyncCleanup(runner.cleanup)
        whep = f"http://127.0.0.1:{runner.addresses[0][1]}"

        live = LiveViews("test-token", whep, lambda: [{"id": "front", "name": "Front Door"}])
        server = web.make_server(Store(":memory:"), ZoneInfo("UTC"), 0, live=live)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method, path, body=None):  # blocking HTTP runs in a thread: the fake camera shares this event loop
            req = urllib.request.Request(base + path, body, method=method, headers={"Content-Type": "application/sdp"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw else None

        pc = RTCPeerConnection()
        self.addAsyncCleanup(pc.close)
        pc.addTransceiver("video", direction="recvonly")
        got = asyncio.get_running_loop().create_future()

        @pc.on("track")
        def on_track(track):
            async def read():
                for _ in range(5):
                    await track.recv()
                got.set_result(await track.recv())
            asyncio.ensure_future(read())

        await pc.setLocalDescription(await pc.createOffer())
        out = await asyncio.to_thread(call, "POST", "/api/live/front", pc.localDescription.sdp.encode())
        self.assertEqual((out["camera"], out["expires_in"]), ("Front Door", 180))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=out["answer"], type="answer"))
        frame = await asyncio.wait_for(got, 20)
        self.assertEqual((frame.width, frame.height), (640, 360))
        self.assertGreater(frame.to_ndarray(format="rgb24").mean(), 20)  # a porch picture, not a black frame
        self.assertEqual(len(fake[SESSIONS]), 1)

        await asyncio.to_thread(call, "DELETE", "/api/live/session/" + out["session"])
        self.assertEqual(fake[SESSIONS], {})  # PorchLink closed the session on the camera's side

    async def test_fake_camera_wants_a_bearer_token(self):
        fake = make_app()
        runner = aioweb.AppRunner(fake)
        await runner.setup()
        await aioweb.TCPSite(runner, "127.0.0.1", 0).start()
        self.addAsyncCleanup(runner.cleanup)
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/devices/front/media/streaming/whep/sessions"

        def post():
            req = urllib.request.Request(url, b"v=0", method="POST", headers={"Content-Type": "application/sdp"})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                return e.code

        self.assertEqual(await asyncio.to_thread(post), 401)


if __name__ == "__main__":
    unittest.main()
