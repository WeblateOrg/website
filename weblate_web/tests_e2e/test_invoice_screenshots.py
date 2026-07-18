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

"""End-to-end visual coverage for rendered invoice PDFs."""

from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from shutil import which
from typing import cast
from uuid import UUID

import pytest
from PIL import Image as PILImage

from weblate_web.invoices.models import (
    Currency,
    Discount,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
    QuantityUnit,
)
from weblate_web.payments.models import Customer, Payment

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("e2e_setup"),
]

SCREENSHOT_DIR = Path("test-results")
ISSUE_DATE = date(2026, 1, 15)
DUE_DATE = date(2026, 1, 29)
MAX_LOGO_RED = 90
MIN_LOGO_GREEN = 120
MIN_LOGO_BLUE = 90
MIN_LOGO_MARK_PIXELS = 500


@dataclass(frozen=True)
class InvoiceItemData:
    description: str
    unit_price: Decimal
    quantity: int = 1
    quantity_unit: QuantityUnit = QuantityUnit.BLANK
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class InvoiceData:
    slug: str
    kind: InvoiceKind
    country: str
    currency: Currency = Currency.EUR
    vat: str = ""
    tax: str = ""
    vat_rate: int = 0
    prepaid: bool = False
    customer_reference: str = ""
    customer_note: str = ""
    contact_point: str = ""
    accounting_reference: str = ""
    discount: Discount | None = None
    items: tuple[InvoiceItemData, ...] = ()
    receipt: bool = False


def create_customer(case: InvoiceData) -> Customer:
    """Create a customer without doing external VAT validation."""
    customer = Customer.objects.create(
        name=f"{case.slug.replace('-', ' ').title()} Customer",
        address="Example street 42",
        address_2="Billing department" if case.customer_note else "",
        city="Example City",
        postcode="424242",
        country=case.country,
        tax=case.tax,
        user_id=-1,
        email=f"{case.slug}@example.test",
        contact_point=case.contact_point,
        accounting_reference=case.accounting_reference,
    )
    if case.vat:
        Customer.objects.filter(pk=customer.pk).update(vat=case.vat)
        customer.vat = case.vat
    return customer


def create_invoice(case: InvoiceData, sequence: int) -> Invoice:
    """Create and render one invoice fixture."""
    invoice = Invoice.objects.create(
        uuid=UUID(int=sequence),
        sequence=sequence,
        issue_date=ISSUE_DATE,
        due_date=DUE_DATE,
        tax_date=ISSUE_DATE,
        kind=case.kind,
        category=InvoiceCategory.HOSTING,
        customer=create_customer(case),
        customer_reference=case.customer_reference,
        customer_note=case.customer_note,
        discount=case.discount,
        vat_rate=case.vat_rate,
        currency=case.currency,
        prepaid=case.prepaid,
    )
    for item in case.items:
        invoice.invoiceitem_set.create(
            description=item.description,
            unit_price=item.unit_price,
            quantity=item.quantity,
            quantity_unit=item.quantity_unit,
            start_date=item.start_date,
            end_date=item.end_date,
        )
    invoice.generate_files()
    if case.receipt:
        Payment.objects.create(
            uuid=UUID(int=100 + sequence),
            amount=int(invoice.total_amount),
            amount_fixed=True,
            backend="pay",
            card_info={
                "number": "515735******2654",
                "expiration_date": "2026-05",
                "brand": "MASTERCARD",
                "type": "debit",
            },
            customer=invoice.customer,
            description=invoice.get_description(),
            paid_invoice=invoice,
            state=Payment.PROCESSED,
        )
        invoice.generate_receipt()
    return invoice


def convert_pdf_to_screenshots(pdf_path: Path, screenshot_name: str) -> list[Path]:
    """Rasterize all PDF pages into Argos-consumed PNG screenshots."""
    converter = which("gs")
    assert converter is not None, "Ghostscript is required for invoice PDF screenshots"

    SCREENSHOT_DIR.mkdir(exist_ok=True)
    for screenshot in SCREENSHOT_DIR.glob(f"{screenshot_name}-*.png"):
        screenshot.unlink()

    output_prefix = SCREENSHOT_DIR / screenshot_name
    subprocess.run(
        [
            converter,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=png16m",
            "-r144",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            f"-sOutputFile={output_prefix.as_posix()}-%d.png",
            pdf_path.as_posix(),
        ],
        check=True,
    )

    screenshots = sorted(SCREENSHOT_DIR.glob(f"{screenshot_name}-*.png"))
    assert screenshots, f"No screenshots generated for {pdf_path}"
    for screenshot in screenshots:
        assert screenshot.stat().st_size > 0
    assert_invoice_logo_rendered(screenshots[0])
    return screenshots


def assert_invoice_logo_rendered(screenshot: Path) -> None:
    """Verify the colored Weblate mark is present, not only the wordmark."""
    with PILImage.open(screenshot) as image:
        header = image.crop((0, 0, min(image.width, 600), min(image.height, 240)))
        header = header.convert("RGB")
        logo_pixels = 0
        for y in range(header.height):
            for x in range(header.width):
                pixel = cast("tuple[int, int, int]", header.getpixel((x, y)))
                if is_logo_mark_pixel(pixel):
                    logo_pixels += 1
    assert logo_pixels > MIN_LOGO_MARK_PIXELS, (
        f"Invoice logo mark is missing from {screenshot}"
    )


def is_logo_mark_pixel(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red < MAX_LOGO_RED and green > MIN_LOGO_GREEN and blue > MIN_LOGO_BLUE


def test_invoice_pdf_screenshots(settings, tmp_path):
    """Generate invoice PDF screenshots for Argos CI visual comparisons."""
    settings.INVOICES_PATH = tmp_path / "invoices"
    settings.SITE_URL = "https://weblate.test"

    discount = Discount.objects.create(
        description="Open source sustainability discount", percents=15
    )
    cases = (
        InvoiceData(
            slug="invoice-vat-items",
            kind=InvoiceKind.INVOICE,
            country="CZ",
            vat="CZ8003280318",
            tax="21668027",
            vat_rate=21,
            customer_reference="PO-2026-001",
            customer_note="Please include the internal cost center.\nApproved by finance.",
            contact_point="Finance approvals",
            accounting_reference="COST-2026-OPEN-SOURCE",
            discount=discount,
            items=(
                InvoiceItemData(
                    description="Dedicated hosted Weblate service",
                    unit_price=Decimal("250.000"),
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                ),
                InvoiceItemData(
                    description="Localization workflow workshop",
                    unit_price=Decimal("120.000"),
                    quantity=4,
                    quantity_unit=QuantityUnit.HOURS,
                ),
                InvoiceItemData(
                    description="Prepaid credit",
                    unit_price=Decimal("-50.000"),
                ),
            ),
        ),
        InvoiceData(
            slug="invoice-reverse-charge",
            kind=InvoiceKind.INVOICE,
            country="DE",
            vat="DE123456789",
            items=(
                InvoiceItemData(
                    description="Self-hosted support renewal",
                    unit_price=Decimal("645.000"),
                ),
            ),
        ),
        InvoiceData(
            slug="invoice-outside-eu-prepaid",
            kind=InvoiceKind.INVOICE,
            country="US",
            currency=Currency.USD,
            prepaid=True,
            items=(
                InvoiceItemData(
                    description="Hosted Weblate backup plan",
                    unit_price=Decimal("180.000"),
                ),
                InvoiceItemData(
                    description="Migration consultation",
                    unit_price=Decimal("95.000"),
                    quantity=1,
                    quantity_unit=QuantityUnit.HOURS,
                ),
            ),
        ),
        InvoiceData(
            slug="proforma-czk-vat",
            kind=InvoiceKind.PROFORMA,
            country="CZ",
            currency=Currency.CZK,
            vat_rate=21,
            tax="21668027",
            items=(
                InvoiceItemData(
                    description="Annual hosted Weblate subscription",
                    unit_price=Decimal("12000.000"),
                ),
            ),
        ),
        InvoiceData(
            slug="quote-support",
            kind=InvoiceKind.QUOTE,
            country="FR",
            customer_reference="RFQ-26-42",
            items=(
                InvoiceItemData(
                    description="Extended self-hosted support",
                    unit_price=Decimal("1275.000"),
                ),
                InvoiceItemData(
                    description="Optional onboarding session",
                    unit_price=Decimal("150.000"),
                    quantity=2,
                    quantity_unit=QuantityUnit.HOURS,
                ),
            ),
        ),
        InvoiceData(
            slug="receipt-card-payment",
            kind=InvoiceKind.INVOICE,
            country="CZ",
            vat_rate=21,
            prepaid=True,
            receipt=True,
            items=(
                InvoiceItemData(
                    description="Community localization sponsorship",
                    unit_price=Decimal("300.000"),
                ),
            ),
        ),
    )

    for sequence, case in enumerate(cases, start=1):
        invoice = create_invoice(case, sequence)
        pdf_path = invoice.receipt_path if case.receipt else invoice.path
        convert_pdf_to_screenshots(pdf_path, case.slug)
