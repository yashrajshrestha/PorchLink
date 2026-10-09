#!/usr/bin/env python3
"""A pretend Ring WHEP camera for testing Watch live without a Ring device (needs .venv: aiortc, aiohttp, Pillow).

It answers like Ring (201 + SDP answer + Location; DELETE closes the session) and streams the simulator's porch
pictures as live video. Every frame is stamped "SIMULATED CAMERA" so nobody mistakes it for Ring.

Run: .venv/bin/python sim/fake_whep.py [--port 9100]
Then start PorchLink (or dashboard_preview.py --whep) with RING_WHEP_BASE=http://127.0.0.1:9100
"""
import argparse, asyncio, glob, os, secrets, time

import av
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from PIL import Image, ImageDraw, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = sorted(glob.glob(os.path.join(HERE, "images", "*.jpg")))
SESSIONS = web.AppKey("sessions", dict)  # the open sessions: fake[SESSIONS]
_PICTURES = []  # the porch pictures cropped to the video's shape, built on first use and shared by every session


def pictures():
    if not _PICTURES:
        _PICTURES.extend(ImageOps.fit(Image.open(p).convert("RGB"), (640, 360)) for p in IMAGES)  # crop, never squash
    return _PICTURES


class PorchTrack(VideoStreamTrack):
    """The sample pictures in a loop, a new one every hold_s seconds, stamped with the camera and the time."""

    def __init__(self, device_id, hold_s=3):
        super().__init__()
        self.device_id, self.hold_s, self.started = device_id, hold_s, time.time()
        self.pictures = pictures()

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        img = self.pictures[int((time.time() - self.started) / self.hold_s) % len(self.pictures)].copy()
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 640, 26), fill=(0, 0, 0))
        draw.text((8, 7), f"SIMULATED CAMERA - {self.device_id} - {time.strftime('%H:%M:%S')}", fill=(255, 194, 77))
        frame = av.VideoFrame.from_image(img)
        frame.pts, frame.time_base = pts, time_base
        return frame


def make_app(hold_s=3):
    sessions = {}

    async def create(request):
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return web.Response(status=401, text="missing bearer token")
        if request.content_type != "application/sdp":
            return web.Response(status=415, text="expected application/sdp")
        pc = RTCPeerConnection()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=await request.text(), type="offer"))
        pc.addTrack(PorchTrack(request.match_info["device_id"], hold_s))
        await pc.setLocalDescription(await pc.createAnswer())  # aiortc gathers all ICE candidates here
        sid = secrets.token_hex(8)
        sessions[sid] = pc
        return web.Response(status=201, text=pc.localDescription.sdp, content_type="application/sdp",
                            headers={"Location": f"/whep/sessions/{sid}"})

    async def delete(request):
        pc = sessions.pop(request.match_info["sid"], None)
        if pc is None:
            return web.Response(status=404)
        await pc.close()
        return web.Response(status=204)

    async def close_all(app):
        await asyncio.gather(*(pc.close() for pc in sessions.values()))
        sessions.clear()

    app = web.Application()
    app.router.add_post("/v1/devices/{device_id}/media/streaming/whep/sessions", create)
    app.router.add_delete("/whep/sessions/{sid}", delete)
    app.on_shutdown.append(close_all)
    app[SESSIONS] = sessions
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--hold", type=float, default=3, help="seconds per picture")
    a = ap.parse_args()
    print(f"Fake Ring camera (WHEP): http://127.0.0.1:{a.port}  — set RING_WHEP_BASE to this")
    web.run_app(make_app(a.hold), host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
