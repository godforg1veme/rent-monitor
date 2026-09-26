from __future__ import annotations

import os
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rent_monitor.config import ConfigurationError, _load_raw

SEARCH_URL = (
    "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/"
    "bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
)


def config_text(*, url: str = SEARCH_URL, interval: int = 60, duplicate: bool = False) -> str:
    second = (
        f'''
[[sources.avito.searches]]
name = "moscow-rent"
url = "{SEARCH_URL}"
poll_interval_seconds = 120
'''
        if duplicate
        else ""
    )
    return f'''
[search]
city = "Москва"
rooms = 2
max_monthly_price_rub = 70000
require_no_commission = true

[limits]
max_response_bytes = 8388608

[sources.avito]
enabled = true

[[sources.avito.searches]]
name = "moscow-rent"
url = "{url}"
poll_interval_seconds = {interval}
{second}
[sources.yandex]
enabled = true
poll_interval_seconds = 300

[sources.cian]
enabled = false
poll_interval_seconds = 300

[sources.domclick]
enabled = false
poll_interval_seconds = 300

[captcha]
enabled = true
token_directory = "/run/rent-monitor-captcha/tokens"

[database]
path = "data/test.sqlite3"
'''


def load_text_config(text: str):
    return _load_raw(tomllib.loads(text), Path("config/search.toml"))


class ConfigTest(unittest.TestCase):
    def test_loads_avito_search_and_disables_future_sources(self) -> None:
        config = load_text_config(config_text())

        avito = next(source for source in config.sources if source.name == "avito")
        self.assertTrue(avito.enabled)
        self.assertEqual(avito.searches[0].name, "moscow-rent")
        self.assertEqual(avito.searches[0].poll_interval_seconds, 60)
        self.assertFalse(next(source for source in config.sources if source.name == "cian").enabled)
        self.assertFalse(
            next(source for source in config.sources if source.name == "domclick").enabled
        )
        self.assertEqual(config.captcha.token_directory, Path("/run/rent-monitor-captcha/tokens"))

    def test_rejects_duplicate_job_names(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_text_config(config_text(duplicate=True))

    def test_rejects_sub_minute_polling(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_text_config(config_text(interval=59))

    def test_rejects_non_https_or_credentialed_avito_url(self) -> None:
        for url in (
            "http://www.avito.ru/moskva/kvartiry",
            "https://user:password@www.avito.ru/moskva/kvartiry",
            "https://example.org/moskva/kvartiry",
        ):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                load_text_config(config_text(url=url))

    def test_environment_overrides_captcha_base_url(self) -> None:
        raw = tomllib.loads(config_text())
        raw["captcha"]["public_base_url"] = "https://from-config.example.ts.net"
        with patch.dict(
            os.environ,
            {"RENT_MONITOR_CAPTCHA_BASE_URL": "https://from-env.example.ts.net"},
        ):
            config = _load_raw(raw, Path("config/search.toml"))

        self.assertEqual(
            config.captcha.public_base_url,
            "https://from-env.example.ts.net",
        )


if __name__ == "__main__":
    unittest.main()
