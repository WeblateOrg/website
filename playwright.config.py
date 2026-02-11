"""Playwright configuration for end-to-end testing."""

from __future__ import annotations

import os

# Base URL for the application
BASE_URL = os.getenv("PLAYWRIGHT_BASE_URL", "http://localhost:8000")

# Headless mode (set to False for debugging)
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"

# Browser to use for tests
BROWSER = os.getenv("PLAYWRIGHT_BROWSER", "chromium")

# Slow down operations for debugging (in milliseconds)
SLOW_MO = int(os.getenv("PLAYWRIGHT_SLOW_MO", "0"))

# Screenshot on failure
SCREENSHOT_ON_FAILURE = True

# Video recording
VIDEO = os.getenv("PLAYWRIGHT_VIDEO", "retain-on-failure")

# Timeout settings (in milliseconds)
DEFAULT_TIMEOUT = 30000
NAVIGATION_TIMEOUT = 30000
