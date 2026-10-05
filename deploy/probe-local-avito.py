"""One local browser navigation using an optional existing proxy."""

import argparse
import asyncio
import tomllib
from pathlib import Path

from playwright.async_api import async_playwright

from rent_monitor.parsers.avito import parse_search_page


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with (root / "config/search.toml").open("rb") as stream:
        config = tomllib.load(stream)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(root / "data" / f"local-probe-{args.channel}"),
            headless=False,
            channel=None if args.channel == "chromium" else args.channel,
            proxy={"server": args.proxy} if args.proxy else None,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0]
            response = await page.goto(
                config["sources"]["avito"]["searches"][0]["url"],
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_timeout(3000)
            parsed = parse_search_page(await page.content(), page.url)
            print("http", response.status if response else None)
            print("title", await page.title())
            print("page", (await page.locator("body").inner_text())[:600])
            print(
                "recognized",
                parsed.recognized,
                "blocked",
                parsed.blocked_reason,
                "cards",
                len(parsed.candidates),
            )
        finally:
            await context.close()


asyncio.run(main())
