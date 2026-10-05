"""Single Firefox collector smoke test; no Telegram writes or raw page logging."""

import argparse
import asyncio
import json
from pathlib import Path

from rent_monitor.browser.firefox import FirefoxBrowserTransport, ProxyCredentials
from rent_monitor.collectors.avito import AvitoCollector
from rent_monitor.config import load_config
from rent_monitor.core.filters import matches_listing
from rent_monitor.parsers.avito import parse_search_page


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/search.toml"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--proxy-file", type=Path)
    parser.add_argument("--binary")
    parser.add_argument("--driver")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--polls", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    config = load_config(args.config)
    source = next(source for source in config.sources if source.name == "avito")
    transport = FirefoxBrowserTransport(
        args.profile,
        headless=not args.headed,
        proxy=ProxyCredentials.load(args.proxy_file) if args.proxy_file else None,
        binary_path=args.binary,
        driver_path=args.driver,
    )
    try:
        collector = AvitoCollector(source.searches)
        for poll in range(args.polls):
            if poll:
                await asyncio.sleep(source.poll_interval_seconds)
            result = await collector.collect(config.criteria, transport)
            page = await transport.current_page()
            print(
                json.dumps(
                    {
                        "poll": poll + 1,
                        "status": result.status.value,
                        "failure": result.failure_code,
                        "http_status": page.status_code if page else None,
                        "cards": len(result.listings),
                        "matching": sum(
                            matches_listing(item, config.criteria) for item in result.listings
                        ),
                        "newest_id": result.seen_source_ids[0] if result.seen_source_ids else None,
                    }
                ),
                flush=True,
            )
            if result.failure_code is not None:
                if page is not None and page.status_code == 439:
                    title = await transport._run(lambda: transport._driver.title)
                    visible = await transport._run(
                        lambda: transport._driver.find_element("tag name", "body").text[:800]
                    )
                    print(
                        "http_439_ui:", json.dumps({"title": title, "visible": visible}), flush=True
                    )
                    for _ in range(4):
                        await asyncio.sleep(10)
                        page = await transport.current_page()
                        parsed = parse_search_page(page.html, page.final_url)
                        print(
                            "http_439_settling:",
                            json.dumps(
                                {
                                    "status": page.status_code,
                                    "recognized": parsed.recognized,
                                    "blocked": parsed.blocked_reason,
                                    "cards": len(parsed.candidates),
                                }
                            ),
                            flush=True,
                        )
                        if parsed.recognized or parsed.blocked_reason:
                            break
                break
    finally:
        await transport.aclose()


asyncio.run(main())
