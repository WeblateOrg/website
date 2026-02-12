"""
End-to-end tests for purchasing services.

Tests cover:
- Purchasing a subscription/service
- Payment flow
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


class TestServicePurchase:  # pylint: disable=redefined-outer-name
    """Test suite for purchasing services."""

    def test_view_hosting_packages(self, page: Page, live_server):
        """Test that users can view available hosting packages."""
        response = page.goto(f"{live_server.url}/en/hosting/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Hosting page returned status {response.status}"

        # Take screenshot of the hosting page with packages
        page.screenshot(path="test-results/hosting-packages.png", full_page=True)

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Check that we can see package information
        # The page should display pricing or package details
        assert page.is_visible("text=Buy now") or page.is_visible("text=Get started")

    def test_purchase_flow_requires_authentication(self, page: Page, live_server):
        """Test that purchase flow redirects unauthenticated users."""
        # Try to access subscription purchase directly without auth
        # This should redirect to login or show an error
        response = page.goto(f"{live_server.url}/en/subscription/new/?plan=basic")

        # Check response is successful (200-level or redirect)
        assert response is not None
        assert response.status < 500, (
            f"Subscription page returned server error {response.status}"
        )

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

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
        self, page: Page, live_server, authenticated_user
    ):
        """Test authenticated user can complete the purchase flow."""
        # Log in through the admin interface
        page.goto(f"{live_server.url}/admin/login/")

        # Fill in login form
        page.fill('input[name="username"]', "testuser")
        page.fill('input[name="password"]', "testpassword123")

        # Take screenshot of login page
        page.screenshot(path="test-results/01-login-page.png", full_page=True)

        # Submit login
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Take screenshot after login
        page.screenshot(path="test-results/02-after-admin-login.png", full_page=True)

        # Navigate to subscription purchase - this should redirect to customer page
        page.goto(f"{live_server.url}/en/subscription/new/?plan=basic")
        page.wait_for_load_state("networkidle")

        # Take screenshot of where we landed
        page.screenshot(path="test-results/03-after-subscription-redirect.png", full_page=True)

        current_url = page.url

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()
        assert not page.locator("text=Internal Server Error").is_visible()

        # Take final screenshot
        page.screenshot(path="test-results/payment-selection.png", full_page=True)

        # Authenticated user should be in the payment flow
        # (either on customer info page or payment selection page)
        assert "payment" in current_url or "customer" in current_url, (
            f"Not in payment flow. URL: {current_url}"
        )

        # Should NOT be on login page
        assert "login" not in current_url.lower(), (
            f"Still on login page. URL: {current_url}"
        )
        assert "saml" not in current_url.lower(), (
            f"Redirected to SAML. URL: {current_url}"
        )

    def test_hosting_page_displays_packages(self, page: Page, live_server):
        """Test that the hosting page displays available packages."""
        response = page.goto(f"{live_server.url}/en/hosting/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Hosting page returned status {response.status}"

        # Wait for page to load
        page.wait_for_load_state("networkidle")

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Check that we can see pricing information
        # Look for common pricing elements
        has_pricing = (
            page.locator("text=Buy now").count() > 0
            or page.locator("text=Get started").count() > 0
            or page.locator('a[href*="subscription-new"]').count() > 0
        )

        assert has_pricing, "Hosting page should display package purchase options"

    def test_support_page_displays_packages(self, page: Page, live_server):
        """Test that the support page displays available support packages."""
        response = page.goto(f"{live_server.url}/en/support/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Support page returned status {response.status}"

        # Wait for page to load
        page.wait_for_load_state("networkidle")

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # The support page should display support packages
        # Check for common elements
        assert page.locator("h1").count() > 0
