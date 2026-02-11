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
        assert page.title() != ""

        # Check for key elements on the homepage
        # The Weblate logo or heading should be visible
        assert page.is_visible("text=Weblate") or page.locator("h1").is_visible()

    def test_navigation_to_hosting_page(self, page: Page, live_server):
        """Test navigation to the hosting page."""
        page.goto(live_server.url)

        # Navigate to hosting page
        page.click("text=Hosting", timeout=5000)

        # Verify we're on the hosting page
        page.wait_for_url("**/hosting/**", timeout=5000)
        assert "hosting" in page.url.lower()

    def test_navigation_to_features_page(self, page: Page, live_server):
        """Test navigation to the features page."""
        page.goto(live_server.url)

        # Navigate to features page
        page.click("text=Features", timeout=5000)

        # Verify we're on the features page
        page.wait_for_url("**/features/**", timeout=5000)
        assert "features" in page.url.lower()

    def test_navigation_to_support_page(self, page: Page, live_server):
        """Test navigation to the support page."""
        page.goto(live_server.url)

        # Navigate to support page
        page.click("text=Support", timeout=5000)

        # Verify we're on the support page
        page.wait_for_url("**/support/**", timeout=5000)
        assert "support" in page.url.lower()

    def test_navigation_to_donate_page(self, page: Page, live_server):
        """Test navigation to the donate page."""
        page.goto(live_server.url)

        # Navigate to donate page
        page.click("text=Donate", timeout=5000)

        # Verify we're on the donate page
        page.wait_for_url("**/donate/**", timeout=5000)
        assert "donate" in page.url.lower()
