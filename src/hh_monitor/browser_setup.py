from __future__ import annotations

import logging
import subprocess
import sys

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

_BROWSER_READY = False


def ensure_chromium_installed(logger: logging.Logger) -> None:
    global _BROWSER_READY
    if _BROWSER_READY:
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        _BROWSER_READY = True
        return
    except PlaywrightError as exc:
        text = str(exc).lower()
        needs_install = "executable doesn't exist" in text or "playwright install" in text
        if not needs_install:
            raise
        logger.info("playwright chromium missing, installing...")

    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    subprocess.run(cmd, check=True)
    _BROWSER_READY = True
    logger.info("playwright chromium installed")
