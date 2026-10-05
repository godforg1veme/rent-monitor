"""Verify HTTPS and token-authenticated VNC without printing access tokens."""

import argparse
import asyncio
import os
import secrets
from pathlib import Path

import aiohttp


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://100.97.66.10:10001")
    base = parser.parse_args().base.rstrip("/")
    async with aiohttp.ClientSession() as client:
        async with client.get(base + "/vnc_lite.html") as response:
            print("http_status", response.status)
        tokens = [
            p
            for p in Path("/run/rent-monitor-captcha/tokens").iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]
        print("active_token_count", len(tokens))
        probe = Path("/run/rent-monitor-captcha/tokens") / secrets.token_urlsafe(32)
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b"127.0.0.1:5900\n")
        finally:
            os.close(descriptor)
        try:
            async with client.ws_connect(
                base.replace("https:", "wss:").replace("http:", "ws:") + "/websockify",
                params={"token": probe.name},
                protocols=["binary"],
            ) as websocket:
                message = await websocket.receive(timeout=10)
                print(
                    "vnc_handshake",
                    message.type.name,
                    message.data[:12] if isinstance(message.data, bytes) else "no_vnc_banner",
                )
        finally:
            probe.unlink(missing_ok=True)


asyncio.run(main())
