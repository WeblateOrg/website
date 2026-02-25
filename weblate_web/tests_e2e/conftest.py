"""Pytest fixtures and configuration for Playwright e2e tests."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch
from weblate_web.models import Package


import pytest
import responses
from django.contrib.auth.models import User

from weblate_web.models import sync_packages
from weblate_web.tests import mock_vies

# Allow Django operations in async context for Playwright tests
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


@pytest.fixture(autouse=True)
def configure_test_settings(settings, live_server):  # pylint: disable=redefined-outer-name
    """Configure Django settings for E2E tests."""
    # Use local login instead of SAML for tests
    settings.LOGIN_URL = "/admin/login/"
    # Enable payment debug mode to use mock payment backends
    settings.PAYMENT_DEBUG = True
    # Set SITE_URL to match live_server so payment redirects work correctly
    settings.SITE_URL = live_server.url


@pytest.fixture(autouse=True)
def mock_external_apis():
    """Mock external API calls and VIES validation for e2e tests."""
    with (
        patch("weblate_web.remote.get_changes", return_value=[]),
        patch("weblate_web.remote.get_contributors", return_value=[]),
        patch("weblate_web.remote.get_activity", return_value=[]),
        patch("weblate_web.remote.get_release", return_value=None),
        patch(
            "weblate_web.exchange_rates.ExchangeRates.download",
            return_value={
                "EUR": Decimal("25.215"),
                "USD": Decimal("22.425"),
                "GBP": Decimal("28.635"),
                "CZK": Decimal("1"),
            },
        ),
        responses.RequestsMock(),
    ):
        # Mock VIES VAT validation service
        mock_vies()
        yield


@pytest.fixture(autouse=True)
def setup_packages(db):
    """Set up test packages in the database for all tests."""

    sync_packages()
    Package.objects.get_or_create(
        name="community", defaults={"verbose": "Community support", "price": 0}
    )


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
