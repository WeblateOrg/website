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
from django.test import SimpleTestCase

from wlhosted.payments.models import Customer, Payment


CUSTOMER = {
    'name': 'Michal Čihař',
    'address': 'Zdiměřická 1439',
    'city': '149 00 Praha 4',
    'country': 'CZ',
    'vat': 'CZ8003280318',
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
