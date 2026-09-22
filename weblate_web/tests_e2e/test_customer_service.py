"""
End-to-end tests for customer and service management.

Tests cover:
- Customer profile editing
- Service management
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from weblate_web.models import Package, Service
from weblate_web.payments.models import Customer, Payment
from weblate_web.tests_e2e.test_donation import log_in
from weblate_web.utils import PAYMENTS_ORIGIN

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]


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

    @pytest.mark.parametrize("width", [1440, 390])
    @pytest.mark.parametrize(
        "provisioned", [False, True], ids=["unprovisioned", "provisioned"]
    )
    @pytest.mark.parametrize("expired", [False, True], ids=["active", "expired"])
    def test_backup_setup_guidance(  # ruff: ignore[too-many-arguments]
        self,
        page: Page,
        live_server,
        authenticated_user: User,
        *,
        width: int,
        provisioned: bool,
        expired: bool,
    ) -> None:
        """Capture backup setup guidance and repository access across expiry states."""
        customer = Customer.objects.create(
            user_id=authenticated_user.pk,
            origin=PAYMENTS_ORIGIN,
            name="Backup Tester",
        )
        customer.owners.add(authenticated_user)
        repository = "ssh://backup.example.com/./backups"
        service = Service.objects.create(
            customer=customer, backup_repository=repository if provisioned else ""
        )
        package = Package.objects.get(name="backup")
        payment = Payment.objects.create(
            uuid=UUID(int=1),
            amount=package.price,
            customer=customer,
            description=package.verbose,
            recurring=package.get_repeat(),
            state=Payment.PROCESSED,
        )
        # e2e_setup uses mock_external_apis to freeze timezone.now() for screenshots.
        subscription = service.subscription_set.create(
            package=package,
            payment=payment,
            expires=timezone.now() + timedelta(days=-30 if expired else 30),
        )
        subscription.created = timezone.now() - timedelta(days=365)
        subscription.save(update_fields=["created"])

        page.set_viewport_size({"width": width, "height": 1000})
        log_in(page, live_server)
        response = page.goto(f"{live_server.url}/en/user/")
        assert response is not None and response.ok
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")

        guidance = page.get_by_text("Set up your backups", exact=True)
        connection = page.get_by_text(
            "Connect your Weblate installation using the activation token to provision your backup storage.",
            exact=True,
        )
        if expired:
            expect(guidance).to_have_count(0)
            expect(connection).to_have_count(0)
        else:
            expect(guidance).to_be_visible()
            expect(connection).to_be_visible()
        repository_field = page.locator(f'input[value="{repository}"]')
        if provisioned:
            expect(repository_field).to_be_visible()
        else:
            expect(repository_field).to_have_count(0)

        screenshot_dir = Path("test-results")
        screenshot_dir.mkdir(exist_ok=True)
        state = "expired" if expired else "active"
        storage = "provisioned" if provisioned else "unprovisioned"
        page.screenshot(
            path=str(screenshot_dir / f"backup-setup-{state}-{storage}-{width}.png"),
            full_page=True,
        )

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
