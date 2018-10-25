# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
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

from __future__ import unicode_literals

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings

from wlhosted.payments.models import Customer, Payment
from wlhosted.payments.backends import (
    get_backend, InvalidState, list_backends
)


CUSTOMER = {
    'name': 'Michal Čihař',
    'address': 'Zdiměřická 1439',
    'city': '149 00 Praha 4',
    'country': 'CZ',
    'vat': 'CZ8003280318',
    'user_id': 6,
}


class ModelTest(SimpleTestCase):
    def test_vat(self):
        customer = Customer()
        self.assertFalse(customer.needs_vat)
        customer = Customer(**CUSTOMER)
        # Czech customer needs VAT
        self.assertTrue(customer.needs_vat)
        # EU enduser needs VAT
        customer.vat = ''
        self.assertTrue(customer.needs_vat)
        # EU company does not need VAT
        customer.vat = 'IE6388047V'
        self.assertFalse(customer.needs_vat)
        # Non EU customer does not need VAT
        customer.vat = ''
        customer.country = 'US'
        self.assertFalse(customer.needs_vat)

    def test_empty(self):
        customer = Customer(country='CZ')
        self.assertTrue(customer.is_empty)
        customer = Customer(**CUSTOMER)
        self.assertFalse(customer.is_empty)

    def test_clean(self):
        customer = Customer(**CUSTOMER)
        customer.clean()
        customer.country = 'IE'
        with self.assertRaises(ValidationError):
            customer.clean()

    def test_vat(self):
        customer = Customer(**CUSTOMER)
        payment = Payment(customer=customer, amount=100)
        self.assertEqual(payment.vat_amount, 121)

        customer.vat = 'IE6388047V'
        payment = Payment(customer=customer, amount=100)
        self.assertEqual(payment.vat_amount, 100)


class BackendTest(TestCase):
    def setUp(self):
        super(BackendTest, self).setUp()
        self.customer = Customer.objects.create(**CUSTOMER)
        self.payment = Payment.objects.create(
            customer=self.customer,
            amount=100,
            description='Test Item'
        )

    def check_payment(self, state):
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.state, state)

    @override_settings(PAYMENT_DEBUG=True)
    def test_pay(self):
        backend = get_backend('pay')(self.payment)
        self.assertIsNone(backend.initiate(None, '', ''))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)

    @override_settings(PAYMENT_DEBUG=True)
    def test_reject(self):
        backend = get_backend('reject')(self.payment)
        self.assertIsNone(backend.initiate(None, '', ''))
        self.check_payment(Payment.PENDING)
        self.assertFalse(backend.complete(None))
        self.check_payment(Payment.REJECTED)

    @override_settings(PAYMENT_DEBUG=True)
    def test_pending(self):
        backend = get_backend('pending')(self.payment)
        self.assertIsNotNone(backend.initiate(None, '', ''))
        self.check_payment(Payment.PENDING)
        self.assertTrue(backend.complete(None))
        self.check_payment(Payment.ACCEPTED)

    @override_settings(PAYMENT_DEBUG=True)
    def test_assertions(self):
        backend = get_backend('pending')(self.payment)
        backend.payment.state = Payment.PENDING
        with self.assertRaises(InvalidState):
            backend.initiate(None, '', '')
        backend.payment.state = Payment.ACCEPTED
        with self.assertRaises(InvalidState):
            backend.complete(None)

    @override_settings(PAYMENT_DEBUG=True)
    def test_list(self):
        backends = list_backends()
        self.assertGreater(len(backends), 0)
