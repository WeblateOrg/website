from decimal import Decimal

from django.test import TestCase

from weblate_web.payments.models import Customer

from .models import Discount, Invoice, InvoiceKindChoices, QuantityUnitChoices


class InvoiceTestCase(TestCase):
    @staticmethod
    def create_customer(vat: str = ""):
        return Customer.objects.create(
            name="Test customer",
            address="Street 42",
            city="City",
            postcode="424242",
            country="cz",
            user_id=-1,
            vat=vat,
        )

    def create_invoice(
        self,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        vat: str = "",
    ):
        invoice = Invoice.objects.create(
            customer=self.create_customer(vat=vat),
            discount=discount,
            vat_rate=vat_rate,
            kind=InvoiceKindChoices.INVOICE,
            customer_reference=customer_reference,
        )
        invoice.invoiceitem_set.create(
            description="Test item",
            unit_price=100,
        )
        return invoice

    def validate_invoice(self, invoice: Invoice):
        invoice.generate_pdf()
        self.assertNotEqual(str(invoice), "")
        if invoice.discount:
            self.assertNotEqual(str(invoice.discount), "")
        for item in invoice.all_items:
            self.assertNotEqual(str(item), "")

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
            quantity_unit=QuantityUnitChoices.HOURS,
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    def test_total_items_hour(self):
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=1,
            quantity_unit=QuantityUnitChoices.HOURS,
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
