from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from django.test import TestCase
from lxml import etree

from weblate_web.models import Package, PackageCategory
from weblate_web.payments.models import Customer

from .models import Currency, Discount, Invoice, InvoiceKind, QuantityUnit

S3_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "schemas" / "money-s3" / "_Document.xsd"
)
S3_SCHEMA = etree.XMLSchema(etree.parse(S3_SCHEMA_PATH))  # noqa: S320


class InvoiceTestCase(TestCase):
    def create_customer(self, vat: str = "") -> Customer:
        return Customer.objects.create(
            name="Zkušební zákazník",
            address="Street 42",
            city="City",
            postcode="424242",
            country="cz",
            user_id=-1,
            vat=vat,
        )

    def create_invoice_base(
        self,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        vat: str = "",
        currency: Currency = Currency.EUR,
    ) -> Invoice:
        return Invoice.objects.create(
            customer=self.create_customer(vat=vat),
            discount=discount,
            vat_rate=vat_rate,
            kind=InvoiceKind.INVOICE,
            customer_reference=customer_reference,
            currency=currency,
        )

    def create_invoice_package(
        self,
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

    def create_invoice(
        self,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        vat: str = "",
    ) -> Invoice:
        invoice = self.create_invoice_base(
            discount=discount,
            vat_rate=vat_rate,
            customer_reference=customer_reference,
            vat=vat,
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
        xml_doc = etree.parse(invoice.xml_path)  # noqa: S320
        S3_SCHEMA.assertValid(xml_doc)

    def test_total(self):
        invoice = self.create_invoice(vat="CZ8003280318")
        self.assertEqual(invoice.total_amount, 100)
        self.validate_invoice(invoice)

    def test_total_vat(self):
        invoice = self.create_invoice(vat_rate=21, customer_reference="PO123456")
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    def test_total_items(self):
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item", unit_price=1000, quantity=4
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    def test_total_items_hours(self):
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=4,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    def test_total_items_hour(self):
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=1,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 1100)
        self.validate_invoice(invoice)

    def test_discount(self):
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50)
        )
        self.assertEqual(invoice.total_amount, 50)
        self.validate_invoice(invoice)

    def test_discount_vat(self):
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50),
            vat_rate=21,
        )
        self.assertEqual(invoice.total_amount, Decimal("60.50"))
        self.validate_invoice(invoice)

    def test_package(self):
        invoice = self.create_invoice_package()
        self.assertEqual(invoice.total_amount, Decimal(100))
        self.validate_invoice(invoice)

    def test_package_usd(self):
        invoice = self.create_invoice_package(currency=Currency.USD)
        self.assertEqual(
            invoice.total_amount, round(Decimal(100) * invoice.exchange_rate_eur, 0)
        )
        self.validate_invoice(invoice)
