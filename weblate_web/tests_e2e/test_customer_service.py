"""
End-to-end tests for customer and service management.

Tests cover:
- Customer profile editing
- Service management
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


class TestCustomerManagement:  # pylint: disable=redefined-outer-name
    """Test suite for customer management features."""

    def test_customer_can_view_profile(
        self, page: Page, live_server, authenticated_user
    ):
        """Test that authenticated customer can view their profile."""
        # Create a session cookie for the authenticated user
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

        # Verify we're on the user page
        assert "/user/" in page.url


class TestServiceManagement:  # pylint: disable=redefined-outer-name
    """Test suite for service management features."""

    def test_user_can_view_services(self, page: Page, live_server, authenticated_user):
        """Test that user can view their services."""
        # Create a session cookie for the authenticated user
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

        # Verify page loaded
        assert "/user/" in page.url
