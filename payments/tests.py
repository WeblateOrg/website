#
# Copyright © 2012–2023 Michal Čihař <michal@cihar.com>
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

import json
from copy import copy
from datetime import date

import responses
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings

from weblate_web.tests import TEST_FAKTURACE

from .backends import FioBank, InvalidState, get_backend, list_backends
from .models import Customer, Payment
from .validators import validate_vatin

CUSTOMER = {
    "name": "Michal Čihař",
    "address": "Zdiměřická 1439",
    "city": "149 00 Praha 4",
    "country": "CZ",
    "vat": "CZ8003280318",
    "email": "noreply@example.com",
    "user_id": 6,
}

FIO_API = "https://www.fio.cz/ib_api/rest/last/test-token/transactions.json"
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
            "currency": "CZK",
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


@override_settings(
    PAYMENT_DEBUG=True, PAYMENT_FAKTURACE=TEST_FAKTURACE, FIO_TOKEN="test-token"
)
class BackendTest(TestCase):
    databases = "__all__"

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(**CUSTOMER)
        self.payment = Payment.objects.create(
            customer=self.customer, amount=100, description="Test Item"
        )

    def check_payment(self, state):
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.state, state)
        return payment

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

    def test_list(self):
        backends = list_backends()
        self.assertGreater(len(backends), 0)

    @responses.activate
    def test_proforma(self):
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

        received = copy(FIO_TRASACTIONS)
        proforma_id = backend.payment.invoice
        transaction = received["accountStatement"]["transactionList"]["transaction"]
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


class VATTest(SimpleTestCase):
    def test_validation_invalid(self):
        with self.assertRaises(ValidationError):
            validate_vatin("XX123456")
        with self.assertRaises(ValidationError):
            validate_vatin("CZ123456")
        with self.assertRaises(ValidationError):
            validate_vatin("CZ8003280317")

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

    def test_direct(self):
        # This test relies on the VAT validation service being alive
        # so we accept failure as well
        cache.delete("VAT-CZ8003280318")
        try:
            validate_vatin("CZ8003280318")
        except ValidationError as error:
            self.assertIn("service unavailable", str(error))
