from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from django.test.utils import override_settings
from lxml import etree

from weblate_web.models import Package, PackageCategory
from weblate_web.payments.models import Customer
from weblate_web.tests import UserTestCase

from .models import (
    Currency,
    Discount,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
    QuantityUnit,
)

S3_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "schemas" / "money-s3" / "_Document.xsd"
)
S3_SCHEMA = etree.XMLSchema(etree.parse(S3_SCHEMA_PATH))


class InvoiceTestCase(UserTestCase):
    def create_customer(self, *, vat: str = "") -> Customer:
        return Customer.objects.create(
            name="Zkušební zákazník",
            address="Street 42",
            city="City",
            postcode="424242",
            country="cz",
            user_id=-1,
            vat=vat,
        )

    def create_invoice_base(  # noqa: PLR0913
        self,
        *,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        customer_note: str = "",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        currency: Currency = Currency.EUR,
        tax_date: date | None = None,
        due_date: date | None = None,
    ) -> Invoice:
        return Invoice.objects.create(
            customer=self.create_customer(vat=vat),
            discount=discount,
            vat_rate=vat_rate,
            kind=kind,
            customer_note=customer_note,
            category=InvoiceCategory.HOSTING,
            customer_reference=customer_reference,
            currency=currency,
            tax_date=cast("date", tax_date),
            due_date=cast("date", due_date),
        )

    def create_invoice_package(
        self,
        *,
        discount: Discount | None = None,
        currency: Currency = Currency.EUR,
    ) -> Invoice:
        invoice = self.create_invoice_base(discount=discount, currency=currency)
        package = Package.objects.create(
            name="hosting",
            verbose="Weblate hosting",
            price=100,
            category=PackageCategory.PACKAGE_DEDICATED,
        )
        invoice.invoiceitem_set.create(package=package)
        return invoice

    def create_invoice(  # noqa: PLR0913
        self,
        *,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        customer_note: str = "",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        tax_date: date | None = None,
        due_date: date | None = None,
    ) -> Invoice:
        invoice = self.create_invoice_base(
            discount=discount,
            vat_rate=vat_rate,
            customer_reference=customer_reference,
            customer_note=customer_note,
            vat=vat,
            kind=kind,
            tax_date=tax_date,
            due_date=due_date,
        )
        invoice.invoiceitem_set.create(
            description="Test item",
            unit_price=100,
        )
        return invoice

    def validate_invoice(self, invoice: Invoice) -> None:
        invoice.generate_files()
        self.assertNotEqual(str(invoice), "")
        if invoice.discount:
            self.assertNotEqual(str(invoice.discount), "")
        for item in invoice.all_items:
            self.assertNotEqual(str(item), "")

        # Validate generated XML
        xml_doc = etree.parse(invoice.xml_path)
        S3_SCHEMA.assertValid(xml_doc)

    def test_dates(self) -> None:
        invoice = self.create_invoice(vat="CZ8003280318")
        self.assertEqual(invoice.tax_date, invoice.issue_date)
        self.assertEqual(invoice.due_date, invoice.issue_date + timedelta(days=14))
        tax_date = date(2020, 10, 10)
        due_date = date(3030, 10, 10)
        invoice = self.create_invoice(
            vat="CZ8003280318", tax_date=tax_date, due_date=due_date
        )
        self.assertEqual(invoice.tax_date, tax_date)
        self.assertEqual(invoice.due_date, due_date)

    def test_total(self) -> None:
        invoice = self.create_invoice(vat="CZ8003280318")
        self.assertEqual(invoice.total_amount, 100)
        self.validate_invoice(invoice)

    def test_total_vat(self) -> None:
        invoice = self.create_invoice(vat_rate=21, customer_reference="PO123456")
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    def test_total_vat_note(self) -> None:
        invoice = self.create_invoice(
            vat_rate=21, customer_reference="PO123456", customer_note="Test note\n" * 3
        )
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    def test_total_items(self) -> None:
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item", unit_price=1000, quantity=4
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    def test_total_items_hours(self) -> None:
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=4,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    def test_total_items_hour(self) -> None:
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=1,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 1100)
        self.validate_invoice(invoice)

    def test_discount(self) -> None:
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50)
        )
        self.assertEqual(invoice.total_amount, 50)
        self.validate_invoice(invoice)

    def test_discount_negative(self) -> None:
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50)
        )
        invoice.invoiceitem_set.create(description="Prepaid amount", unit_price=-10)
        self.assertEqual(invoice.total_amount, 40)
        self.validate_invoice(invoice)

    def test_discount_vat(self) -> None:
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50),
            vat_rate=21,
        )
        self.assertEqual(invoice.total_amount, Decimal("60.50"))
        self.validate_invoice(invoice)

    def test_package(self) -> None:
        invoice = self.create_invoice_package()
        self.assertEqual(invoice.total_amount, Decimal(100))
        self.validate_invoice(invoice)

    def test_package_usd(self) -> None:
        invoice = self.create_invoice_package(currency=Currency.USD)
        self.assertEqual(
            invoice.total_amount,
            round(Decimal(100) * invoice.exchange_rate_eur * Decimal("1.1"), 0),
        )
        self.validate_invoice(invoice)

    def test_invoice_kinds(self) -> None:
        for kind in InvoiceKind.values:
            invoice = self.create_invoice(kind=InvoiceKind(kind))
            self.validate_invoice(invoice)

    @override_settings(PAYMENT_DEBUG=True)
    def test_pay_link(self) -> None:
        invoice = self.create_invoice_package()
        self.validate_invoice(invoice)
        url = cast("str", invoice.get_payment_url())
        self.assertIsNotNone(url)

        # Unauthenticated should redirect to login
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Payment Summary")
        # Unauthenticated user should see note about terms
        self.assertContains(response, "By performing the payment, you accept our")
        self.assertNotContains(response, "Billing information")
        self.assertEqual(invoice.draft_payment_set.count(), 1)

        # Repeated access should reuse existing payment
        self.login()
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Payment Summary")
        # Logged-in user should not see this
        self.assertNotContains(response, "By performing the payment, you accept our")
        self.assertNotContains(response, "Billing information")
        self.assertEqual(invoice.draft_payment_set.count(), 1)

        # Pay
        payment = invoice.draft_payment_set.get()
        payment_url = payment.get_payment_url()
        self.client.post(payment_url, {"method": "pay"})

        # Ensure there is only a single invoice object now
        self.assertEqual(Invoice.objects.count(), 1)
