"""Pytest fixtures and configuration for Playwright e2e tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page

# Import Playwright config
try:
    import playwright.config as pw_config
except ImportError:
    # Fallback defaults if config file is not available
    class pw_config:  # type: ignore[no-redef]
        HEADLESS = True
        SLOW_MO = 0
        BASE_URL = "http://localhost:8000"
        DEFAULT_TIMEOUT = 30000
        VIDEO = None


@pytest.fixture(scope="session")
def browser_type_launch_args():
    """Configure browser launch arguments."""
    return {
        "headless": pw_config.HEADLESS,
        "slow_mo": pw_config.SLOW_MO,
    }


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser context arguments."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "record_video_dir": "test-results/videos" if pw_config.VIDEO else None,
    }


@pytest.fixture
def base_url():
    """Base URL for the application."""
    return pw_config.BASE_URL


@pytest.fixture
def context(browser: Browser, browser_context_args: dict, base_url: str) -> BrowserContext:
    """Create a new browser context for each test."""
    context = browser.new_context(**browser_context_args, base_url=base_url)
    yield context
    context.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    """Create a new page for each test."""
    page = context.new_page()
    page.set_default_timeout(pw_config.DEFAULT_TIMEOUT)
    yield page
    page.close()


@pytest.fixture
def authenticated_user(db):
    """Create a test user for authenticated tests."""
    user = User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpassword123",
    )
    return user
