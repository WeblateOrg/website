"""Pytest fixtures and configuration for Playwright e2e tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

# Allow Django operations in async context for Playwright tests
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


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
    return User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpassword123",  # noqa: S106
    )
