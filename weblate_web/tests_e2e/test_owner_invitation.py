# Copyright © Michal Čihař <michal@weblate.org>
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

"""End-to-end coverage for customer owner invitations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from django.contrib.auth.models import User
from django.core import mail

from weblate_web.models import Service
from weblate_web.payments.models import Customer, CustomerOwnerInvitation
from weblate_web.utils import PAYMENTS_ORIGIN

if TYPE_CHECKING:
    from django.core.mail import EmailAlternative, EmailMultiAlternatives
    from playwright.sync_api import Page

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("e2e_setup")]

SCREENSHOT_DIR = Path("test-results")
PASSWORD = "owner-invitation-password"  # ruff: ignore[hardcoded-password-string]


def log_in(page: Page, live_server, user: User) -> None:
    page.context.clear_cookies()
    page.goto(f"{live_server.url}/admin/login/")
    page.fill('input[name="username"]', user.username)
    page.fill('input[name="password"]', PASSWORD)
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle")


def capture(page: Page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(
        path=(SCREENSHOT_DIR / f"customer-owner-{name}.png").as_posix(),
        full_page=True,
    )


def test_customer_owner_invitation_flow(page: Page, live_server) -> None:
    owner = User.objects.create_user(
        username="inviting-owner",
        email="owner@example.test",
        password=PASSWORD,
        is_staff=True,
    )
    invitee = User.objects.create_user(
        username="invited-owner",
        email="invitee@example.test",
        password=PASSWORD,
        is_staff=True,
    )
    customer = Customer.objects.create(
        name="Invitation Customer",
        email=owner.email,
        origin=PAYMENTS_ORIGIN,
        user_id=owner.pk,
    )
    customer.owners.add(owner)
    Service.objects.create(
        customer=customer,
        site_title="Invitation Service",
        site_url="https://invitation.example.test/",
    )

    log_in(page, live_server, owner)
    page.goto(f"{live_server.url}/en/user/")
    page.fill('input[name="email"]', invitee.email)
    page.get_by_role("button", name="Invite owner").click()
    page.wait_for_load_state("networkidle")

    invitation = CustomerOwnerInvitation.objects.get(customer=customer)
    assert invitee not in customer.owners.all()
    assert invitee.email not in customer.get_notify_emails()
    assert page.get_by_text("Pending invitation", exact=True).is_visible()
    capture(page, "invitation-pending")

    message = cast("EmailMultiAlternatives", mail.outbox[0])
    alternative = cast("EmailAlternative", message.alternatives[0])
    html = str(alternative.content)
    assert customer.verbose_name in message.subject
    assert customer.verbose_name in html
    match = re.search(
        r'href="([^"]+/customer/owner-invitation/[^"]+/)"',
        html,
    )
    assert match is not None
    invitation_url = match.group(1)

    log_in(page, live_server, invitee)
    page.goto(invitation_url)
    assert page.get_by_role("heading", name="Customer owner invitation").is_visible()
    assert page.get_by_text("Invitation Customer", exact=True).is_visible()
    capture(page, "invitation-confirmation")
    page.get_by_role("button", name="Accept invitation").click()
    page.wait_for_load_state("networkidle")

    invitation.refresh_from_db()
    assert invitation.status == CustomerOwnerInvitation.Status.ACCEPTED
    assert invitee in customer.owners.all()
    assert invitee.email in customer.get_notify_emails()
    capture(page, "invitation-accepted")
