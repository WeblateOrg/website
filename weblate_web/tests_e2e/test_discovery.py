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

"""
End-to-end tests for Discover Weblate registration.

Tests cover:
- Registering a self-hosted Weblate server for discovery
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]

SCREENSHOT_DIR = Path("test-results")


def capture(page: Page, name: str) -> None:
    """Capture a full-page screenshot for Argos CI."""
    page.wait_for_load_state("networkidle")
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(
        path=(SCREENSHOT_DIR / f"discovery-{name}.png").as_posix(), full_page=True
    )


class TestDiscoveryRegistration:
    """Test suite for Discover Weblate registration."""

    def test_discovery_registration_stays_on_website(
        self, page: Page, live_server, authenticated_user
    ):
        """Test discovery registration does not redirect to the submitted URL."""
        page.goto(f"{live_server.url}/admin/login/")
        page.fill('input[name="username"]', "testuser")
        page.fill('input[name="password"]', "testpassword123")
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        response = page.goto(f"{live_server.url}/en/subscription/discovery/")
        assert response is not None
        assert response.ok, f"Discovery page returned status {response.status}"
        capture(page, "registration-form")

        page.fill('input[name="site_url"]', "https://evil.example")
        page.fill('textarea[name="discover_text"]', "Discover evil")
        page.click('input[type="submit"]')
        page.wait_for_load_state("networkidle")

        assert "/user/" in page.url, f"Not on user page. URL: {page.url}"
        assert "evil.example" not in page.url
        assert "activation=" not in page.url
        assert not page.locator("text=Server Error").is_visible()
        assert page.get_by_text("Activation token", exact=True).is_visible()
        assert page.locator(
            'input[value="e2e-fixed-service-secret-token-0123456789abcdef0123456789abcd"]'
        ).is_visible()
        assert page.locator('a[href*="activation="]').count() == 0
        capture(page, "activation-token")
