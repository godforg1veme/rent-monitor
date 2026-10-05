"""Opt-in browser backend selection; credentials never enter ordinary config."""

from __future__ import annotations

import os
from pathlib import Path

from rent_monitor.browser.transport import BrowserTransport, PlaywrightBrowserTransport


def create_browser_transport(state_directory: Path, max_response_bytes: int) -> BrowserTransport:
    backend = os.environ.get("RENT_MONITOR_BROWSER_BACKEND", "chromium")
    if backend == "home-pow":
        from rent_monitor.browser.home_pow import HomePowTransport

        return HomePowTransport(
            state_directory / "avito-home-pow",
            max_response_bytes,
            python_path=os.environ["RENT_MONITOR_POW_PYTHON"],
            worker_path=os.environ["RENT_MONITOR_POW_WORKER"],
            proxy="http://127.0.0.1:18782",
        )
    if backend == "chromium":
        from rent_monitor.browser.firefox import ProxyCredentials

        credential_file = os.environ.get("RENT_MONITOR_PROXY_FILE")
        proxy = ProxyCredentials.load(Path(credential_file)) if credential_file else None
        return PlaywrightBrowserTransport(
            state_directory / "avito-browser-profile",
            headless=False,
            proxy=proxy,
            block_media=os.environ.get("RENT_MONITOR_BLOCK_MEDIA", "0") == "1",
        )
    if backend != "firefox":
        raise ValueError("Unsupported browser backend")
    from rent_monitor.browser.firefox import FirefoxBrowserTransport, ProxyCredentials

    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    credential_file = os.environ.get("RENT_MONITOR_PROXY_FILE")
    if credential_file:
        proxy = ProxyCredentials.load(Path(credential_file))
    elif credential_directory and (Path(credential_directory) / "browser_proxy").is_file():
        proxy = ProxyCredentials.load(Path(credential_directory) / "browser_proxy")
    else:
        proxy = None
    return FirefoxBrowserTransport(
        state_directory / "avito-firefox-profile",
        proxy=proxy,
        binary_path=os.environ.get("RENT_MONITOR_FIREFOX_BINARY"),
        driver_path=os.environ.get("RENT_MONITOR_GECKODRIVER"),
        max_response_bytes=max_response_bytes,
    )
