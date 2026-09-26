"""Single-session token files for private noVNC CAPTCHA access."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode


@dataclass(frozen=True, slots=True)
class CaptchaSession:
    token: str
    source: str
    url: str
    expires_at: datetime


class CaptchaSessionManager:
    def __init__(
        self,
        token_directory: Path,
        public_base_url: str,
        *,
        target: str = "127.0.0.1:5900",
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not public_base_url.startswith("https://"):
            raise ValueError("public_base_url must use HTTPS")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.token_directory = token_directory
        self.public_base_url = public_base_url.rstrip("/")
        self.target = target
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active: CaptchaSession | None = None
        self._initialized = False

    async def issue(self, source: str) -> CaptchaSession:
        async with self._lock:
            await self._initialize_unlocked()
            await self._remove_expired_unlocked()
            if self._active is not None:
                return self._active
            token = secrets.token_urlsafe(32)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                raise RuntimeError("generated CAPTCHA token is not URL-safe")
            expires_at = self._clock().astimezone(UTC) + timedelta(seconds=self.ttl_seconds)
            query = urlencode(
                {
                    "autoconnect": "true",
                    "resize": "scale",
                    "reconnect": "false",
                    "show_dot": "true",
                    "token": token,
                }
            )
            session = CaptchaSession(
                token=token,
                source=source,
                url=f"{self.public_base_url}/vnc_lite.html?{query}",
                expires_at=expires_at,
            )
            self._write_token_file(token)
            self._active = session
            return session

    async def active_session(self) -> CaptchaSession | None:
        async with self._lock:
            await self._initialize_unlocked()
            await self._remove_expired_unlocked()
            return self._active

    async def remove_expired(self) -> None:
        async with self._lock:
            await self._initialize_unlocked()
            await self._remove_expired_unlocked()

    async def expire(self, token: str) -> None:
        async with self._lock:
            await self._initialize_unlocked()
            if self._active is None or not secrets.compare_digest(self._active.token, token):
                return
            self._unlink_token(token)
            self._active = None

    async def expire_all(self) -> None:
        async with self._lock:
            self._ensure_directory()
            for path in self.token_directory.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            self._active = None
            self._initialized = True

    async def _initialize_unlocked(self) -> None:
        if self._initialized:
            return
        self._ensure_directory()
        for path in self.token_directory.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
        self._initialized = True

    async def _remove_expired_unlocked(self) -> None:
        if self._active is not None and self._active.expires_at <= self._clock().astimezone(UTC):
            self._unlink_token(self._active.token)
            self._active = None

    def _ensure_directory(self) -> None:
        self.token_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.token_directory, 0o700)

    def _write_token_file(self, token: str) -> None:
        temporary = self.token_directory / f".{token}.tmp"
        destination = self.token_directory / token
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, f"{self.target}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)

    def _unlink_token(self, token: str) -> None:
        (self.token_directory / token).unlink(missing_ok=True)
