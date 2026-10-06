#!/usr/bin/env python3
"""v2 spike: open a Ring live view over WHEP, save one video frame as JPEG, close the session.

Ring rules (Partner API docs): raw SDP offer with Content-Type application/sdp, video required,
recvonly only, all ICE candidates gathered before posting. Ring answers 201 + SDP + Location.

Run: RING_TOKEN=... .venv/bin/python grab_frame.py [out.jpg]
"""
import asyncio, os, sys
from urllib.parse import urljoin

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

DEFAULT_ICE = [RTCIceServer("stun:stun.l.google.com:19302")]


async def grab_frame(whep_url, token, out_path, ice_servers=DEFAULT_ICE, skip_frames=10, timeout=25):
    """Return the saved JPEG path. Skips the first frames, which are often grey before a keyframe."""
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    pc.addTransceiver("video", direction="recvonly")
    got_frame = asyncio.get_running_loop().create_future()

    @pc.on("track")
    def on_track(track):
        async def read():
            try:
                for _ in range(skip_frames):
                    await track.recv()
                frame = await track.recv()
                frame.to_image().save(out_path, "JPEG", quality=90)
                got_frame.set_result(out_path)
            except Exception as e:
                if not got_frame.done():
                    got_frame.set_exception(e)
        asyncio.ensure_future(read())

    # aiortc gathers every ICE candidate inside setLocalDescription, which is what Ring requires.
    await pc.setLocalDescription(await pc.createOffer())
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"}
    session_url = None
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(whep_url, data=pc.localDescription.sdp, headers=headers) as r:
                answer = await r.text()
                if r.status != 201:
                    raise RuntimeError(f"WHEP POST {r.status}: {answer[:300]}")
                if r.headers.get("Location"):
                    session_url = urljoin(whep_url, r.headers["Location"])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            return await asyncio.wait_for(got_frame, timeout)
        finally:
            await pc.close()
            if session_url:  # free the live-view session on Ring's side
                async with http.delete(session_url, headers={"Authorization": f"Bearer {token}"}) as r:
                    print(f"closed session: {r.status}")


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ring_client import RingClient

    token = os.environ.get("RING_TOKEN") or sys.exit("Set RING_TOKEN")
    base = os.environ.get("RING_BASE_URL", "https://api.amazonvision.com").rstrip("/")
    device_id = os.environ.get("RING_DEVICE_ID") or RingClient(token, base).devices()[0]["id"]
    out = sys.argv[1] if len(sys.argv) > 1 else "frame.jpg"
    path = asyncio.run(grab_frame(f"{base}/v1/devices/{device_id}/media/streaming/whep/sessions", token, out))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
