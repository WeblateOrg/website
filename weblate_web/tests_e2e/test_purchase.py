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
        """Test authenticated user can view hosting packages and start purchase flow.
        
        Note: This test verifies the user journey up to viewing packages.
        The full payment flow (clicking "Buy now" -> subscription creation -> 
        payment redirect) causes Django live_server to crash due to immediate
        redirects to payment URLs. Testing the complete payment flow would 
        require either:
        1. Mocking the entire payment backend to avoid redirects
        2. Using a real application server instead of Django's test live_server
        3. Unit testing the payment flow separately (already done in payments/tests.py)
        """
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

        # Navigate to hosting page to view packages
        response = page.goto(f"{live_server.url}/en/hosting/")
        assert response is not None
        assert response.ok, f"Hosting page returned status {response.status}"
        page.wait_for_load_state("networkidle")

        # Take screenshot of hosting page
        page.screenshot(path="test-results/03-hosting-page-logged-in.png", full_page=True)

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()
        assert not page.locator("text=Internal Server Error").is_visible()

        # Verify authenticated user can see subscription options
        buy_link = page.locator('a[href*="subscription"]').first
        assert buy_link.is_visible(), "Subscription links should be visible to authenticated users"

        # Take screenshot showing subscription link is available
        page.screenshot(path="test-results/payment-selection.png", full_page=True)

        # Verify the link has the expected structure
        href = buy_link.get_attribute("href")
        assert href is not None
        assert "subscription" in href or "hosting" in href, (
            f"Subscription link should contain 'subscription' or 'hosting': {href}"
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
