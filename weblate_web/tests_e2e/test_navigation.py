"""
End-to-end tests for basic website navigation and user flows.

Tests cover:
- New user visiting the website
- Navigating through key pages
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


class TestWebsiteNavigation:
    """Test suite for basic website navigation."""

    def test_new_user_visits_homepage(self, page: Page, live_server):
        """Test that a new user can visit the homepage and see key content."""
        # Visit the homepage
        response = page.goto(live_server.url)

        # Check that the page loaded successfully without errors
        assert response is not None
        assert response.ok, f"Homepage returned status {response.status}"

        # Check that the page loaded successfully
        assert page.title()

        # Verify no server error is displayed
        # Check for common error indicators, but be specific to avoid false positives
        assert not page.locator("text=Server Error").is_visible()
        assert not page.locator("text=Internal Server Error").is_visible()

        # Check for key elements on the homepage
        # The Weblate logo or heading should be visible
        assert page.is_visible("text=Weblate") or page.locator("h1").is_visible()

    def test_navigation_to_hosting_page(self, page: Page, live_server):
        """Test navigation to the hosting page."""
        response = page.goto(f"{live_server.url}/en/hosting/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Hosting page returned status {response.status}"

        # Verify we're on the hosting page
        assert "hosting" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_features_page(self, page: Page, live_server):
        """Test navigation to the features page."""
        response = page.goto(f"{live_server.url}/en/features/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Features page returned status {response.status}"

        # Verify we're on the features page
        assert "features" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_support_page(self, page: Page, live_server):
        """Test navigation to the support page."""
        # Navigate to support page directly
        response = page.goto(f"{live_server.url}/en/support/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Support page returned status {response.status}"

        # Verify we're on the support page
        assert "support" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_donate_page(self, page: Page, live_server):
        """Test navigation to the donate page."""
        # Navigate to donate page directly
        response = page.goto(f"{live_server.url}/en/donate/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Donate page returned status {response.status}"

        # Verify we're on the donate page
        assert "donate" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()
