"""Perform one diagnostic navigation, printing only public page structure."""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    import tomllib

    with open("/opt/rent-monitor/current/config/search.toml", "rb") as stream:
        config = tomllib.load(stream)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(
                Path(
                    sys.argv[1]
                    if len(sys.argv) > 1
                    else "/var/lib/rent-monitor/avito-browser-profile"
                )
            ),
            headless="--headless" in sys.argv,
        )
        try:
            page = context.pages[0]
            response = await page.goto(
                config["sources"]["avito"]["searches"][0]["url"],
                wait_until="domcontentloaded",
            )
            print("status", response.status if response else None)
            print("url", page.url)
            print("title", await page.title())
            print("body", (await page.locator("body").inner_text())[:1600])
            print(
                "markers",
                json.dumps(
                    await page.locator("[data-marker]").evaluate_all(
                        "elements => [...new Set(elements.map(e => "
                        "e.getAttribute('data-marker')))].slice(0,80)"
                    )
                ),
            )
        finally:
            await context.close()


asyncio.run(main())
