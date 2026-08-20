from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import requests
import responses
from django.core.exceptions import ValidationError
from django.test.utils import override_settings
from django.urls import reverse
from django.utils.translation import override
from drafthorse.utils import validate_xml  # type: ignore[import-untyped]
from lxml import etree
from pycheval import generate_xml
from pycheval.quantities import QuantityCode
from pycheval.type_codes import TaxCategoryCode

from weblate_web.models import Package, PackageCategory
from weblate_web.payments.models import Customer
from weblate_web.tests import UserTestCase, cnb_mock_rates, mock_vies

from .models import (
    Currency,
    Discount,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
    QuantityUnit,
)
from .validation import EN16931Validator

S3_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "schemas" / "money-s3" / "_Document.xsd"
)
S3_SCHEMA = etree.XMLSchema(etree.parse(S3_SCHEMA_PATH))


class InvoiceTestCase(UserTestCase):
    def create_customer(
        self,
        *,
        vat: str = "",
        contact_point: str = "",
        email: str = "",
        accounting_reference: str = "",
        country: str = "cz",
    ) -> Customer:
        return Customer.objects.create(
            name="Zkušební zákazník",
            address="Street 42",
            city="City",
            postcode="424242",
            country=country,
            user_id=-1,
            vat=vat,
            contact_point=contact_point,
            email=email,
            accounting_reference=accounting_reference,
        )

    def create_invoice_base(  # ruff:ignore[too-many-arguments]
        self,
        *,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        customer_note: str = "",
        customer_contact_point: str = "",
        customer_email: str = "",
        accounting_reference: str = "",
        country: str = "cz",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        currency: Currency = Currency.EUR,
        tax_date: date | None = None,
        due_date: date | None = None,
        prepaid: bool = False,
    ) -> Invoice:
        if vat_rate == 0 and not vat:
            # Ensure VAT ID is present for invoices without VAT
            vat = "CZ21668027"
        return Invoice.objects.create(
            customer=self.create_customer(
                vat=vat,
                contact_point=customer_contact_point,
                email=customer_email,
                accounting_reference=accounting_reference,
                country=country,
            ),
            discount=discount,
            vat_rate=vat_rate,
            kind=kind,
            customer_note=customer_note,
            category=InvoiceCategory.HOSTING,
            customer_reference=customer_reference,
            currency=currency,
            tax_date=cast("date", tax_date),
            due_date=cast("date", due_date),
            prepaid=prepaid,
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

    def create_invoice(  # ruff:ignore[too-many-arguments]
        self,
        *,
        discount: Discount | None = None,
        vat_rate: int = 0,
        customer_reference: str = "",
        customer_note: str = "",
        customer_contact_point: str = "",
        customer_email: str = "",
        accounting_reference: str = "",
        country: str = "cz",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        tax_date: date | None = None,
        due_date: date | None = None,
        unit_price: int = 100,
        prepaid: bool = False,
    ) -> Invoice:
        invoice = self.create_invoice_base(
            discount=discount,
            vat_rate=vat_rate,
            customer_reference=customer_reference,
            customer_note=customer_note,
            customer_contact_point=customer_contact_point,
            customer_email=customer_email,
            accounting_reference=accounting_reference,
            country=country,
            vat=vat,
            kind=kind,
            tax_date=tax_date,
            due_date=due_date,
            prepaid=prepaid,
        )
        invoice.invoiceitem_set.create(
            description="Test item",
            unit_price=unit_price,
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

        # EN 16931 validation
        if invoice.is_final or invoice.is_proforma:
            einvoice = invoice.en_16931_xml_path.read_bytes()

            # Validate using drafthorse
            validate_xml(einvoice, "FACTUR-X_EN16931")

            # Validate calculations
            validator = EN16931Validator()

            is_valid, errors, warnings = validator.validate_bytes(einvoice)
            self.assertTrue(is_valid, errors)
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

            # Validate using https://github.com/gflohr/e-invoice-eu-validator
            if validator_url := os.environ.get("EINVOICE_VALIDATOR_URL"):
                # Validate standalone eInvoice
                response = requests.post(
                    f"{validator_url}validate",
                    files={"invoice": einvoice},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)
                # Validate eInvoice included in the PDF
                response = requests.post(
                    f"{validator_url}validate",
                    files={"invoice": invoice.path.read_bytes()},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)

            # Validate using EU eInvoice Validator service
            response = requests.post(
                "https://www.itb.ec.europa.eu/vitb/rest/invoice/api/validate",
                headers={"Accept": "application/json"},
                json={
                    "validationType": "cii",
                    "contentToValidate": einvoice.decode("utf-8"),
                },
                timeout=10,
            )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["result"], "SUCCESS", result["reports"])

    def get_einvoice_xml_tree(self, invoice: Invoice) -> etree._Element:
        return etree.fromstring(generate_xml(invoice.get_en_16931_xml()).encode())

    def get_money_s3_xml_tree(self, invoice: Invoice) -> etree._Element:
        document, invoices = invoice.get_invoice_xml_root()
        invoice.get_money_s3_xml_tree(invoices)
        return document

    def test_accounting_export_tax_classifications(self) -> None:
        cases = (
            (
                "Czech standard rate",
                "CZ",
                21,
                "",
                TaxCategoryCode.STANDARD_RATE,
                Decimal(21),
                None,
                "19Ř01,02",
            ),
            (
                "EU reverse charge",
                "DE",
                0,
                "DE123456789",
                TaxCategoryCode.REVERSE_CHARGE,
                Decimal(0),
                "Reverse charge",
                "19Ř21",
            ),
            (
                "outside EU",
                "US",
                0,
                "US123456789",
                TaxCategoryCode.OUT_OF_SCOPE,
                None,
                "Not subject to VAT because the place of supply is outside the EU",
                "19Ř26",
            ),
        )

        for (
            name,
            country,
            vat_rate,
            vat,
            expected_category,
            expected_rate,
            expected_reason,
            expected_money_s3_code,
        ) in cases:
            with self.subTest(name):
                invoice = self.create_invoice(
                    country=country,
                    vat_rate=vat_rate,
                    vat=vat,
                )

                einvoice = invoice.get_en_16931_xml()
                self.assertEqual(einvoice.tax[0].category_code, expected_category)
                self.assertEqual(einvoice.tax[0].rate_percent, expected_rate)
                self.assertEqual(einvoice.tax[0].exemption_reason, expected_reason)
                self.assertEqual(
                    {item.tax_category for item in einvoice.line_items},
                    {expected_category},
                )
                self.assertEqual(
                    {item.tax_rate for item in einvoice.line_items}, {expected_rate}
                )

                money_s3 = self.get_money_s3_xml_tree(invoice)
                S3_SCHEMA.assertValid(money_s3)
                self.assertEqual(
                    money_s3.findtext(".//FaktVyd/KodDPH"), expected_money_s3_code
                )

    def test_outside_eu_en_16931_omits_vat_details(self) -> None:
        invoice = self.create_invoice(
            country="US",
            vat="US123456789",
            discount=Discount.objects.create(
                description="Outside EU discount", percents=10
            ),
        )
        invoice.invoiceitem_set.create(description="Credit", unit_price=-10)

        einvoice = invoice.get_en_16931_xml()
        self.assertEqual(
            {allowance.tax_category for allowance in einvoice.allowances},
            {TaxCategoryCode.OUT_OF_SCOPE},
        )
        self.assertEqual(
            {allowance.tax_rate for allowance in einvoice.allowances}, {None}
        )

        xml = generate_xml(einvoice).encode()
        validate_xml(xml, "FACTUR-X_EN16931")
        root = etree.fromstring(xml)
        namespaces = EN16931Validator().namespaces
        categories = root.xpath(
            ".//ram:ApplicableTradeTax/ram:CategoryCode/text() | "
            ".//ram:CategoryTradeTax/ram:CategoryCode/text()",
            namespaces=namespaces,
        )
        self.assertTrue(categories)
        self.assertEqual(set(categories), {TaxCategoryCode.OUT_OF_SCOPE})
        self.assertEqual(
            root.xpath(".//ram:RateApplicablePercent", namespaces=namespaces), []
        )
        self.assertEqual(
            root.xpath(
                ".//ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']",
                namespaces=namespaces,
            ),
            [],
        )
        self.assertEqual(root.xpath(".//ram:TaxTotalAmount", namespaces=namespaces), [])
        self.assertEqual(
            root.xpath(".//ram:ExemptionReason/text()", namespaces=namespaces),
            ["Not subject to VAT because the place of supply is outside the EU"],
        )
        self.assertEqual(
            root.xpath(".//ram:CalculatedAmount/text()", namespaces=namespaces),
            ["0.00"],
        )

    def test_customer_reference_is_buyer_order_reference(self) -> None:
        invoice = self.create_invoice(customer_reference="PO123456")
        root = self.get_einvoice_xml_tree(invoice)

        buyer_order = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/"
            "ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID",
            EN16931Validator().namespaces,
        )

        self.assertIsNotNone(buyer_order)
        if buyer_order is None:
            self.fail("Buyer order reference is missing")
        self.assertEqual(buyer_order.text, "PO123456")

    def test_empty_customer_reference_omits_buyer_order_reference(self) -> None:
        invoice = self.create_invoice()
        root = self.get_einvoice_xml_tree(invoice)

        buyer_order = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerOrderReferencedDocument",
            EN16931Validator().namespaces,
        )

        self.assertIsNone(buyer_order)

    def test_customer_contact_is_buyer_trade_contact(self) -> None:
        invoice = self.create_invoice(
            customer_contact_point="Finance approvals",
            customer_email="finance@example.test",
        )
        root = self.get_einvoice_xml_tree(invoice)

        contact_name = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/"
            "ram:DefinedTradeContact/ram:PersonName",
            EN16931Validator().namespaces,
        )
        contact_email = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/"
            "ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID",
            EN16931Validator().namespaces,
        )

        self.assertIsNotNone(contact_name)
        self.assertIsNotNone(contact_email)
        if contact_name is None or contact_email is None:
            self.fail("Buyer trade contact is missing")
        self.assertEqual(contact_name.text, "Finance approvals")
        self.assertEqual(contact_email.text, "mailto:finance@example.test")

    def test_customer_email_is_buyer_trade_contact_email(self) -> None:
        invoice = self.create_invoice(customer_email="billing@example.test")
        root = self.get_einvoice_xml_tree(invoice)

        contact_email = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/"
            "ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID",
            EN16931Validator().namespaces,
        )

        self.assertIsNotNone(contact_email)
        if contact_email is None:
            self.fail("Buyer trade contact e-mail is missing")
        self.assertEqual(contact_email.text, "mailto:billing@example.test")

    def test_empty_customer_contact_omits_buyer_trade_contact(self) -> None:
        invoice = self.create_invoice()
        root = self.get_einvoice_xml_tree(invoice)

        contact = root.find(
            ".//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/"
            "ram:DefinedTradeContact",
            EN16931Validator().namespaces,
        )

        self.assertIsNone(contact)

    def test_customer_accounting_reference_is_line_trade_account(self) -> None:
        invoice = self.create_invoice(accounting_reference="COST-42")
        invoice.invoiceitem_set.create(description="Other item", unit_price=1000)
        root = self.get_einvoice_xml_tree(invoice)

        trade_accounts = root.findall(
            ".//ram:SpecifiedLineTradeSettlement/"
            "ram:ReceivableSpecifiedTradeAccountingAccount/ram:ID",
            EN16931Validator().namespaces,
        )

        self.assertEqual([account.text for account in trade_accounts], ["COST-42"] * 2)

    def test_empty_accounting_reference_omits_line_trade_account(self) -> None:
        invoice = self.create_invoice()
        root = self.get_einvoice_xml_tree(invoice)

        trade_account = root.find(
            ".//ram:SpecifiedLineTradeSettlement/"
            "ram:ReceivableSpecifiedTradeAccountingAccount",
            EN16931Validator().namespaces,
        )

        self.assertIsNone(trade_account)

    def test_customer_contact_point_is_rendered_on_invoice(self) -> None:
        invoice = self.create_invoice(customer_contact_point="Finance approvals")

        html = invoice.render_html()

        self.assertIn("Contact: Finance approvals", html)

    @responses.activate
    def test_pdf_page_marker_is_added_only_for_multi_page_invoice(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(kind=InvoiceKind.QUOTE)

        with (
            patch(
                "weblate_web.invoices.models.count_pdf_pages",
                side_effect=[1, 2],
            ) as count_pdf_pages,
            patch("weblate_web.invoices.models.render_pdf") as render_pdf,
        ):
            invoice.generate_pdf()
            single_page_html = render_pdf.call_args.kwargs["html"]

            render_pdf.reset_mock()
            invoice.generate_pdf()
            multi_page_html = render_pdf.call_args.kwargs["html"]

        self.assertEqual(count_pdf_pages.call_count, 2)
        self.assertNotIn("with-page-marker", single_page_html)
        self.assertIn("with-page-marker", multi_page_html)

    def mock_requests(self) -> None:
        mock_vies()
        cnb_mock_rates()
        responses.add_passthru(
            "https://www.itb.ec.europa.eu/vitb/rest/invoice/api/validate"
        )
        if validator_url := os.environ.get("EINVOICE_VALIDATOR_URL"):
            responses.add_passthru(f"{validator_url}validate")

    @responses.activate
    def test_dates(self) -> None:
        self.mock_requests()
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

    @responses.activate
    def test_total(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(vat="CZ8003280318")
        self.assertEqual(invoice.total_amount, 100)
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_vat(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(vat_rate=21, customer_reference="PO123456")
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_vat_note(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(
            vat_rate=21, customer_reference="PO123456", customer_note="Test note\n" * 3
        )
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_items(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item", unit_price=1000, quantity=4
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_items_hours(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=4,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 4100)
        self.assertEqual(
            invoice.get_en_16931_xml().line_items[-1].billed_quantity[1],
            QuantityCode.HOUR,
        )
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_items_hour(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice()
        invoice.invoiceitem_set.create(
            description="Other item",
            unit_price=1000,
            quantity=1,
            quantity_unit=QuantityUnit.HOURS,
        )
        self.assertEqual(invoice.total_amount, 1100)
        self.validate_invoice(invoice)

    @responses.activate
    def test_discount(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50)
        )
        self.assertEqual(invoice.total_amount, 50)
        self.validate_invoice(invoice)

    @responses.activate
    def test_discount_negative(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50),
        )
        invoice.invoiceitem_set.create(description="Prepaid amount", unit_price=-10)
        self.assertEqual(invoice.total_amount, 40)
        self.validate_invoice(invoice)

    @responses.activate
    def test_discount_vat(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(
            discount=Discount.objects.create(description="Test discount", percents=50),
            vat_rate=21,
        )
        self.assertEqual(invoice.total_amount, Decimal("60.50"))
        self.validate_invoice(invoice)

    @responses.activate
    def test_refund(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(vat_rate=21, unit_price=-100, prepaid=True)
        self.assertEqual(invoice.total_amount, Decimal(-121))
        self.validate_invoice(invoice)

    @responses.activate
    def test_package(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice_package()
        self.assertEqual(invoice.total_amount, Decimal(100))
        self.validate_invoice(invoice)

    @responses.activate
    def test_package_usd(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice_package(currency=Currency.USD)
        self.assertEqual(
            invoice.total_amount,
            round(Decimal(100) * invoice.exchange_rate_eur * Decimal("1.1"), 0),
        )
        self.validate_invoice(invoice)

    @responses.activate
    def test_invoice_kinds(self) -> None:
        self.mock_requests()
        for kind in InvoiceKind.values:
            invoice = self.create_invoice(kind=InvoiceKind(kind))
            self.validate_invoice(invoice)

    @override_settings(PAYMENT_DEBUG=True)
    def test_final_invoice_payment_skips_customer_revalidation(self) -> None:
        invoice = self.create_invoice_package()

        with patch.object(
            Customer,
            "prepayment_validation",
            side_effect=ValidationError("VIES unavailable"),
        ) as prepayment_validation:
            response = self.client.get(
                cast("str", invoice.get_payment_url()), follow=True
            )

        self.assertContains(response, "Payment Summary")
        self.assertContains(response, 'name="method"')
        prepayment_validation.assert_not_called()

    @override_settings(PAYMENT_DEBUG=True)
    @responses.activate
    def test_pay_link(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice_package()
        self.validate_invoice(invoice)
        url = cast("str", invoice.get_payment_url())
        self.assertIsNotNone(url)

        # Unauthenticated users can open a manually issued invoice payment.
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Payment Summary")
        # Unauthenticated user should see note about terms
        self.assertContains(response, "By performing the payment, you accept our")
        self.assertNotContains(response, "Billing information")
        self.assertEqual(invoice.draft_payment_set.count(), 1)

        # The invoice payment capability does not permit editing its customer.
        payment = invoice.draft_payment_set.get()
        with override("en"):
            customer_url = reverse("payment-customer", kwargs={"pk": payment.pk})
        original_name = invoice.customer.name
        self.assertEqual(self.client.get(customer_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                customer_url,
                {
                    "name": "Unauthorized Customer",
                    "address": "Unauthorized address",
                    "city": "Unauthorized city",
                    "postcode": "12345",
                    "country": "CZ",
                },
            ).status_code,
            404,
        )
        invoice.customer.refresh_from_db()
        self.assertEqual(invoice.customer.name, original_name)

        # Repeated access should reuse existing payment
        self.login()
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Payment Summary")
        # Logged-in user should not see this
        self.assertNotContains(response, "By performing the payment, you accept our")
        self.assertNotContains(response, "Billing information")
        self.assertEqual(invoice.draft_payment_set.count(), 1)

        # Pay
        payment_url = payment.get_payment_url()
        self.client.post(payment_url, {"method": "pay"})

        # Ensure there is only a single invoice object now
        self.assertEqual(Invoice.objects.count(), 1)
