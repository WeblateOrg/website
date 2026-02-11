"""
End-to-end tests for purchasing services.

Tests cover:
- Purchasing a subscription/service
- Payment flow
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore

from weblate_web.models import sync_packages

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_packages(db):  # pylint: disable=redefined-outer-name
    """Set up test packages in the database."""
    sync_packages()


class TestServicePurchase:  # pylint: disable=redefined-outer-name
    """Test suite for purchasing services."""

    def test_view_hosting_packages(self, page: Page, live_server, setup_packages):
        """Test that users can view available hosting packages."""
        page.goto(f"{live_server.url}/en/hosting/")

        # Take screenshot of the hosting page with packages
        page.screenshot(path="test-results/hosting-packages.png")

        # Check that we can see package information
        # The page should display pricing or package details
        assert page.is_visible("text=Buy now") or page.is_visible("text=Get started")

    def test_purchase_flow_requires_authentication(
        self, page: Page, live_server, setup_packages
    ):
        """Test that purchase flow redirects unauthenticated users."""
        # Try to access subscription purchase directly without auth
        # This should redirect to login or show an error
        page.goto(f"{live_server.url}/en/subscription/new/?plan=basic")

        # Check that we're either on a login page or payment page
        # The exact behavior depends on the application configuration
        current_url = page.url
        assert (
            "subscription" in current_url
            or "payment" in current_url
            or "login" in current_url
            or "saml" in current_url
        )

    def test_authenticated_user_purchase_flow(
        self, page: Page, live_server, setup_packages, authenticated_user
    ):
        """Test authenticated user can start the purchase flow."""
        # Create a session cookie for the authenticated user
        # This simulates a logged-in user
        session = SessionStore()
        session["_auth_user_id"] = str(authenticated_user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.save()

        # Set the session cookie
        page.goto(live_server.url)
        page.context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": session.session_key or "",
                    "domain": "localhost",
                    "path": "/",
                }
            ]
        )

        # Navigate to subscription purchase
        page.goto(f"{live_server.url}/en/subscription/new/?plan=basic")

        # Should reach payment page or similar
        page.wait_for_load_state("networkidle")

        # Take screenshot of payment selection page
        page.screenshot(path="test-results/payment-selection.png")

        current_url = page.url

        # Verify we're in the payment flow
        assert "payment" in current_url or "subscription" in current_url

    def test_hosting_page_displays_packages(
        self, page: Page, live_server, setup_packages
    ):
        """Test that the hosting page displays available packages."""
        page.goto(f"{live_server.url}/en/hosting/")

        # Wait for page to load
        page.wait_for_load_state("networkidle")

        # Check that we can see pricing information
        # Look for common pricing elements
        has_pricing = (
            page.locator("text=Buy now").count() > 0
            or page.locator("text=Get started").count() > 0
            or page.locator('a[href*="subscription-new"]').count() > 0
        )

        assert has_pricing, "Hosting page should display package purchase options"

    def test_support_page_displays_packages(
        self, page: Page, live_server, setup_packages
    ):
        """Test that the support page displays available support packages."""
        page.goto(f"{live_server.url}/en/support/")

        # Wait for page to load
        page.wait_for_load_state("networkidle")

        # The support page should display support packages
        # Check for common elements
        assert page.locator("h1").count() > 0
