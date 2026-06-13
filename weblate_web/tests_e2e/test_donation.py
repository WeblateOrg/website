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

"""End-to-end tests for the donation flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]


def assert_no_server_error(page: Page) -> None:
    """Assert the page does not show a Django server error."""
    assert not page.locator("text=Server Error").is_visible()
    assert not page.locator("text=Internal Server Error").is_visible()


def wait_for_reward_animation(page: Page) -> None:
    """Wait for donation reward card transitions to settle."""
    page.evaluate(
        """
        () => new Promise((resolve) => {
          const rewards = document.querySelector(".rewards");
          if (!rewards) {
            resolve();
            return;
          }

          let resolved = false;
          const finish = () => {
            if (resolved) {
              return;
            }
            resolved = true;
            requestAnimationFrame(() => requestAnimationFrame(resolve));
          };

          requestAnimationFrame(() => {
            const animations = rewards
              .getAnimations({ subtree: true })
              .filter((animation) =>
                ["pending", "running"].includes(animation.playState),
              );
            if (!animations.length) {
              finish();
              return;
            }
            Promise.allSettled(
              animations.map((animation) => animation.finished),
            ).then(finish);
            window.setTimeout(finish, 1000);
          });
        })
        """,
    )


def capture_step(page: Page, name: str) -> None:
    """Capture a full-page screenshot for important donation flow steps."""
    wait_for_reward_animation(page)
    page.screenshot(path=f"test-results/donation-{name}.png", full_page=True)


def log_in(page: Page, live_server) -> None:
    """Log in through Django admin using the e2e test user."""
    page.goto(f"{live_server.url}/admin/login/")
    page.fill('input[name="username"]', "testuser")
    page.fill('input[name="password"]', "testpassword123")
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle")


def fill_billing_information(page: Page) -> None:
    """Fill the minimum billing information required before payment."""
    page.fill('input[name="name"]', "Donation Tester")
    page.fill('input[name="address"]', "123 Test Street")
    page.fill('input[name="city"]', "Test City")
    page.fill('input[name="postcode"]', "12345")
    page.select_option('select[name="country"]', "CZ")


def continue_from_billing_to_payment(page: Page) -> None:
    """Submit billing details and wait for the payment summary page."""
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)
    assert page.locator("text=Payment Summary").is_visible()


def complete_debug_payment(page: Page) -> None:
    """Pay using the debug payment backend enabled in e2e settings."""
    page.click('label[for="pay-pay"]')
    page.click('input[type="submit"].make-payment')
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)


class TestDonationFlow:  # pylint: disable=redefined-outer-name
    """Test suite for donations."""

    def test_one_time_donation_without_reward(
        self, page: Page, live_server, authenticated_user
    ):
        """Test completing a one-time donation without a reward."""
        log_in(page, live_server)

        response = page.goto(f"{live_server.url}/en/donate/new/")
        assert response is not None
        assert response.ok, f"Donation form returned status {response.status}"
        assert_no_server_error(page)
        capture_step(page, "one-time-01-form")

        page.fill('input[name="amount"]', "10")
        page.click('label[for="single-payment"]')
        page.click('button[data-reward-input="reward-0"]')
        capture_step(page, "one-time-02-selected")
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        assert_no_server_error(page)
        fill_billing_information(page)
        capture_step(page, "one-time-03-billing")
        continue_from_billing_to_payment(page)
        capture_step(page, "one-time-04-payment")
        complete_debug_payment(page)
        capture_step(page, "one-time-05-user")

        assert "/user/" in page.url, f"Expected user page, got: {page.url}"
        assert page.locator("text=My donations").is_visible()

    def test_recurring_donation_with_link_reward(
        self, page: Page, live_server, authenticated_user
    ):
        """Test completing a recurring donation with a link reward."""
        log_in(page, live_server)

        response = page.goto(f"{live_server.url}/en/donate/new/?recurring=y&amount=400")
        assert response is not None
        assert response.ok, f"Donation form returned status {response.status}"
        assert_no_server_error(page)
        capture_step(page, "reward-01-form")

        page.click('label[for="year"]')
        page.click('button[data-reward-input="reward-2"]')
        capture_step(page, "reward-02-selected")
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        assert_no_server_error(page)
        fill_billing_information(page)
        capture_step(page, "reward-03-billing")
        continue_from_billing_to_payment(page)
        capture_step(page, "reward-04-payment")
        complete_debug_payment(page)
        capture_step(page, "reward-05-edit")

        assert "/donate/edit/" in page.url, (
            f"Expected reward edit page, got: {page.url}"
        )
        assert page.locator("text=Edit your reward link").is_visible()
        assert page.locator('input[name="donation_link_text"]').is_visible()
        assert page.locator('input[name="donation_link_url"]').is_visible()
