"""Single installed-Windows-Firefox smoke check using the prepared isolated profile."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from rent_monitor.browser.firefox import FirefoxBrowserTransport, ProxyCredentials
from rent_monitor.config import load_config
from rent_monitor.core.filters import matches_listing
from rent_monitor.parsers.avito import parse_search_page

root = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
profile = root / "data/firefox-avito-copy"
if not (profile / "cookies.sqlite").is_file():
    raise RuntimeError("Prepare an isolated Avito profile first; original Firefox is not modified")
configuration = subprocess.run(
    ["ssh", "jarvis-vps", "sudo cat /etc/personal-proxy/3proxy.cfg"],
    capture_output=True,
    text=True,
    check=True,
).stdout
users = [
    entry
    for line in configuration.splitlines()
    if line.startswith("users ")
    for entry in line.split()[1:]
]
if len(users) != 1:
    raise RuntimeError("Ambiguous proxy configuration")
username, kind, password = users[0].split(":", 2)
if kind != "CL":
    raise RuntimeError("Unsupported proxy credential format")
proxy = ProxyCredentials("87.120.187.202", 1086, username, password)
del configuration, users, username, password


async def main():
    config = load_config(root / "config/search.toml")
    source = next(source for source in config.sources if source.name == "avito")
    transport = FirefoxBrowserTransport(
        profile,
        proxy=proxy,
        binary_path=r"C:\Program Files\Mozilla Firefox\firefox.exe",
    )
    print("Starting isolated Firefox transport", flush=True)
    try:
        page = await transport.fetch(source.searches[0].url)
        parsed = parse_search_page(page.html, page.final_url, observed_at=page.observed_at)
        print(
            json.dumps(
                {
                    "http_status": page.status_code,
                    "recognized": parsed.recognized,
                    "blocked": parsed.blocked_reason,
                    "cards": len(parsed.candidates),
                    "matching": sum(
                        matches_listing(item.to_listing(), config.criteria)
                        for item in parsed.candidates
                    ),
                    "context": str(parsed.context),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        address_structure = await transport._run(
            lambda: transport._driver.execute_script("""
            const e = document.querySelector('[data-marker="item-address"]');
            const visit = e => ({tag: e.tagName, marker: e.getAttribute('data-marker'),
                itemprop: e.getAttribute('itemprop'), class: e.className,
                text: e.children.length ? null : e.textContent.trim(),
                children: [...e.children].slice(0, 8).map(visit)});
            return e ? visit(e) : null;
        """)
        )
        print("address structure:", json.dumps(address_structure, ensure_ascii=False), flush=True)
    finally:
        await transport.aclose()


asyncio.run(main())
