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

    def test_customer_profile_requires_authentication(
        self, page: Page, live_server
    ):
        """Test that user profile page requires authentication."""
        # Try to access user page without authentication
        response = page.goto(f"{live_server.url}/en/user/")
        page.wait_for_load_state("networkidle")

        # Check response
        assert response is not None

        # Take screenshot after navigation
        page.screenshot(
            path="test-results/02-customer-requires-auth.png", full_page=True
        )

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Take screenshot of user profile page (or login redirect)
        page.screenshot(path="test-results/user-profile.png", full_page=True)

        # Unauthenticated users should be redirected to login
        is_on_login = "login" in page.url.lower() or "saml" in page.url.lower()
        assert is_on_login, (
            f"Expected redirect to login page, but got: {page.url}"
        )


class TestServiceManagement:  # pylint: disable=redefined-outer-name
    """Test suite for service management features."""

    def test_service_management_requires_authentication(
        self, page: Page, live_server
    ):
        """Test that service management requires authentication."""
        # Try to access user page without authentication
        response = page.goto(f"{live_server.url}/en/user/")
        page.wait_for_load_state("networkidle")

        # Check response
        assert response is not None

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

        # Take screenshot of services section (or login redirect)
        page.screenshot(path="test-results/user-services.png", full_page=True)

        # Unauthenticated users should be redirected to login
        is_on_login = "login" in page.url.lower() or "saml" in page.url.lower()
        assert is_on_login, (
            f"Expected redirect to login page, but got: {page.url}"
        )
