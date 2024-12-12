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
from datetime import date
from typing import cast

import responses
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings

from weblate_web.invoices.models import Invoice, InvoiceCategory, InvoiceKind
from weblate_web.tests import (
    THEPAY2_MOCK_SETTINGS,
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
    def test_vat(self):
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

    def test_empty(self):
        customer = Customer(country="CZ")
        self.assertTrue(customer.is_empty)
        customer = Customer(**CUSTOMER)
        self.assertFalse(customer.is_empty)
        customer.vat = None
        self.assertFalse(customer.is_empty)
        customer.postcode = ""
        self.assertTrue(customer.is_empty)

    def test_clean(self):
        customer = Customer(**CUSTOMER)
        customer.clean()
        customer.country = "IE"
        with self.assertRaises(ValidationError):
            customer.clean()

    def test_vat_calculation(self):
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

    def test_short_filename(self):
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


class BackendBaseTestCase(TestCase):
    backend_name: str = ""

    def setUp(self):
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
    def test_pay(self):
        backend = get_backend("pay")(self.payment)
        self.assertIsNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")

    def test_reject(self):
        backend = get_backend("reject")(self.payment)
        self.assertIsNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertFalse(backend.complete(None))
        self.check_payment(Payment.REJECTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org failed")

    def test_pending(self):
        backend = get_backend("pending")(self.payment)
        self.assertIsNotNone(backend.initiate(None, "", ""))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)

    def test_assertions(self):
        backend = get_backend("pending")(self.payment)
        backend.payment.state = Payment.PENDING
        with self.assertRaises(InvalidState):
            backend.initiate(None, "", "")
        backend.payment.state = Payment.ACCEPTED
        with self.assertRaises(InvalidState):
            backend.complete(None)

    @responses.activate
    def test_list(self):
        backends = list_backends()
        self.assertGreater(len(backends), 0)

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_proforma(self):
        mock_vies()
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

        received = FIO_TRASACTIONS.copy()
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

    @responses.activate
    @override_settings(
        FIO_TOKEN="test-token",  # noqa: S106
    )
    def test_invoice_bank(self):
        mock_vies()
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

        responses.add(responses.GET, FIO_API, body=json.dumps(FIO_TRASACTIONS))
        FioBank.fetch_payments()
        self.assertFalse(invoice.paid_payment_set.exists())
        self.assertEqual(len(mail.outbox), 0)

        received = FIO_TRASACTIONS.copy()
        transaction = received["accountStatement"]["transactionList"]["transaction"]  # type: ignore[index]
        transaction[0]["column16"]["value"] = invoice.number
        transaction[1]["column16"]["value"] = invoice.number
        transaction[1]["column1"]["value"] = int(invoice.total_amount)
        responses.replace(responses.GET, FIO_API, body=json.dumps(received))
        FioBank.fetch_payments()
        self.assertTrue(invoice.paid_payment_set.exists())
        payment = invoice.paid_payment_set.get()
        self.maxDiff = None
        self.assertEqual(
            payment.details["transaction"]["recipient_message"], invoice.number
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your payment on weblate.org")


@override_settings(**THEPAY2_MOCK_SETTINGS)
class ThePay2Test(BackendBaseTestCase):
    backend_name = "thepay2-card"

    def setUp(self):
        super().setUp()
        self.backend = self.payment.get_payment_backend()

    @responses.activate
    def test_pay(self):
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
    def test_unpaid(self):
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
    def test_payment_cancelled(self):
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
    def test_payment_error(self):
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
    def test_error(self):
        responses.post(
            "https://demo.api.thepay.cz/v1/projects/42/payments",
            status=400,
        )
        with self.assertRaises(PaymentError):
            self.backend.initiate(None, "", "")
        self.check_payment(Payment.NEW)

    @responses.activate
    def test_error_message(self):
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
    def test_error_no_message(self):
        responses.post(
            "https://demo.api.thepay.cz/v1/projects/42/payments",
            json={},
            status=400,
        )
        with self.assertRaises(PaymentError):
            self.backend.initiate(None, "", "")
        self.check_payment(Payment.NEW)

    @responses.activate
    def test_error_collect(self):
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
    def test_pay_recurring(self):
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
    def test_pay_recurring_error(self):
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
    def test_pay_recurring_error_blank_message(self):
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
    def test_pay_recurring_error_no_message(self):
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
    def test_validation_invalid(self):
        mock_vies(valid=False)
        with self.assertRaises(ValidationError):
            validate_vatin("XX123456")
        with self.assertRaises(ValidationError):
            validate_vatin("CZ123456")
        with self.assertRaises(ValidationError):
            validate_vatin("CZ8003280317")

    @responses.activate
    def test_cache(self):
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
    def test_direct(self):
        mock_vies()
        cache.delete("VAT-CZ8003280318")
        validate_vatin("CZ8003280318")
