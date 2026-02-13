"""
End-to-end tests for customer and service management.

Tests cover:
- Customer profile editing
- Service management
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


class TestCustomerManagement:  # pylint: disable=redefined-outer-name
    """Test suite for customer management features."""

    def test_customer_can_view_profile(
        self, page: Page, live_server, authenticated_user
    ):
        """Test that authenticated customer can view their profile."""
        # Log in through the admin interface
        page.goto(f"{live_server.url}/admin/login/")

        # Fill in login form
        page.fill('input[name="username"]', "testuser")
        page.fill('input[name="password"]', "testpassword123")

        # Submit login
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Take screenshot after auth
        page.screenshot(
            path="test-results/04-customer-authenticated.png", full_page=True
        )

        # Navigate to user page
        response = page.goto(f"{live_server.url}/en/user/")
        page.wait_for_load_state("networkidle")

        # Check response is successful
        assert response is not None
        assert response.ok, f"User page returned status {response.status}"

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Take screenshot of user profile page
        page.screenshot(path="test-results/user-profile.png", full_page=True)

        # Verify we're on the user page (not redirected to login)
        assert "login" not in page.url.lower(), (
            f"Authenticated user redirected to login. URL: {page.url}"
        )
        assert "/user/" in page.url, f"Not on user profile page. URL: {page.url}"


class TestServiceManagement:  # pylint: disable=redefined-outer-name
    """Test suite for service management features."""

    def test_user_can_view_services(self, page: Page, live_server, authenticated_user):
        """Test that authenticated user can view their services."""
        # Log in through the admin interface
        page.goto(f"{live_server.url}/admin/login/")

        # Fill in login form
        page.fill('input[name="username"]', "testuser")
        page.fill('input[name="password"]', "testpassword123")

        # Submit login
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Navigate to user page to see services
        response = page.goto(f"{live_server.url}/en/user/")
        page.wait_for_load_state("networkidle")

        # Check response is successful
        assert response is not None
        assert response.ok, f"User page returned status {response.status}"

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Take screenshot of services section
        page.screenshot(path="test-results/user-services.png", full_page=True)

        # Verify page loaded (not redirected to login)
        assert "login" not in page.url.lower(), (
            f"Authenticated user redirected to login. URL: {page.url}"
        )
        assert "/user/" in page.url, f"Not on user services page. URL: {page.url}"
