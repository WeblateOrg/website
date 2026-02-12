"""Pytest fixtures and configuration for Playwright e2e tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from weblate_web.models import sync_packages

# Allow Django operations in async context for Playwright tests
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


@pytest.fixture(autouse=True)
def configure_test_settings(settings):  # pylint: disable=redefined-outer-name
    """Configure Django settings for E2E tests."""
    # Use local login instead of SAML for tests
    settings.LOGIN_URL = "/admin/login/"
    # Enable payment debug mode to use mock payment backends
    settings.PAYMENT_DEBUG = True


@pytest.fixture(autouse=True)
def mock_external_apis():
    """Mock external API calls for e2e tests."""
    with (
        patch("weblate_web.remote.get_changes", return_value=[]),
        patch("weblate_web.remote.get_contributors", return_value=[]),
        patch("weblate_web.remote.get_activity", return_value=[]),
        patch("weblate_web.remote.get_release", return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def setup_packages(db):
    """Set up test packages in the database for all tests."""
    sync_packages()


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
        password="testpassword123",  # noqa: S106
    )
    # Make user staff so they can use admin login for E2E tests
    user.is_staff = True
    user.save()
    return user
