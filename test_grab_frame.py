"""Local test for grab_frame: a fake WHEP server streams a solid-colour test video.

Run: .venv/bin/python -m unittest test_grab_frame -v
"""
import asyncio, os, tempfile, unittest

import av, numpy
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from PIL import Image

from grab_frame import grab_frame


class OrangeTrack(VideoStreamTrack):
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        img = numpy.zeros((240, 320, 3), numpy.uint8)
        img[:, :] = (255, 128, 0)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        frame.pts, frame.time_base = pts, time_base
        return frame


async def run_fake_whep(tmp):
    """Start a WHEP-shaped server; return (base_url, record_of_requests, runner)."""
    seen, pcs = {"auth": None, "ctype": None, "deleted": False}, []

    async def create(request):
        seen["auth"], seen["ctype"] = request.headers.get("Authorization"), request.headers.get("Content-Type")
        pc = RTCPeerConnection()
        pcs.append(pc)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=await request.text(), type="offer"))
        pc.addTrack(OrangeTrack())
        await pc.setLocalDescription(await pc.createAnswer())
        return web.Response(status=201, text=pc.localDescription.sdp, content_type="application/sdp",
                            headers={"Location": "/whep/sessions/abc"})

    async def delete(request):
        seen["deleted"] = True
        for pc in pcs:
            await pc.close()
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/whep/sessions", create)
    app.router.add_delete("/whep/sessions/abc", delete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", seen, runner


class GrabFrameTest(unittest.TestCase):
    def test_saves_a_frame_and_closes_session(self):
        async def scenario(tmp):
            base, seen, runner = await run_fake_whep(tmp)
            try:
                out = os.path.join(tmp, "frame.jpg")
                await grab_frame(f"{base}/whep/sessions", "test-token", out, ice_servers=[], skip_frames=3, timeout=20)
                return out, seen
            finally:
                await runner.cleanup()

        with tempfile.TemporaryDirectory() as tmp:
            out, seen = asyncio.run(scenario(tmp))
            img = Image.open(out)
            self.assertEqual(img.size, (320, 240))
            r, g, b = img.getpixel((160, 120))
            self.assertTrue(r > 200 and 90 < g < 170 and b < 60, (r, g, b))  # orange survives encoding
        self.assertEqual(seen["auth"], "Bearer test-token")
        self.assertEqual(seen["ctype"], "application/sdp")
        self.assertTrue(seen["deleted"])


if __name__ == "__main__":
    unittest.main()
