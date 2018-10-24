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

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from weblate.billing.models import Plan, Billing
from weblate.trans.tests.utils import create_test_user

from wlhosted.payments.models import Customer, Payment


class PaymentTest(TestCase):
    def setUp(self):
        Payment.objects.all().delete()
        Customer.objects.all().delete()
        self.user = create_test_user()
        self.client.login(username='testuser', password='testpassword')
        self.plan_a = Plan.objects.create(
            name='Plan A', price=19, yearly_price=199, public=True
        )
        self.plan_b = Plan.objects.create(
            name='Plan B', price=49, yearly_price=499, public=True
        )
        self.plan_c = Plan.objects.create(
            name='Plan C', price=9, yearly_price=99, public=False
        )
        self.plan_d = Plan.objects.create(
            name='Plan D', price=0, yearly_price=0, public=True
        )

    @override_settings(PAYMENT_REDIRECT_URL='http://example.com/payment')
    def create_payment(self, **kwargs):
        params = {
            'plan': self.plan_a.id,
            'period': 'y',
        }
        params.update(kwargs)
        response = self.client.post(reverse('create-billing'), params)
        self.assertRedirects(
            response,
            'http://example.com/payment',
            fetch_redirect_response=False
        )

    def create_trial(self):
        bill = Billing.objects.create(
            state=Billing.STATE_TRIAL,
            plan=self.plan_b,
        )
        bill.owners.add(self.user)
        return bill

    def test_create(self):
        response = self.client.get(reverse('create-billing'))
        self.assertContains(response, 'Plan A')
        self.assertContains(response, 'Plan B')
        self.assertNotContains(response, 'Plan C')
        self.assertNotContains(response, 'Plan D')
        self.create_payment(period='y')
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.all()[0]
        self.assertEqual(payment.amount, self.plan_a.yearly_price)
        self.assertEqual(payment.extra, {'plan': self.plan_a.pk})
        self.create_payment(period='m')
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.exclude(uuid=payment.uuid)[0]
        self.assertEqual(payment.amount, self.plan_a.price)

    def test_trial(self):
        bill = self.create_trial()
        response = self.client.get(reverse('create-billing'))
        self.assertContains(response, 'Trial')
        self.create_payment(billing=bill.pk)
        payment = Payment.objects.all()[0]
        self.assertEqual(
            payment.extra,
            {'billing': bill.pk, 'plan': self.plan_a.pk}
        )

    def do_complete(self, **kwargs):
        self.create_payment(**kwargs)
        payment = Payment.objects.all()[0]
        params = {'payment': payment.uuid}
        params.update(kwargs)
        self.assertRedirects(
            self.client.get(reverse('create-billing'), params),
            reverse('create-billing')
        )
        payment.paid = True
        payment.save()
        self.assertRedirects(
            self.client.get(reverse('create-billing'), params),
            reverse('billing')
        )

    def test_complete(self):
        self.do_complete()
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_trial(self):
        bill = self.create_trial()
        self.do_complete(billing=bill.pk)
        bill = Billing.objects.get(pk=bill.pk)
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)
