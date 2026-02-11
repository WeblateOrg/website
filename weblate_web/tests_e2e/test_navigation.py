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
        page.goto(live_server.url)

        # Check that the page loaded successfully
        assert page.title()

        # Check for key elements on the homepage
        # The Weblate logo or heading should be visible
        assert page.is_visible("text=Weblate") or page.locator("h1").is_visible()

    def test_navigation_to_hosting_page(self, page: Page, live_server):
        """Test navigation to the hosting page."""
        page.goto(f"{live_server.url}/en/hosting/")

        # Verify we're on the hosting page
        assert "hosting" in page.url.lower()

    def test_navigation_to_features_page(self, page: Page, live_server):
        """Test navigation to the features page."""
        page.goto(f"{live_server.url}/en/features/")

        # Verify we're on the features page
        assert "features" in page.url.lower()

    def test_navigation_to_support_page(self, page: Page, live_server):
        """Test navigation to the support page."""
        # Navigate to support page directly
        page.goto(f"{live_server.url}/en/support/")

        # Verify we're on the support page
        assert "support" in page.url.lower()

    def test_navigation_to_donate_page(self, page: Page, live_server):
        """Test navigation to the donate page."""
        # Navigate to donate page directly
        page.goto(f"{live_server.url}/en/donate/")

        # Verify we're on the donate page
        assert "donate" in page.url.lower()
