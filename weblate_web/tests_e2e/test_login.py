# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, Page, Route

pytestmark = pytest.mark.django_db(transaction=True)


def test_login_automatically_posts(page: Page, live_server, settings) -> None:
    settings.COMPRESS_ENABLED = False
    requests = []

    def begin(route: Route) -> None:
        requests.append(route.request)
        route.fulfill(body="<!doctype html><title>Login started</title>")

    page.route("**/oidc/login/weblate/", begin)
    with page.expect_request("**/oidc/login/weblate/") as sent:
        page.goto(f"{live_server.url}/oidc/?next=/user/")
    page.wait_for_url("**/oidc/login/weblate/")
    assert len(requests) == 1
    assert sent.value.method == "POST"
    data = parse_qs(sent.value.post_data or "")
    assert data["next"] == ["/user/"]
    assert data["csrfmiddlewaretoken"][0]


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
def test_login_fallback_without_javascript(
    browser: Browser, live_server, settings, tmp_path: Path, width: int
) -> None:
    settings.COMPRESS_ENABLED = False
    context = browser.new_context(
        java_script_enabled=False,
        locale="cs-CZ",
        viewport={"width": width, "height": 900},
    )
    try:
        page = context.new_page()
        page.route(
            "**/oidc/login/weblate/",
            lambda route: route.fulfill(
                body="<!doctype html><title>Login started</title>"
            ),
        )
        page.goto(f"{live_server.url}/oidc/?next=/user/")
        assert page.locator("html").get_attribute("lang") == "cs"
        assert page.locator("h1").inner_text() == "Přihlásit se"
        button = page.get_by_role("button", name="Přihlásit se", exact=True)
        assert button.is_visible()
        box = button.bounding_box()
        assert box is not None
        assert box["height"] >= 50
        assert abs(box["x"] + box["width"] / 2 - width / 2) < 2
        screenshot = tmp_path / f"oidc-login-{width}.png"
        page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
        with page.expect_request("**/oidc/login/weblate/") as sent:
            button.click()
        assert sent.value.method == "POST"
        data = parse_qs(sent.value.post_data or "")
        assert data["next"] == ["/user/"]
        assert data["csrfmiddlewaretoken"][0]
    finally:
        context.close()
