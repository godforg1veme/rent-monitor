"""Resolve Mozilla geckodriver with Selenium Manager and install a service-readable binary."""

import shutil
from pathlib import Path

from selenium.webdriver.common.selenium_manager import SeleniumManager

result = SeleniumManager().binary_paths(
    [
        "--browser",
        "firefox",
        "--browser-path",
        "/usr/bin/firefox",
        "--avoid-stats",
    ]
)
destination = Path("/opt/rent-monitor/geckodriver")
shutil.copyfile(result["driver_path"], destination)
destination.chmod(0o755)
print("Service geckodriver installed")
