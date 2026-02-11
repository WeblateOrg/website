"""Pytest fixtures and configuration for Playwright e2e tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

# Allow Django operations in async context for Playwright tests
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

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


@pytest.fixture(scope="session", autouse=True)
def mock_external_apis():
    """Mock external API calls for e2e tests."""
    with (
        patch("weblate_web.remote.get_changes", return_value=[]),
        patch("weblate_web.remote.get_contributors", return_value=[]),
        patch("weblate_web.remote.get_activity", return_value=[]),
    ):
        yield


@pytest.fixture(scope="session")
def base_url(live_server):
    """Provide base URL for playwright tests from live_server."""
    return live_server.url


@pytest.fixture
def authenticated_user(db):
    """Create a test user for authenticated tests."""
    user = User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpassword123",
    )
    return user
