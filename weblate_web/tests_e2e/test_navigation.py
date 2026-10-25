"""
End-to-end tests for basic website navigation and user flows.

Tests cover:
- New user visiting the website
- Navigating through key pages
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]


class TestWebsiteNavigation:
    """Test suite for basic website navigation."""

    def test_new_user_visits_homepage(self, page: Page, live_server):
        """Test that a new user can visit the homepage and see key content."""
        # Visit the homepage
        response = page.goto(live_server.url)

        # Check that the page loaded successfully without errors
        assert response is not None
        assert response.ok, f"Homepage returned status {response.status}"

        # Check that the page loaded successfully
        assert page.title()

        # Verify no server error is displayed
        # Check for common error indicators, but be specific to avoid false positives
        assert not page.locator("text=Server Error").is_visible()
        assert not page.locator("text=Internal Server Error").is_visible()

        # Check for key elements on the homepage
        # The Weblate logo or heading should be visible
        assert page.is_visible("text=Weblate") or page.locator("h1").is_visible()

    def test_navigation_to_hosting_page(self, page: Page, live_server):
        """Test navigation to the hosting page."""
        response = page.goto(f"{live_server.url}/en/hosting/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Hosting page returned status {response.status}"

        # Verify we're on the hosting page
        assert "hosting" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_features_page(self, page: Page, live_server):
        """Test navigation to the features page."""
        response = page.goto(f"{live_server.url}/en/features/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Features page returned status {response.status}"

        # Verify we're on the features page
        assert "features" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_support_page(self, page: Page, live_server):
        """Test navigation to the support page."""
        # Navigate to support page directly
        response = page.goto(f"{live_server.url}/en/support/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Support page returned status {response.status}"

        # Verify we're on the support page
        assert "support" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()

    def test_navigation_to_donate_page(self, page: Page, live_server):
        """Test navigation to the donate page."""
        # Navigate to donate page directly
        response = page.goto(f"{live_server.url}/en/donate/")

        # Check response is successful
        assert response is not None
        assert response.ok, f"Donate page returned status {response.status}"

        # Verify we're on the donate page
        assert "donate" in page.url.lower()

        # Verify no server error is displayed
        assert not page.locator("text=Server Error").is_visible()


@pytest.mark.parametrize("locale", ["en", "ar"])
@pytest.mark.parametrize("page_name", ["", "features/", "hosting/"])
@pytest.mark.parametrize("width", [1440, 390])
def test_localization_pages(page: Page, live_server, locale, page_name, width):
    """Check the responsive customer journey and capture stable visual evidence."""
    page.set_viewport_size({"width": width, "height": 900})
    response = page.goto(f"{live_server.url}/{locale}/{page_name}")
    assert response is not None and response.ok
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    assert page.locator("h1").count() == 1
    assert page.locator("html").get_attribute("dir") == (
        "rtl" if locale == "ar" else "ltr"
    )
    # Include hosting in both writing directions for the setup guide.
    if locale == "en" or page_name == "hosting/":
        screenshot_dir = Path("test-results")
        screenshot_dir.mkdir(exist_ok=True)
        name = page_name.rstrip("/") or "home"
        page.screenshot(
            path=str(screenshot_dir / f"marketing-{name}-{locale}-{width}.png"),
            full_page=True,
        )
        if page_name == "hosting/":
            page.locator("#setup-guide").screenshot(
                path=str(screenshot_dir / f"hosting-guide-{locale}-{width}.png"),
            )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        page.evaluate(
            """() => Array.from(document.querySelectorAll('body *'))
        .filter(element => !element.closest('.pricing-table-right'))
        .map(element => ({tag: element.tagName, class: element.className,
          left: element.getBoundingClientRect().left,
          right: element.getBoundingClientRect().right,
          width: element.getBoundingClientRect().width}))
        .filter(element => element.width > 0 &&
          (element.right > window.innerWidth || element.left < 0))
        .slice(0, 10)"""
        )
    )
    if not page_name:
        page.locator(".features .f-box a").first.click()
        page.wait_for_url(f"**/{locale}/features/#development")
        assert page.locator("#development").is_visible()
    services = page.locator(f'section a.button[href="/{locale}/hosting/"]').first
    services.click()
    page.wait_for_url(f"**/{locale}/hosting/")


@pytest.mark.parametrize("locale", ["en", "ar"])
@pytest.mark.parametrize("width", [1440, 1024, 768, 390])
def test_hosting_setup_choices(page: Page, live_server, locale, width):
    """Follow setup recommendations without losing pricing functionality."""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_server.url}/{locale}/hosting/")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    guide = page.locator("#setup-guide")
    page.locator("#calculator-strings").fill("1000")
    page.locator("#calculator-languages").fill("2")
    guide.locator("a.dedicated-enable").focus()
    page.keyboard.press("Enter")
    assert page.locator("#dedicated-checkbox").is_checked()
    assert page.locator("#calculator-dedicated").is_checked()
    assert page.locator("#calculator-buy").is_visible()
    assert page.locator("#calculator-trial").is_hidden()
    expect(page.locator("#calculator-buy-plan")).to_have_value("dedicated:160k")
    assert page.locator("#dedicated").is_visible()
    dedicated_form = page.locator("#dedicated form").first
    expect(dedicated_form).to_have_attribute("method", "post")
    expect(dedicated_form).to_have_attribute(
        "action", f"/{locale}/subscription/new/?plan=dedicated:160k"
    )
    expect(dedicated_form.locator('input[name="plan"]')).to_have_value("dedicated:160k")
    expect(dedicated_form.locator('input[type="submit"]')).to_be_visible()
    guide.locator("a.dedicated-toggle:not(.dedicated-enable)").click()
    assert not page.locator("#dedicated-checkbox").is_checked()
    assert not page.locator("#calculator-dedicated").is_checked()
    assert page.locator("#calculator-buy").is_hidden()
    assert page.locator("#yearly").is_visible()

    assert page.locator("#calculator-plan").is_visible()
    assert page.locator("#calculator-trial").is_visible()
    page.locator("#calculator-dedicated").check()
    assert page.locator("#calculator-buy").is_visible()
    buy_form = page.locator("#calculator-buy").locator("..")
    expect(buy_form).to_have_attribute("method", "post")
    expect(buy_form).to_have_attribute(
        "action", f"{live_server.url}/{locale}/subscription/new/?plan=dedicated%3A160k"
    )
    expect(buy_form.locator('input[name="plan"]')).to_have_value("dedicated:160k")

    guide.locator('a[href="#libre"]').click()
    assert page.url.endswith("#libre")
    assert page.locator("#libre").is_visible()
    guide.locator(f'.f-box a[href="/{locale}/support/"]').click()
    page.wait_for_url(f"**/{locale}/support/")
    support_form = page.locator(
        f'form[action="/{locale}/subscription/new/?plan=extended"]'
    )
    expect(support_form).to_have_attribute("method", "post")
    expect(support_form.locator('input[name="plan"]')).to_have_value("extended")
    expect(support_form.locator('input[type="submit"]')).to_be_visible()


@pytest.mark.browser_context_args(java_script_enabled=False)
@pytest.mark.parametrize("locale", ["en", "ar"])
def test_hosting_setup_without_javascript(page: Page, live_server, locale):
    """Recommendations and free self-hosting remain available without scripts."""
    response = page.goto(f"{live_server.url}/{locale}/hosting/")
    assert response is not None and response.ok
    guide = page.locator("#setup-guide")
    assert guide.is_visible()
    assert guide.locator("h3").count() == 3
    assert guide.locator(".setup-choices a.button").count() == 3
    support = guide.locator("#hosting-support")
    assert support.is_visible()
    assert support.locator(f'a.button[href="/{locale}/support/"]').is_visible()
    assert guide.locator('.page-desc a[href="#libre"]').is_visible()
    assert guide.locator(f'.f-box a[href="/{locale}/support/"]').is_visible()
    assert guide.locator(
        'a[href="https://docs.weblate.org/en/latest/admin/install.html"]'
    ).is_visible()
    assert page.locator(f'a[href="/{locale}/download/"]').count()
    guide.locator('a[href="#hosted"]').first.click()
    assert page.url.endswith("#hosted")
    assert page.locator("#hosted").is_visible()


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_existing_project_evaluation(page: Page, live_server, locale):
    """Reach migration guidance and deployment choices from the pricing page."""
    page.goto(f"{live_server.url}/{locale}/hosting/")
    page.locator(f'a[href="/{locale}/features/#migration"]').first.click()
    page.wait_for_url(f"**/{locale}/features/#migration")
    migration = page.locator("#migration")
    assert migration.is_visible()
    assert migration.locator(
        'a[href="https://docs.weblate.org/en/latest/devel/migration.html"]'
    ).is_visible()
    assert migration.locator(
        'a[href^="mailto:"][href$="Weblate%20migration"]'
    ).is_visible()
    assert page.locator(
        '#open-source a[href="https://docs.weblate.org/en/latest/admin/backup.html"]'
    ).is_visible()
    assert page.locator('section a[href="https://hosted.weblate.org/trial/"]').count()
    page.locator(f'#open-source a[href="/{locale}/download/"]').click()
    page.wait_for_url(f"**/{locale}/download/")
    assert page.locator(
        'a[href="https://docs.weblate.org/en/latest/admin/install.html"]'
    ).count()
