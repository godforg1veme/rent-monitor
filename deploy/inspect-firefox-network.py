"""Read proxy egress and cookie metadata; perform at most one configured Avito navigation."""

import argparse
import asyncio
import ipaddress
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from rent_monitor.browser.firefox import FirefoxBrowserTransport, ProxyCredentials
from rent_monitor.config import load_config
from rent_monitor.parsers.avito import parse_search_page


def cookie_metadata(profile: Path):
    database = profile.resolve() / "cookies.sqlite"
    if not database.is_file():
        return {"schema_version": None, "avito_cookie_count": 0}
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        return {
            "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "avito_cookie_count": connection.execute(
                "SELECT count(*) FROM moz_cookies WHERE host='avito.ru' OR host LIKE '%.avito.ru'"
            ).fetchone()[0],
        }
    finally:
        connection.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--proxy-file", type=Path)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--avito", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config/search.toml"))
    args = parser.parse_args()
    print("cookies_before:", json.dumps(cookie_metadata(args.profile)), flush=True)
    if args.metadata_only:
        return
    transport = FirefoxBrowserTransport(
        args.profile,
        proxy=ProxyCredentials.load(args.proxy_file) if args.proxy_file else None,
        binary_path="/usr/bin/firefox",
        driver_path="/opt/rent-monitor/geckodriver",
    )
    try:
        await transport.start()
        original_observer = transport._observe_response

        def observe(event):
            original_observer(event)
            if event.get("context") == transport._context_id and event.get("navigation"):
                request = event.get("request", {})
                parts = urlsplit(request.get("url", ""))
                print(
                    "navigation_response:",
                    json.dumps(
                        {
                            "status": event.get("response", {}).get("status"),
                            "host": parts.hostname,
                            "path": parts.path[:100],
                            "destination": request.get("destination"),
                            "mime": event.get("response", {}).get("mimeType"),
                        }
                    ),
                    flush=True,
                )

        await transport._run(
            lambda: transport._driver.network.add_event_handler("response_started", observe)
        )
        await transport._run(lambda: transport._driver.get("https://api.ipify.org"))
        visible = await transport._run(
            lambda: transport._driver.find_element("tag name", "body").text.strip()
        )
        print("browser_egress:", str(ipaddress.ip_address(visible)), flush=True)
        browser = await transport._run(
            lambda: transport._driver.execute_script(
                "return {ua:navigator.userAgent, languages:navigator.languages, "
                "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}"
            )
        )
        print("browser:", json.dumps(browser), flush=True)
        if args.avito:
            config = load_config(args.config)
            source = next(source for source in config.sources if source.name == "avito")
            page = await transport.fetch(source.searches[0].url)
            for delay in (0, 10, 20):
                if delay:
                    await asyncio.sleep(delay)
                page = await transport.current_page()
                sample = parse_search_page(page.html, page.final_url, observed_at=page.observed_at)
                print(
                    "settling_sample:",
                    json.dumps(
                        {
                            "status": page.status_code,
                            "recognized": sample.recognized,
                            "blocked": sample.blocked_reason,
                            "cards": len(sample.candidates),
                        }
                    ),
                    flush=True,
                )
                if sample.recognized or sample.blocked_reason:
                    break
            parsed = parse_search_page(page.html, page.final_url, observed_at=page.observed_at)
            print(
                "avito:",
                json.dumps(
                    {
                        "http_status": page.status_code,
                        "final_url": page.final_url,
                        "blocked": parsed.blocked_reason,
                        "recognized": parsed.recognized,
                        "cards": len(parsed.candidates),
                    }
                ),
                flush=True,
            )
            if parsed.blocked_reason:
                title = await transport._run(lambda: transport._driver.title)
                print("restriction_title:", title, flush=True)
                # Only restriction UI, never listing descriptions or contact data.
                visible = await transport._run(
                    lambda: transport._driver.find_element("tag name", "body").text[:800]
                )
                print("restriction_ui:", visible, flush=True)
    finally:
        await transport.aclose()
    print("cookies_after:", json.dumps(cookie_metadata(args.profile)), flush=True)


asyncio.run(main())
