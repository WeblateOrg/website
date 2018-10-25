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


from dateutil.relativedelta import relativedelta

from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone
from django.urls import reverse

from weblate.billing.models import Plan, Billing, Invoice
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

    def test_existing_billing(self):
        bill = self.create_trial()
        bill_args = {'billing': bill.pk}
        # Test default selection
        response = self.client.get(reverse('create-billing'))
        self.assertContains(response, 'Trial')
        # Test manual selection
        response = self.client.get(reverse('create-billing'), bill_args)
        self.assertContains(response, 'Trial')
        # Test invalid selection
        response = self.client.get(
            reverse('create-billing'), {'billing': 'x'}
        )
        self.assertNotContains(response, 'Trial')
        # Create payment for billing
        self.create_payment(**bill_args)
        payment = Payment.objects.all()[0]
        bill_args['plan'] = self.plan_a.pk
        # The billing should be stored in the payment
        self.assertEqual(payment.extra, bill_args)

    def test_error_handling(self):
        response = self.client.post(reverse('create-billing'))
        self.assertContains(response, 'This field is required')

        with override_settings(PAYMENT_ENABLED=False):
            response = self.client.post(
                reverse('create-billing'),
                {
                    'plan': self.plan_a.id,
                    'period': 'y',
                }
            )
            self.assertRedirects(response, reverse('create-billing'))

    @override_settings(PAYMENT_REDIRECT_URL='http://example.com/payment')
    def test_payment_redirects(self):
        # Invalid UUID
        self.assertRedirects(
            self.client.get(reverse('create-billing'), {'payment': 'i'}),
            reverse('create-billing')
        )
        self.create_payment()
        payment = Payment.objects.all()[0]
        bill_url = reverse('billing')
        create_url = reverse('create-billing')
        pay_url = 'http://example.com/payment'
        params = create_url, {'payment': payment.uuid}
        # New should redirect to payment interface
        self.assertRedirects(
            self.client.get(*params), pay_url,
            fetch_redirect_response=False
        )
        # Pending should redirect to billings
        payment.state = Payment.PENDING
        payment.save()
        self.assertRedirects(self.client.get(*params), bill_url)
        # Accepted should redirect to billings
        payment.state = Payment.ACCEPTED
        payment.save()
        self.assertRedirects(self.client.get(*params), bill_url)
        # Processed should redirect to billings
        payment.state = Payment.PROCESSED
        payment.save()
        self.assertRedirects(self.client.get(*params), bill_url)
        # Rejected should redirect to create
        payment.state = Payment.REJECTED
        payment.save()
        self.assertRedirects(self.client.get(*params), create_url)
        # Non existing should redirect to create
        payment.delete()
        self.assertRedirects(self.client.get(*params), create_url)

    def do_complete(self, **kwargs):
        self.create_payment(**kwargs)
        payment = Payment.objects.all()[0]
        payment.state = Payment.ACCEPTED
        payment.save()
        self.assertRedirects(
            self.client.get(
                reverse('create-billing'), {'payment': payment.uuid}
            ),
            reverse('billing')
        )

    def test_complete(self):
        self.do_complete()
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_monthly(self):
        self.do_complete(period='m')
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_trial(self):
        bill = self.create_trial()
        self.do_complete(billing=bill.pk)
        bill = Billing.objects.get(pk=bill.pk)
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_second(self):
        bill = self.create_trial()
        now = timezone.now()
        Invoice.objects.create(
            billing=bill,
            start=now,
            end=now + relativedelta(months=1),
        )
        old_i = bill.invoice_set.all()[0]
        self.do_complete(billing=bill.pk)
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)
        self.assertEqual(bill.invoice_set.count(), 2)
        new_i = bill.invoice_set.exclude(pk=old_i.pk)[0]
        self.assertLess(old_i.end, new_i.start)
