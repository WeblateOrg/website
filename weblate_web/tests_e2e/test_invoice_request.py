#
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
#

"""End-to-end coverage for customer-care renewal invoice requests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from weblate_web.crm.models import Interaction
from weblate_web.tests_e2e.test_donation import log_in
from weblate_web.tests_e2e.test_upgrade import create_active_subscription

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from playwright.sync_api import Page

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("e2e_setup")]


def test_invoice_request(page: Page, live_server, authenticated_user: User) -> None:
    subscription = create_active_subscription(authenticated_user, "basic")
    log_in(page, live_server)
    page.goto(f"{live_server.url}/en/user/")
    request_button = page.get_by_role("button", name="Request invoice", exact=True)
    assert request_button.is_visible()
    screenshot_dir = Path("test-results")
    screenshot_dir.mkdir(exist_ok=True)
    page.screenshot(
        path=str(screenshot_dir / "invoice-request-user.png"), full_page=True
    )
    request_button.click()
    assert page.get_by_role("heading", name="Request invoice", exact=True).is_visible()
    page.get_by_label("Customer reference").fill("PO-2026-001")
    page.screenshot(
        path=str(screenshot_dir / "invoice-request-form.png"), full_page=True
    )
    with (
        patch(
            "weblate_web.invoice_requests.create_invoice_request_ticket",
            return_value={"id": 42, "number": "10042"},
        ) as ticket,
        patch("weblate_web.invoice_requests.create_invoice_request_article") as article,
    ):
        page.get_by_role("button", name="Send invoice request").click()
        assert page.get_by_text(
            "Customer care has received your invoice request.", exact=False
        ).is_visible()
        request_button.click()
        page.get_by_role("button", name="Send invoice request").click()
        ticket.assert_called_once()
        article.assert_called_once()
    interaction = Interaction.objects.get(origin=Interaction.Origin.INVOICE_REQUEST)
    assert interaction.details["subscription_id"] == subscription.pk
    assert interaction.details["customer_reference"] == "PO-2026-001"
    assert interaction.details["status"] == "submitted"
