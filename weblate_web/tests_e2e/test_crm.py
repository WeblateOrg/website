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

"""End-to-end visual coverage for the internal CRM."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from weblate_web.crm.models import CRM_STORAGE, Interaction
from weblate_web.invoices.models import (
    Currency,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
    QuoteStatus,
)
from weblate_web.models import Package, Service
from weblate_web.payments.models import Customer, CustomerFollowUp, Payment
from weblate_web.saml import get_default_saml_provider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]

SCREENSHOT_DIR = Path("test-results")
CRM_PASSWORD = "testpassword123"  # ruff:ignore[hardcoded-password-string]
FIXED_TIMESTAMP = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class CrmData:
    staff: User
    customer: Customer
    prospect: Customer
    merge_source: Customer
    merge_target: Customer
    service: Service
    quote: Invoice
    refund_invoice: Invoice
    attachment_interaction: Interaction
    email_interaction: Interaction
    new_subscription_package: Package


@dataclass(frozen=True)
class ServiceFixture:
    package_name: str
    title: str
    expires: timedelta
    maintenance_window: str = ""
    url: str = "https://crm-service.example.test/"


@dataclass(frozen=True)
class InvoiceFixture:
    kind: InvoiceKind
    category: InvoiceCategory
    amount: Decimal
    issue_date: date
    description: str


def clear_storage_location_cache() -> None:
    """Clear cached paths after moving the CRM attachment storage."""
    for cached_name in ("base_location", "location"):
        CRM_STORAGE.__dict__.pop(cached_name, None)


def set_crm_storage_location(location: Path | str | None) -> None:
    """Point CRM attachment storage at a test-owned directory."""
    CRM_STORAGE.__dict__["_location"] = None if location is None else str(location)
    clear_storage_location_cache()


def create_customer(
    name: str,
    email: str,
    *,
    end_client: str = "",
    city: str = "Example City",
) -> Customer:
    """Create a customer without EU VAT validation side effects."""
    return Customer.objects.create(
        user_id=-1,
        name=name,
        end_client=end_client,
        address="Example street 42",
        city=city,
        postcode="424242",
        country="US",
        email=email,
    )


def create_payment(customer: Customer, package: Package) -> Payment:
    """Create a processed payment suitable for a subscription."""
    payment = Payment.objects.create(
        amount=package.price,
        customer=customer,
        description=package.verbose,
        recurring=package.get_repeat(),
        state=Payment.PROCESSED,
    )
    Payment.objects.filter(pk=payment.pk).update(created=FIXED_TIMESTAMP)
    payment.created = FIXED_TIMESTAMP
    return payment


def create_service(customer: Customer, data: ServiceFixture) -> Service:
    """Create a service with a subscription."""
    package = Package.objects.get(name=data.package_name)
    service = Service.objects.create(
        customer=customer,
        maintenance_window=data.maintenance_window,
        site_title=data.title,
        site_url=data.url,
        site_version="5.0",
        site_users=42,
        site_projects=7,
        backup_repository="ssh://backup.example.test/repository.git",
        backup_directory="/srv/weblate",
        backup_size=1_048_576,
    )
    service.subscription_set.create(
        package=package,
        expires=timezone.now() + data.expires,
        payment=create_payment(customer, package),
    )
    return service


def create_invoice(customer: Customer, data: InvoiceFixture) -> Invoice:
    """Create an invoice row and one line item without rendering a PDF."""
    invoice = Invoice.objects.create(
        kind=data.kind,
        category=data.category,
        customer=customer,
        issue_date=data.issue_date,
        currency=Currency.EUR,
    )
    invoice.invoiceitem_set.create(description=data.description, unit_price=data.amount)
    return invoice


@contextmanager
def fixed_interaction_timestamp(timestamp: datetime) -> Iterator[None]:
    """Pin action-created interaction timestamps before visual captures."""
    field = Interaction._meta.get_field("timestamp")  # pylint: disable=protected-access
    original_default = field.default
    original_cached_default = field.__dict__.pop("_get_default", None)
    field.default = lambda: timestamp
    try:
        yield
    finally:
        field.default = original_default
        field.__dict__.pop("_get_default", None)
        if original_cached_default is not None:
            field.__dict__["_get_default"] = original_cached_default


def test_fixed_interaction_timestamp_rebuilds_cached_default(  # pylint: disable=redefined-outer-name
    crm_data: CrmData,
) -> None:
    """Ensure visual-test interactions use the pinned timestamp."""
    field = Interaction._meta.get_field("timestamp")  # pylint: disable=protected-access
    field.get_default()

    timestamp = FIXED_TIMESTAMP + timedelta(minutes=10)
    with fixed_interaction_timestamp(timestamp):
        interaction = crm_data.customer.interaction_set.create(
            origin=Interaction.Origin.MANUAL_NOTE,
            summary="Manual CRM note",
            content="Manual CRM note",
            user=crm_data.staff,
        )

    assert interaction.timestamp == timestamp
    assert Interaction.objects.get(pk=interaction.pk).timestamp == timestamp


@pytest.fixture
def crm_staff_user(db) -> User:
    """Create a superuser for CRM browser tests."""
    return User.objects.create_superuser(
        username="crmadmin",
        email="crm-admin@example.test",
        password=CRM_PASSWORD,
    )


@pytest.fixture
def crm_data(  # pylint: disable=redefined-outer-name
    e2e_setup, crm_staff_user: User, tmp_path
) -> Iterator[CrmData]:
    """Create representative CRM data for list, detail, and action views."""
    original_storage_location = cast(
        "str | None", getattr(CRM_STORAGE, "_location", None)
    )
    set_crm_storage_location(tmp_path / "crm")

    try:
        customer = create_customer(
            "CRM Primary Customer",
            "billing-primary@example.test",
            end_client="End Client One",
        )
        prospect = create_customer("CRM Prospect", "prospect@example.test")
        merge_source = create_customer("CRM Merge Source", "merge-source@example.test")
        merge_target = create_customer("CRM Merge Target", "merge-target@example.test")

        service = create_service(
            customer,
            ServiceFixture(
                "extended",
                "Primary Extended Support",
                timedelta(days=90),
                maintenance_window="Sundays 02:00 UTC",
            ),
        )
        create_service(
            customer,
            ServiceFixture(
                "dedicated:160k",
                "Dedicated Hosting Customer",
                timedelta(days=120),
                url="https://dedicated.example.test/",
            ),
        )
        create_service(
            customer,
            ServiceFixture(
                "premium",
                "Premium Support Customer",
                timedelta(days=75),
                url="https://premium.example.test/",
            ),
        )
        create_service(
            customer,
            ServiceFixture(
                "basic",
                "Expired Support Customer",
                timedelta(days=-30),
                url="https://expired.example.test/",
            ),
        )

        quote = create_invoice(
            customer,
            InvoiceFixture(
                InvoiceKind.QUOTE,
                InvoiceCategory.SUPPORT,
                Decimal(420),
                date(2026, 1, 15),
                "CRM quote for support renewal",
            ),
        )
        create_invoice(
            customer,
            InvoiceFixture(
                InvoiceKind.INVOICE,
                InvoiceCategory.HOSTING,
                Decimal(1200),
                date(2026, 1, 15),
                "CRM hosted service invoice",
            ),
        )
        create_invoice(
            customer,
            InvoiceFixture(
                InvoiceKind.INVOICE,
                InvoiceCategory.DEVEL,
                Decimal(900),
                date(2026, 2, 15),
                "CRM consultation invoice",
            ),
        )
        create_invoice(
            customer,
            InvoiceFixture(
                InvoiceKind.INVOICE,
                InvoiceCategory.DONATE,
                Decimal(150),
                date(2026, 3, 15),
                "CRM donation invoice",
            ),
        )
        refund_invoice = create_invoice(
            customer,
            InvoiceFixture(
                InvoiceKind.INVOICE,
                InvoiceCategory.SUPPORT,
                Decimal(-75),
                date(2026, 3, 20),
                "CRM refund invoice",
            ),
        )

        attachment_interaction = customer.interaction_set.create(
            origin=Interaction.Origin.ZAMMAD_ATTACHMENT,
            summary="contract.pdf",
            content="contract.pdf",
            remote_id=500,
            details={
                "ticket_id": 10,
                "article_id": 100,
                "attachment_id": 500,
                "filename": "contract.pdf",
            },
            timestamp=FIXED_TIMESTAMP,
            user=crm_staff_user,
        )
        attachment_interaction.attachment.save(
            "contract.pdf", ContentFile(b"PDF content")
        )
        email_interaction = customer.interaction_set.create(
            origin=Interaction.Origin.EMAIL,
            summary="Payment completed",
            content="<strong>HTML body</strong>",
            details={
                "subject": "Payment completed",
                "notification": "payment_completed",
                "recipients": ["billing-primary@example.test"],
            },
            timestamp=FIXED_TIMESTAMP,
            user=crm_staff_user,
        )

        yield CrmData(
            staff=crm_staff_user,
            customer=customer,
            prospect=prospect,
            merge_source=merge_source,
            merge_target=merge_target,
            service=service,
            quote=quote,
            refund_invoice=refund_invoice,
            attachment_interaction=attachment_interaction,
            email_interaction=email_interaction,
            new_subscription_package=Package.objects.get(name="backup"),
        )
    finally:
        set_crm_storage_location(original_storage_location)


def assert_no_server_error(page: Page) -> None:
    """Assert the page does not show a Django server error."""
    assert not page.locator("text=Server Error").is_visible()
    assert not page.locator("text=Internal Server Error").is_visible()


def assert_loaded(page: Page, response, description: str) -> None:
    """Assert a navigation reached a successful non-error page."""
    assert response is not None, f"{description} did not return a response"
    assert response.ok, f"{description} returned status {response.status}"
    assert_no_server_error(page)


def absolute_url(live_server, path: str) -> str:
    """Build an absolute live-server URL from a Django path."""
    return f"{live_server.url}{path}"


def capture(page: Page, name: str) -> None:
    """Capture a full-page screenshot for Argos CI."""
    page.wait_for_load_state("networkidle")
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(
        path=(SCREENSHOT_DIR / f"crm-{name}.png").as_posix(), full_page=True
    )


def assert_text_visible(page: Page, text: str, *, exact: bool = True) -> None:
    """Assert at least one matching text node is visible."""
    assert page.get_by_text(text, exact=exact).first.is_visible()


def assert_customer_tab_selected(page: Page, name: str) -> None:
    """Assert a customer detail tab is selected in the tab navigation."""
    tab = page.get_by_label("Customer sections").get_by_role(
        "link", name=name, exact=True
    )
    assert tab.get_attribute("aria-current") == "page"
    assert "is-active" in (tab.get_attribute("class") or "")


def assert_no_horizontal_overflow(page: Page) -> None:
    """Assert the document fits the current viewport horizontally."""
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= "
        "document.documentElement.clientWidth + 1"
    )


def assert_income_charts_fit(page: Page) -> None:
    """Assert generated income charts scale inside their panels."""
    charts = page.locator(".crm-chart")
    assert charts.count() == 2
    for index in range(charts.count()):
        assert charts.nth(index).evaluate(
            """element => {
                const svg = element.querySelector("svg");
                if (!svg) {
                    return false;
                }
                const chartRect = element.getBoundingClientRect();
                const svgRect = svg.getBoundingClientRect();
                return svgRect.width > 0
                    && svgRect.width <= chartRect.width + 1
                    && element.scrollWidth <= element.clientWidth + 1;
            }"""
        )


def assert_section_table_does_not_scroll(page: Page, heading: str) -> None:
    """Assert a section table is not wider than its wrapping panel."""
    section = page.locator(
        "section.crm-section",
        has=page.get_by_role("heading", name=heading, exact=True),
    )
    assert section.locator(".crm-table-wrap").evaluate(
        "element => element.scrollWidth <= element.clientWidth + 1"
    )


def assert_summary_chips_same_height(page: Page) -> None:
    """Assert invoice summary chips share the same visual height."""
    assert page.locator(".crm-summary-line > span").evaluate_all(
        """elements => {
            const heights = elements.map(
                element => Math.round(element.getBoundingClientRect().height)
            );
            return Math.max(...heights) - Math.min(...heights) <= 1;
        }"""
    )


def log_in(page: Page, live_server, user: User) -> None:
    """Log in through Django admin using the CRM staff user."""
    response = page.goto(f"{live_server.url}/admin/login/")
    assert_loaded(page, response, "Admin login page")
    page.fill('input[name="username"]', user.username)
    page.fill('input[name="password"]', CRM_PASSWORD)
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)


def create_work_queue_visual_data(data: CrmData) -> Invoice:
    """Create records that exercise every Today queue section."""
    CustomerFollowUp.objects.create(
        customer=data.customer,
        follow_up_at=FIXED_TIMESTAMP - timedelta(hours=4),
        note="Call about renewal quote.",
        type=CustomerFollowUp.Type.MANUAL,
    )
    CustomerFollowUp.objects.create(
        customer=data.prospect,
        follow_up_at=FIXED_TIMESTAMP + timedelta(days=3),
        note="Send onboarding check-in.",
        type=CustomerFollowUp.Type.MANUAL,
    )

    old_invoice_customer = create_customer(
        "CRM Work Queue Invoice", "work-invoice@example.test"
    )
    create_invoice(
        old_invoice_customer,
        InvoiceFixture(
            InvoiceKind.INVOICE,
            InvoiceCategory.HOSTING,
            Decimal(640),
            date(2025, 12, 30),
            "CRM old unpaid invoice",
        ),
    )
    stale_quote_customer = create_customer(
        "CRM Work Queue Quote", "work-quote@example.test"
    )
    stale_quote = create_invoice(
        stale_quote_customer,
        InvoiceFixture(
            InvoiceKind.QUOTE,
            InvoiceCategory.SUPPORT,
            Decimal(360),
            date(2025, 12, 20),
            "CRM stale quote",
        ),
    )
    closed_quote_customer = create_customer(
        "CRM Closed Work Queue Quote", "closed-work-quote@example.test"
    )
    closed_quote = create_invoice(
        closed_quote_customer,
        InvoiceFixture(
            InvoiceKind.QUOTE,
            InvoiceCategory.SUPPORT,
            Decimal(280),
            date(2025, 12, 18),
            "CRM closed stale quote",
        ),
    )
    closed_quote.quote_status = QuoteStatus.LOST
    closed_quote.quote_status_note = "Customer rejected the offer."
    closed_quote.save(update_fields=["quote_status", "quote_status_note"])
    expired_service_customer = create_customer(
        "CRM Work Queue Service", "work-service@example.test"
    )
    create_service(
        expired_service_customer,
        ServiceFixture(
            "basic",
            "Queue Expired Support",
            timedelta(days=-20),
            url="https://queue-expired.example.test/",
        ),
    )
    return stale_quote


def assert_work_queue_items(page: Page) -> None:
    """Assert the full queue shows actionable work and hides closed quotes."""
    for heading in ("Follow-ups", "Billing", "Services"):
        assert page.get_by_role("heading", name=heading, exact=True).is_visible()
    for text in (
        "Manual follow-up",
        "Call about renewal quote.",
        "Upcoming follow-up",
        "Send onboarding check-in.",
        "Unpaid invoice",
        "CRM Work Queue Invoice",
        "Stale quote",
        "CRM Work Queue Quote",
        "Expired service",
        "Queue Expired Support",
    ):
        assert_text_visible(page, text, exact=False)
    assert page.get_by_text("CRM Closed Work Queue Quote").count() == 0


def capture_quote_status_actions(page: Page, live_server, stale_quote: Invoice) -> None:
    """Close and reopen a quote while capturing the CRM quote status UI."""
    response = page.goto(
        absolute_url(
            live_server,
            reverse("crm:invoice-detail", kwargs={"pk": stale_quote.pk}),
        )
    )
    assert_loaded(page, response, "Quote detail with status controls")
    assert page.get_by_role("heading", name="Quote status").is_visible()
    assert page.locator('select[name="quote_status"]').is_visible()
    capture(page, "quote-status-open")

    page.select_option('select[name="quote_status"]', str(int(QuoteStatus.SUPERSEDED)))
    page.fill(
        'textarea[name="quote_status_note"]',
        "Customer accepted an alternative quote.",
    )
    page.click('input[name="close_quote"]')
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)
    assert_text_visible(page, "Superseded", exact=False)
    assert_text_visible(page, "Customer accepted an alternative quote.")
    assert page.locator('input[name="reopen_quote"]').is_visible()
    capture(page, "quote-status-closed")

    page.click('input[name="reopen_quote"]')
    page.wait_for_load_state("networkidle")
    assert_no_server_error(page)
    assert_text_visible(page, "Open", exact=False)
    assert page.locator('input[name="close_quote"]').is_visible()
    capture(page, "quote-status-reopened")


class TestCrmVisualCoverage:  # pylint: disable=redefined-outer-name
    """Browser coverage for existing CRM screens and workflows."""

    def test_dashboard_lists_and_income_reports(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Visit CRM dashboard, list filters, and income reporting screens."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(absolute_url(live_server, reverse("crm:index")))
        assert_loaded(page, response, "CRM dashboard")
        assert page.get_by_role("link", name="Customers", exact=True).is_visible()
        assert (
            page.locator(".crm-brand span").evaluate(
                "element => getComputedStyle(element).color"
            )
            == "rgb(255, 255, 255)"
        )
        capture(page, "dashboard")

        page.locator(".crm-dashboard-item").filter(has_text="Active customers").click()
        page.wait_for_load_state("networkidle")
        assert page.url == absolute_url(
            live_server, reverse("crm:customer-list", kwargs={"kind": "active"})
        )

        for kind in ("all", "active"):
            response = page.goto(
                absolute_url(
                    live_server, reverse("crm:customer-list", kwargs={"kind": kind})
                )
            )
            assert_loaded(page, response, f"Customer list {kind}")
            assert_text_visible(page, "CRM Primary Customer")
            capture(page, f"customers-{kind}")

        response = page.goto(
            absolute_url(
                live_server,
                f"{reverse('crm:customer-list', kwargs={'kind': 'all'})}?q=prospect",
            )
        )
        assert_loaded(page, response, "Customer search")
        assert_text_visible(page, "CRM Prospect")
        capture(page, "customers-search")

        for kind in ("all", "expired", "extended", "dedicated", "premium"):
            response = page.goto(
                absolute_url(
                    live_server, reverse("crm:service-list", kwargs={"kind": kind})
                )
            )
            assert_loaded(page, response, f"Service list {kind}")
            capture(page, f"services-{kind}")

        page.get_by_label("CRM navigation").get_by_role("link", name="Invoices").click()
        page.wait_for_load_state("networkidle")
        assert page.url == absolute_url(
            live_server, reverse("crm:invoice-list", kwargs={"kind": "invoice"})
        )

        for kind in ("all", "invoice", "quote", "unpaid"):
            response = page.goto(
                absolute_url(
                    live_server, reverse("crm:invoice-list", kwargs={"kind": kind})
                )
            )
            assert_loaded(page, response, f"Invoice list {kind}")
            capture(page, f"invoices-{kind}")

        response = page.goto(absolute_url(live_server, reverse("crm:income")))
        assert_loaded(page, response, "Income yearly report")
        assert page.get_by_role("heading", name="Total Income - 2026").is_visible()
        capture(page, "income-year")

        response = page.goto(
            absolute_url(
                live_server,
                reverse("crm:income-month", kwargs={"year": 2026, "month": 3}),
            )
        )
        assert_loaded(page, response, "Income monthly report")
        assert page.get_by_role("heading", name="Daily Income").is_visible()
        capture(page, "income-month")

    def test_work_queue_dashboard_and_full_page(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Capture the CRM Today queue on dashboard, desktop, and mobile."""
        stale_quote = create_work_queue_visual_data(crm_data)
        log_in(page, live_server, crm_data.staff)
        response = page.goto(absolute_url(live_server, reverse("crm:index")))
        assert_loaded(page, response, "CRM dashboard with Today queue")
        today_section = page.locator(
            "section.crm-section",
            has=page.get_by_role("heading", name="Today", exact=True),
        )
        assert today_section.get_by_text("Manual follow-up").is_visible()
        assert today_section.get_by_text("Unpaid invoice").is_visible()
        assert today_section.get_by_text("Expired service").first.is_visible()
        capture(page, "work-queue-dashboard")

        today_section.locator(f'a[href="{reverse("crm:work-queue")}"]').click()
        page.wait_for_load_state("networkidle")
        assert page.url == absolute_url(live_server, reverse("crm:work-queue"))
        assert_work_queue_items(page)
        capture(page, "work-queue")

        capture_quote_status_actions(page, live_server, stale_quote)
        page.set_viewport_size({"width": 390, "height": 900})
        response = page.goto(absolute_url(live_server, reverse("crm:work-queue")))
        assert_loaded(page, response, "Mobile CRM Today queue")
        assert_no_horizontal_overflow(page)
        assert page.get_by_role("heading", name="Today", exact=True).is_visible()
        capture(page, "work-queue-mobile")

    def test_mobile_navigation_menu(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Use the compact CRM navigation on a narrow viewport."""
        page.set_viewport_size({"width": 390, "height": 800})
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(
                live_server, reverse("crm:service-list", kwargs={"kind": "all"})
            )
        )
        assert_loaded(page, response, "Mobile service list")
        assert (
            page.locator("#crm-menu-toggle").evaluate("element => element.tabIndex")
            == 0
        )
        page.get_by_text("Menu", exact=True).click()
        page.get_by_label("CRM navigation").get_by_role("link", name="Invoices").click()
        page.wait_for_load_state("networkidle")
        assert page.url == absolute_url(
            live_server, reverse("crm:invoice-list", kwargs={"kind": "invoice"})
        )

    def test_mobile_search_layout(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Keep the CRM listing search compact on a narrow viewport."""
        page.set_viewport_size({"width": 390, "height": 800})
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(
                live_server, reverse("crm:customer-list", kwargs={"kind": "all"})
            )
        )
        assert_loaded(page, response, "Mobile customer list")
        assert page.locator(".crm-search .crm-field--inline").evaluate(
            "element => element.getBoundingClientRect().height < 100"
        )
        capture(page, "customers-search-mobile")

    def test_income_reports_mobile_layout(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Check income charts and compact tables on a narrow viewport."""
        page.set_viewport_size({"width": 390, "height": 900})
        log_in(page, live_server, crm_data.staff)

        response = page.goto(absolute_url(live_server, reverse("crm:income")))
        assert_loaded(page, response, "Mobile income yearly report")
        assert_no_horizontal_overflow(page)
        assert_income_charts_fit(page)
        assert_section_table_does_not_scroll(page, "Yearly category income")
        capture(page, "income-year-mobile")

        response = page.goto(
            absolute_url(
                live_server,
                reverse("crm:income-month", kwargs={"year": 2026, "month": 3}),
            )
        )
        assert_loaded(page, response, "Mobile income monthly report")
        assert_no_horizontal_overflow(page)
        assert_income_charts_fit(page)
        assert_section_table_does_not_scroll(page, "Category income")
        capture(page, "income-month-mobile")

    def test_customer_detail_tabs(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Visit every customer detail tab through its direct URL."""
        log_in(page, live_server, crm_data.staff)
        customer_url = absolute_url(live_server, crm_data.customer.get_absolute_url())

        response = page.goto(customer_url)
        assert_loaded(page, response, "Customer overview tab")
        assert (
            page.locator("#crm-menu-toggle").evaluate("element => element.tabIndex")
            == -1
        )
        assert_customer_tab_selected(page, "Overview")
        for heading in (
            "Customer",
            "Actions",
            "Customer services",
            "Agreements",
            "Donations",
        ):
            assert page.get_by_role("heading", name=heading, exact=True).is_visible()
        assert (
            page.get_by_role("heading", name="Invoice history", exact=True).count() == 0
        )
        assert (
            page.get_by_role("heading", name="Interaction history", exact=True).count()
            == 0
        )
        assert (
            page.get_by_role("heading", name="Payment history", exact=True).count() == 0
        )
        capture(page, "customer-tab-overview")

        response = page.goto(f"{customer_url}?tab=interactions")
        assert_loaded(page, response, "Customer interactions tab")
        assert_customer_tab_selected(page, "Interactions")
        assert page.get_by_role(
            "heading", name="Interaction history", exact=True
        ).first.is_visible()
        assert_text_visible(page, "Payment completed")
        assert_text_visible(page, "contract.pdf")
        assert (
            page.get_by_role("heading", name="Customer services", exact=True).count()
            == 0
        )
        assert (
            page.get_by_role("heading", name="Invoice history", exact=True).count() == 0
        )
        assert (
            page.get_by_role("heading", name="Payment history", exact=True).count() == 0
        )
        capture(page, "customer-tab-interactions")

        response = page.goto(f"{customer_url}?tab=invoices")
        assert_loaded(page, response, "Customer invoices tab")
        assert_customer_tab_selected(page, "Invoices")
        assert page.get_by_role(
            "heading", name="Invoice history", exact=True
        ).is_visible()
        assert_text_visible(page, "CRM hosted service invoice")
        assert_text_visible(page, "Issue new invoice")
        assert (
            page.get_by_role("heading", name="Customer services", exact=True).count()
            == 0
        )
        assert (
            page.get_by_role("heading", name="Interaction history", exact=True).count()
            == 0
        )
        assert (
            page.get_by_role("heading", name="Payment history", exact=True).count() == 0
        )
        capture(page, "customer-tab-invoices")

        response = page.goto(f"{customer_url}?tab=payments")
        assert_loaded(page, response, "Customer payments tab")
        assert_customer_tab_selected(page, "Payments")
        assert page.get_by_role(
            "heading", name="Payment history", exact=True
        ).is_visible()
        assert_text_visible(page, "Weblate extended self-hosted support (yearly)")
        assert (
            page.get_by_role("heading", name="Customer services", exact=True).count()
            == 0
        )
        assert (
            page.get_by_role("heading", name="Invoice history", exact=True).count() == 0
        )
        assert (
            page.get_by_role("heading", name="Interaction history", exact=True).count()
            == 0
        )
        capture(page, "customer-tab-payments")

        response = page.goto(f"{customer_url}?tab=unknown")
        assert_loaded(page, response, "Customer invalid tab fallback")
        assert_customer_tab_selected(page, "Overview")
        assert page.get_by_role(
            "heading", name="Customer services", exact=True
        ).first.is_visible()
        capture(page, "customer-tab-invalid")

    def test_customer_detail_actions(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Exercise customer manual note and hosted-user linking actions."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(live_server, crm_data.customer.get_absolute_url())
        )
        assert_loaded(page, response, "Customer detail")
        capture(page, "customer-detail")

        merge_height = page.locator('input[name="merge"]').evaluate(
            "element => element.getBoundingClientRect().height"
        )
        email_height = page.locator('input[name="email"]').evaluate(
            "element => element.getBoundingClientRect().height"
        )
        assert round(merge_height) == round(email_height)

        page.fill('textarea[name="note"]', "Manual CRM note\nFollow-up requested.")
        with fixed_interaction_timestamp(FIXED_TIMESTAMP + timedelta(minutes=10)):
            page.click('input[name="add_manual_note"]')
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert "tab=interactions" in page.url
        assert_text_visible(page, "Manual CRM note", exact=False)
        capture(page, "customer-manual-note")
        page.get_by_role("link", name="Overview").click()
        page.wait_for_load_state("networkidle")

        hosted_payload = {
            "provider": get_default_saml_provider(),
            "external_id": "crm-linked-user",
            "profile": {
                "username": "crm-linked-user",
                "email": "crm-linked@example.test",
                "last_name": "Linked User",
            },
        }
        with (
            fixed_interaction_timestamp(FIXED_TIMESTAMP + timedelta(minutes=20)),
            patch(
                "weblate_web.crm.views.ensure_hosted_user",
                return_value=(hosted_payload, True),
            ),
        ):
            page.fill('input[name="email"]', "crm-linked@example.test")
            page.fill('input[name="full_name"]', "CRM Linked User")
            page.click('input[name="add_customer_user"]')
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "crm-linked@example.test", exact=False)
        capture(page, "customer-add-user")

    def test_interaction_detail_and_download(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Visit interaction detail pages and download an attachment."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(
                live_server,
                reverse(
                    "crm:interaction-detail",
                    kwargs={"pk": crm_data.attachment_interaction.pk},
                ),
            )
        )
        assert_loaded(page, response, "Attachment interaction detail")
        assert_text_visible(page, "Ticket ID")
        capture(page, "interaction-attachment-detail")

        with page.expect_download() as download_info:
            page.locator(
                f'a[href="{crm_data.attachment_interaction.attachment_download_url}"]'
            ).click()
        assert download_info.value.suggested_filename == "contract.pdf"

        response = page.goto(
            absolute_url(
                live_server,
                reverse(
                    "crm:interaction-detail",
                    kwargs={"pk": crm_data.email_interaction.pk},
                ),
            )
        )
        assert_loaded(page, response, "Email interaction detail")
        assert_text_visible(page, "payment_completed")
        capture(page, "interaction-email-detail")

    def test_customer_subscription_invoice_and_merge(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Create a new customer subscription invoice and merge customers."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(live_server, crm_data.prospect.get_absolute_url())
        )
        assert_loaded(page, response, "Prospect detail")
        assert_text_visible(page, "New service purchase")
        page.select_option(
            'select[name="package"]', str(crm_data.new_subscription_package.pk)
        )
        page.fill('input[name="customer_reference"]', "PO-PROSPECT-001")
        page.fill('textarea[name="customer_note"]', "Prospect subscription quote.")
        with patch.object(Invoice, "generate_files"):
            page.click('input[value="Create"]')
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "PO-PROSPECT-001", exact=False)
        capture(page, "customer-new-subscription-invoice")

        response = page.goto(
            absolute_url(live_server, crm_data.merge_source.get_absolute_url())
        )
        assert_loaded(page, response, "Merge source detail")
        page.fill('input[name="merge"]', str(crm_data.merge_target.pk))
        page.click('input[value="Review merge"]')
        page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "will be merged into", exact=False)
        capture(page, "customer-merge-review")
        page.click('input[value="Merge"]')
        page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert page.url == absolute_url(
            live_server, crm_data.merge_target.get_absolute_url()
        )
        assert not Customer.objects.filter(pk=crm_data.merge_source.pk).exists()
        assert page.get_by_role("heading", name="CRM Merge Target").is_visible()
        capture(page, "customer-merge-complete")

    def test_service_detail_actions(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Exercise service maintenance, invoicing, upgrade, and disable actions."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(live_server, crm_data.service.get_absolute_url())
        )
        assert_loaded(page, response, "Service detail")
        assert page.get_by_role("heading", name="Maintenance window").is_visible()
        capture(page, "service-detail")

        page.fill('input[name="maintenance_window"]', "Mondays 03:00 UTC")
        page.click('input[name="update_maintenance_window"]')
        page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert (
            page.locator('input[name="maintenance_window"]').input_value()
            == "Mondays 03:00 UTC"
        )
        capture(page, "service-maintenance-window")

        renewal_form = page.locator(
            'form:has(input[name="action"][value="renewal"])'
        ).first
        renewal_form.locator('input[name="customer_reference"]').fill("PO-RENEW-001")
        renewal_form.locator('textarea[name="customer_note"]').fill(
            "Renewal quote from CRM."
        )
        with patch.object(Invoice, "generate_files"):
            renewal_form.locator('input[type="submit"]').click()
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "PO-RENEW-001", exact=False)
        capture(page, "service-renewal-quote")

        response = page.goto(
            absolute_url(live_server, crm_data.service.get_absolute_url())
        )
        assert_loaded(page, response, "Service detail for upgrade")
        upgrade_invoice = page.locator(
            'form:has(input[name="action"][value="upgrade"])'
        ).first
        upgrade_invoice.locator(
            f'input[name="kind"][value="{int(InvoiceKind.INVOICE)}"]'
        ).check()
        upgrade_invoice.locator('input[name="customer_reference"]').fill(
            "PO-UPGRADE-001"
        )
        upgrade_invoice.locator('textarea[name="customer_note"]').fill(
            "Upgrade invoice from CRM."
        )
        with patch.object(Invoice, "generate_files"):
            upgrade_invoice.locator('input[type="submit"]').click()
            page.locator("#crm-invoice-confirm-dialog").get_by_role(
                "button", name="Issue invoice"
            ).click()
            page.wait_for_url("**/crm/invoices/detail/*/")
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "PO-UPGRADE-001", exact=False)
        capture(page, "service-upgrade-invoice")

        response = page.goto(
            absolute_url(live_server, crm_data.service.get_absolute_url())
        )
        assert_loaded(page, response, "Service detail for disable")
        disable_form = (
            page.locator("form").filter(has=page.locator('input[name="disable"]')).first
        )
        disable_form.locator('input[type="checkbox"]').check()
        disable_form.locator('input[name="disable"]').click()
        page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "Service is terminated")
        capture(page, "service-disabled")

    def test_invoice_detail_actions(
        self, page: Page, live_server, crm_data: CrmData
    ) -> None:
        """Convert a quote to an invoice and confirm a refund."""
        log_in(page, live_server, crm_data.staff)

        response = page.goto(
            absolute_url(
                live_server,
                reverse("crm:invoice-detail", kwargs={"pk": crm_data.quote.pk}),
            )
        )
        assert_loaded(page, response, "Quote detail")
        assert page.get_by_role("heading", name="Issue invoice from quote").is_visible()
        capture(page, "quote-detail")
        quote_url = page.url
        page.fill('input[name="customer_reference"]', "PO-CONVERTED-001")
        page.fill('textarea[name="customer_note"]', "Converted quote from CRM.")
        with patch.object(Invoice, "generate_files"):
            page.click('input[name="invoice"]')
            page.locator("#crm-invoice-confirm-dialog").get_by_role(
                "button", name="Issue invoice"
            ).click()
            page.wait_for_url(lambda url: url != quote_url)
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert_text_visible(page, "Generated from", exact=False)
        capture(page, "quote-converted-invoice")

        response = page.goto(
            absolute_url(
                live_server,
                reverse(
                    "crm:invoice-detail", kwargs={"pk": crm_data.refund_invoice.pk}
                ),
            )
        )
        assert_loaded(page, response, "Refund invoice detail")
        assert page.get_by_role("heading", name="Confirm refund done").is_visible()
        assert_summary_chips_same_height(page)
        capture(page, "refund-detail")
        page.fill('input[name="description"]', "Refunded by bank transfer")
        with patch.object(Invoice, "generate_receipt"):
            page.click('input[name="confirm_refund"]')
            page.wait_for_load_state("networkidle")
        assert_no_server_error(page)
        assert (
            page.locator(".crm-summary-line .crm-badge--success")
            .filter(has_text="Paid")
            .first.is_visible()
        )
        assert page.get_by_role("heading", name="Confirm refund done").count() == 0
        capture(page, "refund-confirmed")
