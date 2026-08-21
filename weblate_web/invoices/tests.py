from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

import requests
import responses
from django.apps import apps
from django.core.exceptions import ValidationError
from django.forms import modelform_factory
from django.test.utils import override_settings
from django.urls import reverse
from django.utils.translation import override
from drafthorse.utils import validate_xml  # type: ignore[import-untyped]
from lxml import etree
from pycheval.quantities import QuantityCode
from pycheval.type_codes import TaxCategoryCode

from weblate_web.models import Package, PackageCategory
from weblate_web.payments.models import Customer, Payment
from weblate_web.tests import UserTestCase, cnb_mock_rates, mock_vies

from .models import (
    Currency,
    Discount,
    Invoice,
    InvoiceCalculationVersion,
    InvoiceCategory,
    InvoiceKind,
    QuantityUnit,
)
from .validation import EN16931Validator

S3_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "schemas" / "money-s3" / "_Document.xsd"
)
S3_SCHEMA = etree.XMLSchema(etree.parse(S3_SCHEMA_PATH))


class InvoiceTestCase(UserTestCase):  # ruff:ignore[too-many-public-methods]
    def create_customer(
        self,
        *,
        vat: str = "",
        contact_point: str = "",
        email: str = "",
        accounting_reference: str = "",
        country: str = "DE",
    ) -> Customer:
        customer = Customer.objects.create(
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
        if vat:
            customer.vat_validation_state = Customer.VatValidationState.VALID
            customer.save(update_fields=["vat_validation_state"])
        return customer

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
        country: str = "DE",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        currency: Currency = Currency.EUR,
        tax_date: date | None = None,
        due_date: date | None = None,
        prepaid: bool = False,
    ) -> Invoice:
        if vat_rate == 0 and not vat and country.upper() == "DE":
            # Ensure VAT ID is present for invoices without VAT
            vat = "DE123456789"
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
        country: str = "DE",
        vat: str = "",
        kind: InvoiceKind = InvoiceKind.INVOICE,
        tax_date: date | None = None,
        due_date: date | None = None,
        unit_price: int | Decimal = 100,
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
        return etree.fromstring(invoice.get_en_16931_xml_string().encode())

    def get_money_s3_xml_tree(self, invoice: Invoice) -> etree._Element:
        document, invoices = invoice.get_invoice_xml_root()
        invoice.get_money_s3_xml_tree(invoices)
        return document

    def test_initial_vat_rate_is_derived_from_customer(self) -> None:
        cases = (
            ("Czech customer", "CZ", "", Customer.VatValidationState.UNKNOWN, 21),
            ("EU end user", "DE", "", Customer.VatValidationState.UNKNOWN, 21),
            (
                "EU customer with valid VAT",
                "DE",
                "DE123456789",
                Customer.VatValidationState.VALID,
                0,
            ),
            ("non-EU customer", "US", "", Customer.VatValidationState.UNKNOWN, 0),
        )

        for name, country, vat, validation_state, expected_rate in cases:
            with self.subTest(name):
                customer = self.create_customer(country=country, vat=vat)
                customer.vat_validation_state = validation_state
                customer.save(update_fields=["vat_validation_state"])

                invoice = Invoice.objects.create(
                    customer=customer,
                    kind=InvoiceKind.DRAFT,
                    category=InvoiceCategory.HOSTING,
                )

                self.assertEqual(invoice.vat_rate, expected_rate)

    def test_initial_vat_rate_rejects_unvalidated_eu_vat(self) -> None:
        for validation_state in (
            Customer.VatValidationState.UNKNOWN,
            Customer.VatValidationState.INVALID,
        ):
            with self.subTest(validation_state=validation_state):
                customer = self.create_customer(country="DE", vat="DE123456789")
                customer.vat_validation_state = validation_state
                customer.save(update_fields=["vat_validation_state"])

                with self.assertRaisesMessage(
                    ValidationError,
                    "EU reverse-charge invoices require a valid buyer VAT ID.",
                ):
                    Invoice.objects.create(
                        customer=customer,
                        kind=InvoiceKind.DRAFT,
                        category=InvoiceCategory.HOSTING,
                    )

    def test_initial_positive_vat_rate_is_preserved(self) -> None:
        invoice = Invoice.objects.create(
            customer=self.create_customer(country="DE"),
            kind=InvoiceKind.DRAFT,
            category=InvoiceCategory.HOSTING,
            vat_rate=19,
        )

        self.assertEqual(invoice.vat_rate, 19)

        invoice.vat_rate = 7
        invoice.save(update_fields=["vat_rate"])
        invoice.refresh_from_db()
        self.assertEqual(invoice.vat_rate, 7)

    def test_invoice_form_rejects_unvalidated_eu_vat(self) -> None:
        customer = self.create_customer(country="DE", vat="DE123456789")
        customer.vat_validation_state = Customer.VatValidationState.INVALID
        customer.save(update_fields=["vat_validation_state"])
        invoice_form = modelform_factory(
            Invoice,
            fields=("kind", "category", "customer", "vat_rate", "currency"),
        )
        form = invoice_form(
            data={
                "kind": InvoiceKind.INVOICE,
                "category": InvoiceCategory.HOSTING,
                "customer": customer.pk,
                "vat_rate": 0,
                "currency": Currency.EUR,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertFormError(
            form,
            "vat_rate",
            "EU reverse-charge invoices require a valid buyer VAT ID.",
        )

    def test_duplicate_preserves_explicit_zero_vat_rate(self) -> None:
        invoice = self.create_invoice(
            country="DE", vat="DE123456789", kind=InvoiceKind.DRAFT
        )
        invoice.customer.vat_validation_state = Customer.VatValidationState.UNKNOWN
        invoice.customer.save(update_fields=["vat_validation_state"])

        duplicate = invoice.duplicate(kind=InvoiceKind.DRAFT)

        self.assertEqual(duplicate.vat_rate, 0)
        self.assertEqual(duplicate.total_amount, invoice.total_amount)

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

        xml = invoice.get_en_16931_xml_string().encode()
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

    def test_invalid_zero_vat_states_are_rejected_before_generation(self) -> None:
        cases = (
            ("CZ", "CZ21668027", Customer.VatValidationState.VALID),
            ("DE", "", Customer.VatValidationState.UNKNOWN),
            ("DE", "DE123456789", Customer.VatValidationState.UNKNOWN),
            ("DE", "DE123456789", Customer.VatValidationState.INVALID),
        )
        for country, vat, validation_state in cases:
            with (
                self.subTest(
                    country=country, vat=vat, validation_state=validation_state
                ),
                TemporaryDirectory() as temp_dir,
                override_settings(INVOICES_PATH=Path(temp_dir)),
            ):
                invoice = self.create_invoice(country=country, vat=vat)
                invoice.customer.vat_validation_state = validation_state
                invoice.customer.save(update_fields=["vat_validation_state"])
                invoice.vat_rate = 0
                invoice.save(update_fields=["vat_rate"])

                with self.assertRaises(ValidationError):
                    invoice.full_clean()
                with self.assertRaises(ValidationError):
                    invoice.generate_files()

                self.assertFalse(invoice.xml_path.exists())
                self.assertFalse(invoice.en_16931_xml_path.exists())
                self.assertFalse(invoice.path.exists())

    def test_draft_validation_allows_incomplete_tax_state(self) -> None:
        invoice = self.create_invoice(
            country="CZ", vat="CZ21668027", kind=InvoiceKind.DRAFT
        )
        invoice.vat_rate = 0
        invoice.save(update_fields=["vat_rate"])

        invoice.full_clean()
        with self.assertRaises(ValidationError):
            invoice.generate_files()

    def test_quote_validation_rejects_incomplete_tax_state(self) -> None:
        invoice = self.create_invoice(country="FR", kind=InvoiceKind.QUOTE)
        invoice.vat_rate = 0
        invoice.save(update_fields=["vat_rate"])

        with self.assertRaises(ValidationError):
            invoice.full_clean()
        with self.assertRaises(ValidationError):
            invoice.generate_files()

    def test_validation_without_customer_reports_field_error(self) -> None:
        invoice_form = modelform_factory(
            Invoice,
            fields=("kind", "category", "customer", "vat_rate", "currency"),
        )
        form = invoice_form(
            data={
                "kind": InvoiceKind.INVOICE,
                "category": InvoiceCategory.HOSTING,
                "customer": "",
                "vat_rate": 0,
                "currency": Currency.EUR,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("customer", form.errors)

    def test_tax_point_date_is_exported_only_for_final_invoice(self) -> None:
        tax_date = date(2025, 6, 30)
        final_invoice = self.create_invoice(vat_rate=21, tax_date=tax_date)
        proforma = self.create_invoice(
            vat_rate=21, tax_date=tax_date, kind=InvoiceKind.PROFORMA
        )
        namespaces = EN16931Validator().namespaces

        final_root = self.get_einvoice_xml_tree(final_invoice)
        self.assertEqual(
            final_root.xpath(
                ".//ram:TaxPointDate/udt:DateString/text()",
                namespaces=namespaces,
            ),
            ["20250630"],
        )
        proforma_root = self.get_einvoice_xml_tree(proforma)
        self.assertEqual(
            proforma_root.xpath(".//ram:TaxPointDate", namespaces=namespaces), []
        )

    def test_validator_rejects_three_decimal_monetary_amounts(self) -> None:
        root = self.get_einvoice_xml_tree(self.create_invoice(vat_rate=21))
        namespaces = EN16931Validator().namespaces
        mutations = (
            (
                (
                    ".//ram:SpecifiedTradeSettlementLineMonetarySummation/"
                    "ram:LineTotalAmount"
                ),
                "BR-DEC-23",
            ),
            (
                (
                    ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
                    "ram:LineTotalAmount"
                ),
                "BR-DEC-09",
            ),
            (
                (
                    ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
                    "ram:TaxBasisTotalAmount"
                ),
                "BR-DEC-12",
            ),
        )
        for xpath, expected_rule in mutations:
            with self.subTest(rule=expected_rule):
                mutated = etree.fromstring(etree.tostring(root))
                element = mutated.find(xpath, namespaces)
                self.assertIsNotNone(element)
                if element is None:
                    self.fail(f"Missing test element for {expected_rule}")
                element.text = "100.001"

                is_valid, errors, _warnings = EN16931Validator().validate_bytes(
                    etree.tostring(mutated)
                )

                self.assertFalse(is_valid)
                self.assertIn(expected_rule, {error.rule for error in errors})

    def test_validator_rejects_incomplete_reverse_charge(self) -> None:
        invoice = self.create_invoice(
            discount=Discount.objects.create(
                description="Reverse charge discount", percents=10
            )
        )
        root = self.get_einvoice_xml_tree(invoice)
        namespaces = EN16931Validator().namespaces
        buyer_vat = root.find(
            ".//ram:BuyerTradeParty/ram:SpecifiedTaxRegistration/"
            "ram:ID[@schemeID='VA']",
            namespaces,
        )
        exemption_reason = root.find(
            ".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/"
            "ram:ExemptionReason",
            namespaces,
        )
        self.assertIsNotNone(buyer_vat)
        self.assertIsNotNone(exemption_reason)
        if buyer_vat is None or exemption_reason is None:
            self.fail("Generated reverse-charge test data is incomplete")
        buyer_vat_parent = buyer_vat.getparent()
        exemption_reason_parent = exemption_reason.getparent()
        if buyer_vat_parent is None or exemption_reason_parent is None:
            self.fail("Generated reverse-charge elements have no parent")
        buyer_vat_parent.remove(buyer_vat)
        exemption_reason_parent.remove(exemption_reason)

        is_valid, errors, _warnings = EN16931Validator().validate_bytes(
            etree.tostring(root)
        )

        self.assertFalse(is_valid)
        rules = {error.rule for error in errors}
        required_rules = {"BR-AE-02", "BR-AE-03", "BR-AE-04"}
        self.assertSetEqual(required_rules & rules, required_rules)
        self.assertIn("BR-AE-10", rules)

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
        invoice = self.create_invoice(vat="DE123456789")
        self.assertEqual(invoice.total_amount, 100)
        self.validate_invoice(invoice)

    @responses.activate
    def test_total_vat(self) -> None:
        self.mock_requests()
        invoice = self.create_invoice(vat_rate=21, customer_reference="PO123456")
        self.assertEqual(invoice.total_amount, 121)
        self.validate_invoice(invoice)

    def test_amounts_are_calculated_once(self) -> None:
        invoice = self.create_invoice(vat_rate=21)

        with patch.object(
            invoice,
            "_get_en_16931_amounts",
            wraps=invoice._get_en_16931_amounts,  # pylint: disable=protected-access
        ) as calculate_amounts:
            self.assertEqual(invoice.total_amount_no_vat, 100)
            self.assertEqual(invoice.total_vat, 21)
            self.assertEqual(invoice.total_amount, 121)

        calculate_amounts.assert_called_once_with()

    def test_half_up_vat_rounding_is_shared_by_all_formats(self) -> None:
        invoice = self.create_invoice(vat_rate=21, unit_price=Decimal("0.50"))

        self.assertEqual(invoice.total_amount_no_vat, Decimal("0.50"))
        self.assertEqual(invoice.total_vat, Decimal("0.11"))
        self.assertEqual(invoice.total_amount, Decimal("0.61"))
        self.assertIn("€0.11", invoice.render_html())

        einvoice = invoice.get_en_16931_xml()
        self.assertEqual(einvoice.tax_basis_total_amount.amount, Decimal("0.50"))
        self.assertEqual(einvoice.tax_total_amounts[0].amount, Decimal("0.11"))
        self.assertEqual(einvoice.grand_total_amount.amount, Decimal("0.61"))

        money_s3 = self.get_money_s3_xml_tree(invoice)
        self.assertEqual(money_s3.findtext(".//Valuty/SouhrnDPH/Zaklad22"), "0.50")
        self.assertEqual(money_s3.findtext(".//Valuty/SouhrnDPH/DPH22"), "0.11")
        self.assertEqual(money_s3.findtext(".//Valuty/Celkem"), "0.61")

    def test_linked_payment_does_not_change_invoice_amounts(self) -> None:
        invoice = self.create_invoice(
            vat_rate=21, unit_price=Decimal("50.00"), prepaid=True
        )
        Payment.objects.create(
            amount=60,
            amount_fixed=True,
            customer=invoice.customer,
            currency=Payment.CURRENCY_EUR,
            description="Legacy truncated payment",
            paid_invoice=invoice,
            state=Payment.PROCESSED,
        )

        self.assertEqual(invoice.total_amount_no_vat, Decimal("50.00"))
        self.assertEqual(invoice.total_vat, Decimal("10.50"))
        self.assertEqual(invoice.total_amount, Decimal("60.50"))
        xml = invoice.get_en_16931_xml_string().encode()
        validate_xml(xml, "FACTUR-X_EN16931")
        is_valid, errors, warnings = EN16931Validator().validate_bytes(xml)
        self.assertTrue(is_valid, errors)
        self.assertEqual(warnings, [])

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

    def test_legacy_discount_rounding_is_preserved(self) -> None:
        discount = Discount.objects.create(description="Legacy discount", percents=50)
        invoice = self.create_invoice(discount=discount, unit_price=Decimal(101))
        self.assertEqual(invoice.total_amount, Decimal("50.50"))

        invoice.calculation_version = InvoiceCalculationVersion.LEGACY
        invoice.save(update_fields=["calculation_version"])
        self.assertEqual(invoice.total_discount, Decimal(-50))
        self.assertEqual(invoice.total_amount_no_vat, Decimal(51))
        self.assertEqual(invoice.total_amount, Decimal(51))

        einvoice = invoice.get_en_16931_xml()
        allowance_total = einvoice.allowance_total_amount
        if allowance_total is None:
            self.fail("Legacy discount invoice has no allowance total")
        self.assertEqual(allowance_total.amount, Decimal(50))
        self.assertEqual(einvoice.tax_basis_total_amount.amount, Decimal(51))
        self.assertEqual(einvoice.grand_total_amount.amount, Decimal(51))

    def test_draft_migration_preserves_in_flight_calculation(self) -> None:
        in_flight = self.create_invoice(kind=InvoiceKind.DRAFT)
        convertible = self.create_invoice(kind=InvoiceKind.DRAFT)
        Invoice.objects.filter(pk__in=(in_flight.pk, convertible.pk)).update(
            calculation_version=InvoiceCalculationVersion.LEGACY
        )
        Payment.objects.create(
            amount=100,
            customer=in_flight.customer,
            description="In-flight draft",
            draft_invoice=in_flight,
            state=Payment.PENDING,
        )

        migration = import_module(
            "weblate_web.invoices.migrations.0003_invoice_calculation_version"
        )
        migration.use_current_calculation_for_drafts(apps, None)

        in_flight.refresh_from_db()
        convertible.refresh_from_db()
        self.assertEqual(
            in_flight.calculation_version, InvoiceCalculationVersion.LEGACY
        )
        self.assertEqual(
            convertible.calculation_version, InvoiceCalculationVersion.EN_16931
        )

    def test_duplicate_preserves_calculation_version(self) -> None:
        discount = Discount.objects.create(description="Proforma discount", percents=50)
        proforma = self.create_invoice(
            discount=discount,
            kind=InvoiceKind.PROFORMA,
            unit_price=Decimal(101),
        )
        proforma.calculation_version = InvoiceCalculationVersion.LEGACY
        proforma.save(update_fields=["calculation_version"])

        invoice = proforma.duplicate(
            kind=InvoiceKind.INVOICE,
            tax_date=proforma.tax_date,
        )

        self.assertEqual(invoice.calculation_version, InvoiceCalculationVersion.LEGACY)
        self.assertEqual(invoice.total_amount, Decimal(51))

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
