#
# Copyright (C) Weblate
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""End-to-end tests for pro-rated subscription upgrades."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.utils import timezone

from weblate_web.models import Package, Service
from weblate_web.payments.models import Customer, Payment
from weblate_web.tests_e2e.test_donation import (
    assert_no_server_error,
    complete_debug_payment,
    log_in,
)
from weblate_web.utils import PAYMENTS_ORIGIN

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from playwright.sync_api import Page

    from weblate_web.models import Subscription

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]


def create_active_subscription(user: User, package_name: str) -> Subscription:
    """Create an active subscription visible on the user profile page."""
    customer = Customer.objects.create(
        user_id=user.pk,
        origin=PAYMENTS_ORIGIN,
        name="Upgrade Tester",
        address="123 Test Street",
        city="Test City",
        postcode="12345",
        country="US",
    )
    customer.users.add(user)
    package = Package.objects.get(name=package_name)
    payment = Payment.objects.create(
        amount=package.price,
        customer=customer,
        description=package.verbose,
        recurring=package.get_repeat(),
        state=Payment.PROCESSED,
    )
    service = Service.objects.create(customer=customer)
    return service.subscription_set.create(
        package=package,
        payment=payment,
        expires=timezone.now() + timedelta(days=90),
    )


def submit_upgrade(page: Page, package_name: str) -> None:
    """Submit the upgrade form for the selected target package."""
    form = page.locator('form[action*="/subscription/upgrade/"]').filter(
        has=page.locator(f'input[name="package"][value="{package_name}"]')
    )
    assert form.count() == 1
    form.locator('input[type="submit"]').click()
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)


class TestSubscriptionUpgradeFlow:  # pylint: disable=redefined-outer-name
    """Test suite for subscription upgrades."""

    def test_support_upgrade_to_premium(
        self, page: Page, live_server, authenticated_user: User
    ) -> None:
        """Test upgrading a basic support subscription to premium support."""
        subscription = create_active_subscription(authenticated_user, "basic")
        original_expires = subscription.expires

        log_in(page, live_server)
        response = page.goto(f"{live_server.url}/en/user/")
        assert response is not None
        assert response.ok, f"User page returned status {response.status}"
        assert_no_server_error(page)
        page.screenshot(path="test-results/upgrade-support-options.png", full_page=True)
        assert page.locator("text=Upgrade to Weblate premium").is_visible()

        submit_upgrade(page, "premium")
        page.screenshot(path="test-results/upgrade-support-payment.png", full_page=True)
        assert page.locator("text=Payment Summary").is_visible()
        complete_debug_payment(page)
        page.screenshot(path="test-results/upgrade-support-user.png", full_page=True)

        subscription.refresh_from_db()
        assert subscription.package.name == "premium"
        assert subscription.expires == original_expires
        assert "/user/" in page.url, f"Expected user page, got: {page.url}"
        assert page.get_by_text(
            "Weblate premium self-hosted support (yearly)", exact=True
        ).is_visible()

    def test_hosted_upgrade(
        self, page: Page, live_server, authenticated_user: User
    ) -> None:
        """Test upgrading a hosted subscription to the next hosted plan."""
        subscription = create_active_subscription(authenticated_user, "hosted:10k")
        original_expires = subscription.expires

        log_in(page, live_server)
        response = page.goto(f"{live_server.url}/en/user/")
        assert response is not None
        assert response.ok, f"User page returned status {response.status}"
        assert_no_server_error(page)
        page.screenshot(path="test-results/upgrade-hosted-options.png", full_page=True)
        assert page.locator("text=Upgrade to Weblate hosting (40k strings").is_visible()

        submit_upgrade(page, "hosted:40k")
        page.screenshot(path="test-results/upgrade-hosted-payment.png", full_page=True)
        assert page.locator("text=Payment Summary").is_visible()
        complete_debug_payment(page)
        page.screenshot(path="test-results/upgrade-hosted-user.png", full_page=True)

        subscription.refresh_from_db()
        assert subscription.package.name == "hosted:40k"
        assert subscription.expires == original_expires
        assert "/user/" in page.url, f"Expected user page, got: {page.url}"
        assert page.get_by_text(
            "Weblate hosting (40k strings, yearly)", exact=True
        ).is_visible()
