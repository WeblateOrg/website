#
# Copyright © Michal Čihař <michal@weblate.org>
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

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import Any, cast
from unittest.mock import patch

import responses
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings
from django.utils import timezone

from weblate_web.crm.models import Interaction
from weblate_web.invoices.models import Invoice, InvoiceCategory, InvoiceKind
from weblate_web.tests import (
    THEPAY2_MOCK_SETTINGS,
    cnb_mock_rates,
    mock_vies,
    thepay_mock_create_payment,
    thepay_mock_payment,
)

from .backends import FioBank, InvalidState, PaymentError, get_backend, list_backends
from .models import Customer, Payment
from .validators import validate_vatin

CUSTOMER = {
    "name": "Michal Čihař",
    "address": "Zdiměřická 1439",
    "city": "Praha 4",
    "postcode": "149 00",
    "country": "CZ",
    "vat": "CZ8003280318",
    "email": "noreply@example.com",
    "user_id": 6,
}
VIES_MS_UNAVAILABLE = "MS_UNAVAILABLE"
VIES_MS_MAX_CONCURRENT_REQ = "MS_MAX_CONCURRENT_REQ"
VIES_TIMEOUT = "TIMEOUT"


FIO_API = "https://fioapi.fio.cz/v1/rest/last/test-token/transactions.json"
FIO_TRASACTIONS = {
    "accountStatement": {
        "info": {
            "dateStart": "2016-08-03+0200",
            "idList": None,
            "idLastDownload": None,
            "closingBalance": 2060.52,
            "bic": "FIOBCZPPXXX",
            "yearList": None,
            "idTo": 10000000001,
            "currency": "EUR",
            "openingBalance": 2543.81,
            "iban": "CZ1220100000001234567890",
            "idFrom": 10000000002,
            "bankId": "2010",
            "dateEnd": "2016-08-03+0200",
            "accountId": "1234567890",
        },
        "transactionList": {
            "transaction": [
                {
                    "column18": None,
                    "column26": None,
                    "column10": None,
                    "column12": None,
                    "column14": {"name": "M\u011bna", "value": "CZK", "id": 14},
                    "column17": {"name": "ID pokynu", "value": 12210748893, "id": 17},
                    "column16": {
                        "name": "Zpr\u00e1va pro p\u0159\u00edjemce",
                        "value": "N\u00e1kup: ORDR, PRAGUE",
                        "id": 16,
                    },
                    "column22": {"name": "ID pohybu", "value": 10000000002, "id": 22},
                    "column9": {"name": "Provedl", "value": "Javorek, Jan", "id": 9},
                    "column8": {"name": "Typ", "value": "Platba kartou", "id": 8},
                    "column25": {
                        "name": "Koment\u00e1\u0159",
                        "value": "N\u00e1kup: ORDR, PRAGUE",
                        "id": 25,
                    },
                    "column5": {"name": "VS", "value": "5678", "id": 5},
                    "column4": None,
                    "column7": {
                        "name": "U\u017eivatelsk\u00e1 identifikace",
                        "value": "N\u00e1kup: ORDR, PRAGUE",
                        "id": 7,
                    },
                    "column6": None,
                    "column1": {"name": "Objem", "value": -130.0, "id": 1},
                    "column0": {"name": "Datum", "value": "2016-08-03+0200", "id": 0},
                    "column3": None,
                    "column2": None,
                },
                {
                    "column18": None,
                    "column26": None,
                    "column10": None,
                    "column12": None,
                    "column14": {"name": "M\u011bna", "value": "CZK", "id": 14},
                    "column17": {"name": "ID pokynu", "value": 12210832097, "id": 17},
                    "column16": {
                        "name": "Zpr\u00e1va pro p\u0159\u00edjemce",
                        "value": "200000000",
                        "id": 16,
                    },
                    "column22": {"name": "ID pohybu", "value": 10000000001, "id": 22},
                    "column9": {"name": "Provedl", "value": "Javorek, Jan", "id": 9},
                    "column8": {"name": "Typ", "value": "Platba kartou", "id": 8},
                    "column25": {
                        "name": "Koment\u00e1\u0159",
                        "value": "N\u00e1kup: Billa Ul. Konevova",
                        "id": 25,
                    },
                    "column5": {"name": "VS", "value": "1234", "id": 5},
                    "column4": None,
                    "column7": {
                        "name": "U\u017eivatelsk\u00e1 identifikace",
                        "value": "N\u00e1kup: Billa Ul. Konevova",
                        "id": 7,
                    },
                    "column6": None,
                    "column1": {"name": "Objem", "value": -353.29, "id": 1},
                    "column0": {"name": "Datum", "value": "2016-08-03+0200", "id": 0},
                    "column3": None,
                    "column2": None,
                },
            ]
        },
    }
}


class ModelTest(SimpleTestCase):
    def test_vat(self) -> None:
        customer = Customer()
        self.assertFalse(customer.needs_vat)
        customer = Customer(**CUSTOMER)
        # Czech customer needs VAT
        self.assertTrue(customer.needs_vat)
        # EU enduser needs VAT
        customer.vat = ""
        self.assertTrue(customer.needs_vat)
        # EU company does not need VAT
        customer.vat = "IE6388047V"
        self.assertFalse(customer.needs_vat)
        # Non EU customer does not need VAT
        customer.vat = ""
        customer.country = "US"
        self.assertFalse(customer.needs_vat)

    def test_empty(self) -> None:
        customer = Customer(country="CZ")
        self.assertTrue(customer.is_empty)
        customer = Customer(**CUSTOMER)
        self.assertFalse(customer.is_empty)
        customer.vat = None
        self.assertFalse(customer.is_empty)
        customer.postcode = ""
        self.assertTrue(customer.is_empty)

    def test_clean(self) -> None:
        customer = Customer(**CUSTOMER)
        customer.clean()
        customer.country = "IE"
        with self.assertRaises(ValidationError):
            customer.clean()

    def test_vat_calculation(self) -> None:
        customer = Customer(**CUSTOMER)
        payment = Payment(customer=customer, amount=100)
        self.assertEqual(payment.vat_amount, 121)
        payment = Payment(customer=customer, amount=100, amount_fixed=True)
        self.assertEqual(payment.vat_amount, 100)
        self.assertAlmostEqual(payment.amount_without_vat, 82.64, places=2)

        customer.vat = "IE6388047V"
        payment = Payment(customer=customer, amount=100)
        self.assertEqual(payment.vat_amount, 100)
        payment = Payment(customer=customer, amount=100, amount_fixed=True)
        self.assertEqual(payment.vat_amount, 100)
        self.assertEqual(payment.amount_without_vat, 100)

    def test_short_filename(self) -> None:
        customer = Customer()
        customer.name = "Weblate s.r.o."
        self.assertEqual(customer.short_filename, "Weblate_sro")
        customer.name = "Weblate / s.r.o."
        self.assertEqual(customer.short_filename, "Weblate_sro")
        customer.name = " Weblate / s.r.o.\\"
        self.assertEqual(customer.short_filename, "Weblate_sro")
        customer.name = " Weblate - s.r.o.\\"
        self.assertEqual(customer.short_filename, "Weblate")
        customer.name = "Zkouška"
        self.assertEqual(customer.short_filename, "Zkouska")
        customer.name = "Ελληνικά"
        self.assertEqual(customer.short_filename, "Ellenika")
        customer.name = "Русский"
        self.assertEqual(customer.short_filename, "Russkii")
        customer.name = "正體中文"
        self.assertEqual(customer.short_filename, "Zheng_Ti_Zhong_Wen")


class ModelObjectsTestCase(TestCase):
    def create_customer_for_vat_validation(self) -> Customer:
        customer = Customer.objects.create(
            user_id=-1,
            name=cast("str", CUSTOMER["name"]),
            address=cast("str", CUSTOMER["address"]),
            city=cast("str", CUSTOMER["city"]),
            postcode=cast("str", CUSTOMER["postcode"]),
            country=cast("str", CUSTOMER["country"]),
            vat="",
        )
        customer.vat = cast("str", CUSTOMER["vat"])
        customer.vat_validation_state = Customer.VatValidationState.VALID
        return customer

    def test_merge(self) -> None:
        customer = Customer.objects.create(**CUSTOMER)
        self.assertEqual(0, customer.users.count())
        Payment.objects.create(customer=customer, amount=100)
        customer2 = Customer.objects.create(**CUSTOMER)
        Payment.objects.create(customer=customer2, amount=100)
        customer.merge(customer2)
        self.assertEqual(2, customer.payment_set.count())
        customer2 = Customer.objects.create(**CUSTOMER)
        customer2.users.add(User.objects.create())
        customer.merge(customer2)
        self.assertEqual(1, customer.users.count())

    def test_automated_vies_transient_fault_is_not_logged(self) -> None:
        for code in (
            VIES_MS_UNAVAILABLE,
            f"env:Server: {VIES_MS_UNAVAILABLE}",
            VIES_MS_MAX_CONCURRENT_REQ,
            f"env:Server: {VIES_MS_MAX_CONCURRENT_REQ}",
            VIES_TIMEOUT,
            f"soap:Server: {VIES_TIMEOUT}",
        ):
            with self.subTest(code=code):
                customer = self.create_customer_for_vat_validation()

                with (
                    patch(
                        "weblate_web.payments.models.validate_vatin",
                        side_effect=ValidationError(
                            "The VIES service is unavailable",
                            code=code,
                        ),
                    ),
                    self.assertRaises(ValidationError),
                ):
                    customer.prepayment_validation(automated=True)

                self.assertFalse(Interaction.objects.filter(customer=customer).exists())

    def test_automated_vies_validation_error_is_logged(self) -> None:
        customer = self.create_customer_for_vat_validation()
        code = "INVALID_INPUT"
        message = "The VIES service rejected the VAT ID"

        with (
            patch(
                "weblate_web.payments.models.validate_vatin",
                side_effect=ValidationError(message, code=code),
            ),
            self.assertRaises(ValidationError),
        ):
            customer.prepayment_validation(automated=True)

        interaction = Interaction.objects.get(customer=customer)
        self.assertEqual(interaction.origin, Interaction.Origin.VIES)
        self.assertEqual(interaction.summary, code)
        self.assertEqual(interaction.content, message)
        self.assertEqual(
            interaction.details,
            {
                "automated": True,
                "vat": customer.vat,
                "code": code,
                "message": message,
            },
        )


class BackendBaseTestCase(TestCase):
    backend_name: str = ""

    def setUp(self) -> None:
        super().setUp()
        self.customer = Customer.objects.create(**CUSTOMER)
        self.payment = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description="Test Item",
            backend=self.backend_name,
        )

    def check_payment(self, state: int) -> Payment:
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.state, state)
        return payment


@override_settings(PAYMENT_DEBUG=True)
class BackendTest(BackendBaseTestCase):
    def create_invoice(self) -> Invoice:
        mock_vies()
        cnb_mock_rates()
        invoice = Invoice.objects.create(
            customer=self.customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        invoice.invoiceitem_set.create(
            description="Test item",
            unit_price=100,
        )
        invoice.generate_files()
        return invoice

    def mock_fio_payment(  # noqa: PLR0913
        self,
        invoice: Invoice,
        payment_message: str = "",
        *,
        amount: int | None = None,
        currency: str = "EUR",
        sender_account: str | None = None,
        transaction_id: int | None = 12210832097,
        replace: bool = False,
    ) -> None:
        received: dict[str, Any] = deepcopy(FIO_TRASACTIONS)
        received["accountStatement"]["info"]["currency"] = currency
        transaction = received["accountStatement"]["transactionList"]["transaction"]  # type: ignore[index]
        if not payment_message:
            payment_message = invoice.number
        transaction[0]["column16"]["value"] = payment_message
        transaction[1]["column16"]["value"] = payment_message
        transaction[1]["column1"]["value"] = (
            int(invoice.total_amount) if amount is None else amount
        )
        if sender_account is not None:
            transaction[1]["column2"] = {
                "name": "Protiúčet",
                "value": sender_account,
                "id": 2,
            }
            transaction[1]["column3"] = {
                "name": "Kód banky",
                "value": "0100",
                "id": 3,
            }
        if transaction_id is None:
            transaction[1]["column22"] = None
        else:
            transaction[1]["column22"]["value"] = transaction_id
        if replace:
            responses.replace(responses.GET, FIO_API, body=json.dumps(received))
        else:
            responses.add(responses.GET, FIO_API, body=json.dumps(received))

    def test_pay(self) -> None:
        backend = get_backend("pay")(self.payment)
        self.assertIsNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")
        self.assertIn("Thank you for your payment", mail.outbox[0].body)
        self.assertNotIn("You are the heart of Weblate", mail.outbox[0].body)

    def test_donation_pay(self) -> None:
        self.payment.extra = {"category": "donate"}
        self.payment.save(update_fields=["extra"])
        backend = get_backend("pay")(self.payment)
        self.assertIsNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")
        self.assertIn("You are the heart of Weblate", mail.outbox[0].body)
        self.assertNotIn("Thank you for your payment", mail.outbox[0].body)

    def test_reject(self) -> None:
        backend = get_backend("reject")(self.payment)
        self.assertIsNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertFalse(backend.complete(None))
        self.check_payment(Payment.REJECTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    def test_pending(self) -> None:
        backend = get_backend("pending")(self.payment)
        self.assertIsNotNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)

    def test_assertions(self) -> None:
        backend = get_backend("pending")(self.payment)
        backend.payment.state = Payment.PENDING
        with self.assertRaises(InvalidState):
            backend.initiate(None, "", "")
        backend.payment.state = Payment.ACCEPTED
        with self.assertRaises(InvalidState):
            backend.complete(None)

    @responses.activate
    def test_list(self) -> None:
        backends = list_backends()
        self.assertGreater(len(backends), 0)
        self.assertNotIn("manual", {backend.name for backend in backends})

    def test_manual_backend(self) -> None:
        backend_class = get_backend("manual")
        self.assertEqual(str(backend_class.verbose), "Manual payment")
        self.assertEqual(self.payment.get_payment_description(), "")

        self.payment.backend = "manual"
        self.payment.save(update_fields=["backend"])
        self.assertEqual(self.payment.get_payment_description(), "Manual payment")

        backend = backend_class(self.payment)
        with self.assertRaises(PaymentError):
            backend.initiate(None, "", "")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_proforma(self) -> None:
        mock_vies()
        cnb_mock_rates()
        backend = get_backend("fio-bank")(self.payment)
        self.assertIsNotNone(backend.initiate(None, "", "/complete/"))
        self.check_payment(Payment.PENDING)
        self.assertFalse(backend.complete(None))
        self.check_payment(Payment.PENDING)
        responses.add(responses.GET, FIO_API, body=json.dumps(FIO_TRASACTIONS))
        FioBank.fetch_payments()
        self.check_payment(Payment.PENDING)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your pending payment on weblate.org")
        mail.outbox = []

        received = deepcopy(FIO_TRASACTIONS)
        self.assertIsNotNone(backend.payment.draft_invoice)
        proforma_id = cast("Invoice", backend.payment.draft_invoice).number
        transaction = received["accountStatement"]["transactionList"]["transaction"]  # type: ignore[index]
        transaction[0]["column16"]["value"] = proforma_id
        transaction[1]["column16"]["value"] = proforma_id
        transaction[1]["column1"]["value"] = backend.payment.amount * 1.21
        responses.replace(responses.GET, FIO_API, body=json.dumps(received))
        FioBank.fetch_payments()
        payment = self.check_payment(Payment.ACCEPTED)
        self.maxDiff = None
        self.assertEqual(
            payment.details["transaction"]["recipient_message"], proforma_id
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")
        mail.outbox = []

        FioBank.fetch_payments()
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank(self, format_string="{}") -> None:
        mock_vies()
        cnb_mock_rates()
        customer = Customer.objects.create(**CUSTOMER)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        invoice.invoiceitem_set.create(
            description="Test item",
            unit_price=100,
        )
        invoice.generate_files()

        responses.add(responses.GET, FIO_API, body=json.dumps(FIO_TRASACTIONS))
        FioBank.fetch_payments()
        self.assertFalse(invoice.paid_payment_set.exists())
        self.assertEqual(len(mail.outbox), 0)

        received = deepcopy(FIO_TRASACTIONS)
        transaction = received["accountStatement"]["transactionList"]["transaction"]  # type: ignore[index]
        payment_message = format_string.format(invoice.number)
        transaction[0]["column16"]["value"] = payment_message
        transaction[1]["column16"]["value"] = payment_message
        transaction[1]["column1"]["value"] = int(invoice.total_amount)
        responses.replace(responses.GET, FIO_API, body=json.dumps(received))
        FioBank.fetch_payments()
        self.assertTrue(invoice.paid_payment_set.exists())
        self.assertTrue(invoice.path.exists())
        self.assertTrue(invoice.receipt_path.exists())
        payment = invoice.paid_payment_set.get()
        self.maxDiff = None
        self.assertEqual(
            payment.details["transaction"]["recipient_message"], payment_message
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")
        mail.outbox = []

        FioBank.fetch_payments()
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_vs(self) -> None:
        # Czech bank notation
        self.test_invoice_bank(format_string="VS{}/SS/KS")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_in_text(self) -> None:
        # Czech bank notation
        self.test_invoice_bank(format_string="PROFORMA{}PAYMENT")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_url(self) -> None:
        invoice = self.create_invoice()

        url = cast("str", invoice.get_payment_url())
        self.assertIsNotNone(url)

        # Trigger payment what creates an empty payment object
        self.client.get(url, follow=True)
        placeholder = invoice.draft_payment_set.get()
        self.assertEqual(placeholder.backend, "")
        self.assertEqual(placeholder.state, Payment.NEW)

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()
        self.assertTrue(invoice.paid_payment_set.exists())
        payment = invoice.paid_payment_set.get()
        self.maxDiff = None
        self.assertEqual(
            payment.details["transaction"]["recipient_message"], invoice.number
        )
        placeholder.refresh_from_db()
        self.assertEqual(placeholder.backend, "")
        self.assertEqual(placeholder.state, Payment.NEW)
        response = self.client.get(placeholder.get_payment_url())
        self.assertRedirects(response, "/en/", fetch_redirect_response=False)
        response = self.client.post(
            placeholder.get_payment_url(), {"method": "thepay2-card"}
        )
        self.assertRedirects(response, "/en/", fetch_redirect_response=False)
        placeholder.refresh_from_db()
        self.assertEqual(placeholder.backend, "")
        self.assertEqual(placeholder.state, Payment.NEW)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")
        mail.outbox = []

        FioBank.fetch_payments()
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_ignores_other_draft_backends(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        card_payment.state = Payment.PENDING
        card_payment.details["pay_url"] = "https://gate.thepay.cz/12345/pay"
        card_payment.save(update_fields=["state", "details"])

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()

        payment = invoice.paid_payment_set.get()
        self.assertNotEqual(payment.pk, card_payment.pk)
        self.assertEqual(payment.backend, "fio-bank")
        self.assertEqual(payment.state, Payment.ACCEPTED)
        self.assertEqual(
            payment.details["transaction"]["recipient_message"], invoice.number
        )
        card_payment.refresh_from_db()
        self.assertEqual(card_payment.backend, "thepay2-card")
        self.assertEqual(card_payment.state, Payment.PENDING)
        self.assertNotIn("reject_reason", card_payment.details)
        self.assertIsNone(card_payment.paid_invoice)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_reuses_matching_draft_backend(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        old_fio_payment = invoice.create_payment(backend="fio-bank")
        old_fio_payment.state = Payment.PENDING
        old_fio_payment.save(update_fields=["state"])
        fio_payment = invoice.create_payment(backend="fio-bank")
        fio_payment.state = Payment.PENDING
        fio_payment.save(update_fields=["state"])

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()

        payment = invoice.paid_payment_set.get()
        self.assertEqual(payment.pk, fio_payment.pk)
        self.assertEqual(payment.backend, "fio-bank")
        card_payment.refresh_from_db()
        self.assertEqual(card_payment.backend, "thepay2-card")
        self.assertEqual(card_payment.state, Payment.NEW)
        self.assertNotIn("reject_reason", card_payment.details)
        self.assertIsNone(card_payment.paid_invoice)
        old_fio_payment.refresh_from_db()
        self.assertEqual(old_fio_payment.state, Payment.PENDING)
        self.assertNotIn("reject_reason", old_fio_payment.details)
        self.assertIsNone(old_fio_payment.paid_invoice)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    @override_settings(**THEPAY2_MOCK_SETTINGS)
    def test_late_card_completion_after_bank_payment_records_duplicate(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        card_payment.state = Payment.PENDING
        card_payment.save(update_fields=["state"])

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()
        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.PENDING)
        self.assertNotIn("reject_reason", card_payment.details)
        self.assertIsNone(card_payment.paid_invoice)
        mail.outbox = []

        thepay_mock_payment(card_payment.pk)
        backend = get_backend("thepay2-card")(card_payment)
        self.assertFalse(backend.complete(None))

        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.REJECTED)
        self.assertEqual(card_payment.details["reject_reason"], "Invoice already paid")
        self.assertIsNone(card_payment.paid_invoice)
        self.assertEqual(invoice.paid_payment_set.count(), 1)
        interaction = self.customer.interaction_set.get(
            origin=Interaction.Origin.MANUAL_NOTE
        )
        self.assertEqual(
            interaction.summary,
            f"Duplicate payment for paid invoice {invoice.number}",
        )
        self.assertEqual(
            interaction.details["duplicate_payment_backend"], "thepay2-card"
        )
        self.assertEqual(interaction.details["existing_payment_backend"], "fio-bank")
        self.customer.refresh_from_db()
        self.assertEqual(
            self.customer.follow_up_note,
            f"Duplicate payment for paid invoice {invoice.number}",
        )
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    @override_settings(**THEPAY2_MOCK_SETTINGS)
    def test_late_card_failure_after_bank_payment_skips_failed_mail(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        card_payment.state = Payment.PENDING
        card_payment.save(update_fields=["state"])

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()
        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.PENDING)
        mail.outbox = []

        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{card_payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "uid": str(card_payment.pk),
                "project_id": 1,
                "order_id": "CZ12131415",
                "state": "error",
                "events": [
                    {
                        "occured_at": "2021-04-20T11:05:49.000000Z",
                        "type": "payment_cancelled",
                    }
                ],
            },
        )
        backend = get_backend("thepay2-card")(card_payment)
        self.assertFalse(backend.complete(None))

        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.REJECTED)
        self.assertEqual(card_payment.details["reject_reason"], "Payment cancelled")
        self.assertIsNone(card_payment.paid_invoice)
        self.assertEqual(invoice.paid_payment_set.count(), 1)
        self.assertFalse(
            self.customer.interaction_set.filter(
                origin=Interaction.Origin.MANUAL_NOTE
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    @override_settings(**THEPAY2_MOCK_SETTINGS)
    def test_late_pending_card_after_bank_payment_hides_payment_link(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        card_payment.state = Payment.PENDING
        card_payment.details["pay_url"] = "https://gate.thepay.cz/12345/pay"
        card_payment.save(update_fields=["state", "details"])

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()
        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.PENDING)
        mail.outbox = []

        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{card_payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "uid": str(card_payment.pk),
                "project_id": 1,
                "order_id": "CZ12131415",
                "state": "waiting_for_payment",
            },
        )
        response = self.client.get(f"/en/payment/{card_payment.pk}/complete/")
        self.assertRedirects(response, "/en/", fetch_redirect_response=False)

        card_payment.refresh_from_db()
        self.assertEqual(card_payment.state, Payment.PENDING)
        self.assertEqual(
            card_payment.details["pay_url"], "https://gate.thepay.cz/12345/pay"
        )
        self.assertIsNone(card_payment.paid_invoice)
        self.assertEqual(invoice.paid_payment_set.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_defers_duplicate_when_transfer_pays_other_invoice(
        self,
    ) -> None:
        paid_invoice = self.create_invoice()
        card_payment = paid_invoice.create_payment(backend="thepay2-card")
        get_backend("thepay2-card")(card_payment).success()
        unpaid_invoice = self.create_invoice()
        mail.outbox = []

        self.mock_fio_payment(
            unpaid_invoice,
            payment_message=f"{paid_invoice.number} {unpaid_invoice.number}",
        )
        FioBank.fetch_payments()

        self.assertEqual(paid_invoice.paid_payment_set.count(), 1)
        self.assertTrue(unpaid_invoice.paid_payment_set.exists())
        self.assertFalse(
            self.customer.interaction_set.filter(
                origin=Interaction.Origin.MANUAL_NOTE
            ).exists()
        )
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.follow_up_at)
        self.assertEqual(self.customer.follow_up_note, "")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_records_duplicate_payment(self) -> None:
        invoice = self.create_invoice()
        previous_follow_up_at = timezone.now()
        self.customer.follow_up_at = previous_follow_up_at
        self.customer.follow_up_note = "Check renewal"
        self.customer.save(update_fields=["follow_up_at", "follow_up_note"])
        card_payment = invoice.create_payment(backend="thepay2-card")
        get_backend("thepay2-card")(card_payment).success()
        card_payment.refresh_from_db()
        mail.outbox = []

        self.mock_fio_payment(invoice)
        FioBank.fetch_payments()

        self.assertEqual(invoice.paid_payment_set.count(), 1)
        interaction = self.customer.interaction_set.get(
            origin=Interaction.Origin.MANUAL_NOTE
        )
        self.assertEqual(interaction.origin, Interaction.Origin.MANUAL_NOTE)
        self.assertEqual(
            interaction.summary,
            f"Duplicate bank transfer for paid invoice {invoice.number}",
        )
        self.assertEqual(interaction.details["invoice"], invoice.number)
        self.assertEqual(
            interaction.details["existing_payment_id"], str(card_payment.pk)
        )
        self.assertEqual(
            interaction.details["existing_payment_backend"], "thepay2-card"
        )
        self.assertEqual(
            interaction.details["transaction"]["recipient_message"], invoice.number
        )
        self.assertEqual(
            interaction.details["follow_up_note"],
            f"Duplicate bank transfer for paid invoice {invoice.number}",
        )
        self.assertEqual(
            interaction.details["previous_follow_up_at"],
            previous_follow_up_at.isoformat(),
        )
        self.assertEqual(
            interaction.details["previous_follow_up_note"], "Check renewal"
        )
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.follow_up_at)
        self.assertLessEqual(self.customer.follow_up_at, timezone.now())
        self.assertEqual(
            self.customer.follow_up_note,
            f"Duplicate bank transfer for paid invoice {invoice.number}",
        )
        self.assertEqual(len(mail.outbox), 0)

        FioBank.fetch_payments()
        self.assertEqual(
            self.customer.interaction_set.filter(
                origin=Interaction.Origin.MANUAL_NOTE
            ).count(),
            1,
        )
        self.assertEqual(invoice.paid_payment_set.count(), 1)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_skips_duplicate_with_wrong_amount(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        get_backend("thepay2-card")(card_payment).success()
        mail.outbox = []

        self.mock_fio_payment(invoice, amount=int(invoice.total_amount) - 1)
        FioBank.fetch_payments()

        self.assertEqual(invoice.paid_payment_set.count(), 1)
        self.assertFalse(
            self.customer.interaction_set.filter(
                origin=Interaction.Origin.MANUAL_NOTE
            ).exists()
        )
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.follow_up_at)
        self.assertEqual(self.customer.follow_up_note, "")
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_skips_duplicate_with_wrong_currency(self) -> None:
        invoice = self.create_invoice()
        card_payment = invoice.create_payment(backend="thepay2-card")
        get_backend("thepay2-card")(card_payment).success()
        mail.outbox = []

        self.mock_fio_payment(invoice, currency="USD")
        FioBank.fetch_payments()

        self.assertEqual(invoice.paid_payment_set.count(), 1)
        self.assertFalse(
            self.customer.interaction_set.filter(
                origin=Interaction.Origin.MANUAL_NOTE
            ).exists()
        )
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.follow_up_at)
        self.assertEqual(self.customer.follow_up_note, "")
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank_records_fallback_duplicate_from_different_account(
        self,
    ) -> None:
        invoice = self.create_invoice()
        self.mock_fio_payment(invoice, sender_account="111111", transaction_id=None)
        FioBank.fetch_payments()
        payment = invoice.paid_payment_set.get()
        self.assertEqual(payment.backend, "fio-bank")
        self.assertEqual(payment.details["transaction_currency"], "EUR")
        mail.outbox = []

        self.mock_fio_payment(
            invoice,
            sender_account="222222",
            transaction_id=None,
            replace=True,
        )
        FioBank.fetch_payments()

        self.assertEqual(invoice.paid_payment_set.count(), 1)
        interaction = self.customer.interaction_set.get(
            origin=Interaction.Origin.MANUAL_NOTE
        )
        self.assertEqual(
            interaction.summary,
            f"Duplicate bank transfer for paid invoice {invoice.number}",
        )
        self.assertEqual(interaction.details["account_number"], "222222")
        self.assertEqual(interaction.details["currency"], "EUR")
        self.assertEqual(len(mail.outbox), 0)


@override_settings(**THEPAY2_MOCK_SETTINGS)
class ThePay2Test(BackendBaseTestCase):
    backend_name = "thepay2-card"

    def setUp(self) -> None:
        super().setUp()
        self.backend = self.payment.get_payment_backend()

    @responses.activate
    def test_pay(self) -> None:
        thepay_mock_create_payment()
        thepay_mock_payment(self.payment.pk)
        response = self.backend.initiate(None, "", "")
        self.assertIsNotNone(response)
        self.assertRedirects(
            response,
            "https://gate.thepay.cz/12345/pay",
            fetch_redirect_response=False,
        )
        self.check_payment(Payment.PENDING)
        self.assertTrue(self.backend.complete(None))
        self.check_payment(Payment.ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    @responses.activate
    def test_unpaid(self) -> None:
        thepay_mock_create_payment()
        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{self.payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "uid": "efd7d8e6-2fa3-3c46-b475-51762331bf56",
                "project_id": 1,
                "order_id": "CZ12131415",
                "state": "waiting_for_payment",
            },
        )
        response = self.backend.initiate(None, "", "")
        self.assertIsNotNone(response)
        self.assertRedirects(
            response,
            "https://gate.thepay.cz/12345/pay",
            fetch_redirect_response=False,
        )
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        self.check_payment(Payment.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    def test_payment_cancelled(self) -> None:
        thepay_mock_create_payment()
        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{self.payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "uid": "efd7d8e6-2fa3-3c46-b475-51762331bf56",
                "project_id": 1,
                "order_id": "CZ12131415",
                "state": "error",
                "events": [
                    {
                        "occured_at": "2021-04-20T11:05:49.000000Z",
                        "type": "payment_cancelled",
                    }
                ],
            },
        )
        response = self.backend.initiate(None, "", "")
        self.assertIsNotNone(response)
        self.assertRedirects(
            response,
            "https://gate.thepay.cz/12345/pay",
            fetch_redirect_response=False,
        )
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        payment = self.check_payment(Payment.REJECTED)
        self.assertEqual(payment.details["reject_reason"], "Payment cancelled")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    @responses.activate
    def test_payment_error(self) -> None:
        thepay_mock_create_payment()
        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{self.payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "uid": "efd7d8e6-2fa3-3c46-b475-51762331bf56",
                "project_id": 1,
                "order_id": "CZ12131415",
                "state": "error",
            },
        )
        response = self.backend.initiate(None, "", "")
        self.assertIsNotNone(response)
        self.assertRedirects(
            response,
            "https://gate.thepay.cz/12345/pay",
            fetch_redirect_response=False,
        )
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        payment = self.check_payment(Payment.REJECTED)
        self.assertEqual(payment.details["reject_reason"], "error")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    @responses.activate
    def test_error(self) -> None:
        responses.post(
            "https://demo.api.thepay.cz/v1/projects/42/payments",
            status=400,
        )
        with self.assertRaises(PaymentError):
            self.backend.initiate(None, "", "")
        self.check_payment(Payment.NEW)

    @responses.activate
    def test_error_message(self) -> None:
        responses.post(
            "https://demo.api.thepay.cz/v1/projects/42/payments",
            json={
                "message": "Something wrong",
            },
            status=400,
        )
        with self.assertRaises(PaymentError):
            self.backend.initiate(None, "", "")
        self.check_payment(Payment.NEW)

    @responses.activate
    def test_error_no_message(self) -> None:
        responses.post(
            "https://demo.api.thepay.cz/v1/projects/42/payments",
            json={},
            status=400,
        )
        with self.assertRaises(PaymentError):
            self.backend.initiate(None, "", "")
        self.check_payment(Payment.NEW)

    @responses.activate
    def test_error_collect(self) -> None:
        thepay_mock_create_payment()
        responses.get(
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{self.payment.pk}?merchant_id=00000000-0000-0000-0000-000000000000",
            status=401,
        )
        response = self.backend.initiate(None, "", "")
        self.assertIsNotNone(response)
        self.assertRedirects(
            response,
            "https://gate.thepay.cz/12345/pay",
            fetch_redirect_response=False,
        )
        self.check_payment(Payment.PENDING)
        with self.assertRaises(PaymentError):
            self.backend.complete(None)
        self.check_payment(Payment.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    @responses.activate
    def test_pay_recurring(self) -> None:
        # Modify backend payment as it has a copy
        self.backend.payment.repeat = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description="Test Item",
            backend=self.backend_name,
        )
        self.backend.payment.save()
        responses.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{self.backend.payment.repeat.pk}/savedauthorization?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "state": "paid",
                "parent": {"recurring_payments_available": True},
            },
        )
        self.assertIsNone(self.backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(self.backend.complete(None))
        self.check_payment(Payment.ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    @responses.activate
    def test_pay_recurring_error(self) -> None:
        # Modify backend payment as it has a copy
        self.backend.payment.repeat = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description="Test Item",
            backend=self.backend_name,
        )
        self.backend.payment.save()
        responses.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{self.backend.payment.repeat.pk}/savedauthorization?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "state": "error",
                "message": "Failed card",
                "parent": {"recurring_payments_available": True},
            },
        )
        self.assertIsNone(self.backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        payment = self.check_payment(Payment.REJECTED)
        self.assertEqual(payment.details["reject_reason"], "Failed card")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    @responses.activate
    def test_pay_recurring_error_blank_message(self) -> None:
        # Modify backend payment as it has a copy
        self.backend.payment.repeat = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description="Test Item",
            backend=self.backend_name,
        )
        self.backend.payment.save()
        responses.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{self.backend.payment.repeat.pk}/savedauthorization?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "state": "error",
                "message": "",
                "parent": {"recurring_payments_available": False},
            },
        )
        self.assertIsNone(self.backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        payment = self.check_payment(Payment.REJECTED)
        self.assertEqual(
            payment.details["reject_reason"], "Recurring payment is no longer available"
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    @responses.activate
    def test_pay_recurring_error_no_message(self) -> None:
        # Modify backend payment as it has a copy
        self.backend.payment.repeat = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description="Test Item",
            backend=self.backend_name,
        )
        self.backend.payment.save()
        responses.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{self.backend.payment.repeat.pk}/savedauthorization?merchant_id=00000000-0000-0000-0000-000000000000",
            json={
                "state": "error",
            },
        )
        self.assertIsNone(self.backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertFalse(self.backend.complete(None))
        payment = self.check_payment(Payment.REJECTED)
        self.assertEqual(payment.details["reject_reason"], "Recurring payment failed")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")


class VATTest(SimpleTestCase):
    @responses.activate
    def test_validation_invalid(self) -> None:
        mock_vies(valid=False)
        with self.assertRaises(ValidationError):
            validate_vatin("XX123456")
        with self.assertRaises(ValidationError):
            validate_vatin("CZ123456")
        with self.assertRaises(ValidationError) as error:
            validate_vatin("CZ8003280317")
        self.assertEqual(error.exception.code, "Invalid VAT")

    @responses.activate
    def test_cache(self) -> None:
        cache.set(
            "VAT-CZ8003280318",
            {
                "countryCode": "CZ",
                "vatNumber": "8003280318",
                "requestDate": date(2020, 3, 20),
                "valid": True,
                "name": "Ing. Michal Čihař",
                "address": "Zdiměřická 1439/8\nPRAHA 11 - CHODOV\n149 00  PRAHA 415",
            },
        )
        validate_vatin("CZ8003280318")

    @responses.activate
    def test_direct(self) -> None:
        mock_vies()
        cache.delete("VAT-CZ8003280318")
        validate_vatin("CZ8003280318")
