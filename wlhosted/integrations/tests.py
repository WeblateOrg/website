#
# Copyright © 2012 - 2019 Michal Čihař <michal@cihar.com>
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

from time import sleep

import httpretty
from dateutil.relativedelta import relativedelta
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from weblate.billing.models import Billing, Invoice, Plan
from weblate.trans.tests.utils import create_test_user

from wlhosted.integrations.tasks import pending_payments, recurring_payments
from wlhosted.payments.backends import get_backend
from wlhosted.payments.models import Customer, Payment
from wlhosted.payments.tests import setup_dirs


class PaymentTest(TestCase):
    databases = "__all__"

    def setUp(self):
        Payment.objects.all().delete()
        Customer.objects.all().delete()
        self.user = create_test_user()
        self.client.login(username="testuser", password="testpassword")
        self.plan_a = Plan.objects.create(
            name="Plan A", price=19, yearly_price=199, public=True
        )
        self.plan_b = Plan.objects.create(
            name="Plan B", price=49, yearly_price=499, public=True
        )
        self.plan_c = Plan.objects.create(
            name="Plan C", price=9, yearly_price=99, public=False
        )
        self.plan_d = Plan.objects.create(
            name="Plan D", price=0, yearly_price=0, public=True
        )
        setup_dirs()

    @override_settings(PAYMENT_REDIRECT_URL="http://example.com/payment")
    def create_payment(self, **kwargs):
        params = {"plan": self.plan_a.id, "period": "y"}
        params.update(kwargs)
        response = self.client.post(reverse("create-billing"), params)
        self.assertRedirects(
            response, "http://example.com/payment", fetch_redirect_response=False
        )

    def create_trial(self):
        bill = Billing.objects.create(state=Billing.STATE_TRIAL, plan=self.plan_b)
        bill.owners.add(self.user)
        return bill

    def test_create(self):
        response = self.client.get(reverse("create-billing"))
        self.assertContains(response, "Plan A")
        self.assertContains(response, "Plan B")
        self.assertNotContains(response, "Plan C")
        self.assertNotContains(response, "Plan D")
        self.create_payment(period="y")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.all()[0]
        self.assertEqual(payment.amount, self.plan_a.yearly_price)
        self.assertEqual(payment.extra, {"plan": self.plan_a.pk, "period": "y"})
        self.create_payment(period="m")
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.exclude(uuid=payment.uuid)[0]
        self.assertEqual(payment.amount, self.plan_a.price)

    def test_pending_payments(self):
        self.test_create()
        Payment.objects.all().update(state=Payment.ACCEPTED)
        pending_payments()
        self.assertFalse(Payment.objects.filter(state=Payment.ACCEPTED).exists())

    def test_existing_billing(self):
        bill = self.create_trial()
        bill_args = {"billing": bill.pk}
        # Test default selection
        response = self.client.get(reverse("create-billing"))
        self.assertContains(response, "Trial")
        # Test manual selection
        response = self.client.get(reverse("create-billing"), bill_args)
        self.assertContains(response, "Trial")
        # Test invalid selection
        response = self.client.get(reverse("create-billing"), {"billing": "x"})
        self.assertNotContains(response, "Trial")
        # Create payment for billing
        self.create_payment(**bill_args)
        payment = Payment.objects.all()[0]
        bill_args["plan"] = self.plan_a.pk
        bill_args["period"] = "y"
        # The billing should be stored in the payment
        self.assertEqual(payment.extra, bill_args)

    def test_error_handling(self):
        response = self.client.post(reverse("create-billing"))
        self.assertContains(response, "This field is required")

        with override_settings(PAYMENT_ENABLED=False):
            response = self.client.post(
                reverse("create-billing"), {"plan": self.plan_a.id, "period": "y"}
            )
            self.assertRedirects(response, reverse("create-billing"))

    @override_settings(PAYMENT_REDIRECT_URL="http://example.com/payment")
    def test_payment_redirects(self):
        # Invalid UUID
        self.assertRedirects(
            self.client.get(reverse("create-billing"), {"payment": "i"}),
            reverse("create-billing"),
        )
        self.create_payment()
        payment = Payment.objects.all()[0]
        bill_url = reverse("billing")
        create_url = reverse("create-billing")
        pay_url = "http://example.com/payment"
        params = create_url, {"payment": payment.uuid}
        # New should redirect to payment interface
        self.assertRedirects(
            self.client.get(*params), pay_url, fetch_redirect_response=False
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
            self.client.get(reverse("create-billing"), {"payment": payment.uuid}),
            reverse("billing"),
        )

    def test_complete(self):
        self.do_complete()
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_monthly(self):
        self.do_complete(period="m")
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
            billing=bill, start=now, end=now + relativedelta(months=1), amount=10
        )
        old_i = bill.invoice_set.all()[0]
        self.do_complete(billing=bill.pk)
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)
        self.assertEqual(bill.invoice_set.count(), 2)
        new_i = bill.invoice_set.exclude(pk=old_i.pk)[0]
        self.assertLess(old_i.end, new_i.start)

    def prepare_recurring(self, method):
        self.create_payment(period="y")
        payment = Payment.objects.all()[0]

        # Complete the payment
        backend = get_backend(method)(payment)
        backend.initiate(None, "", "")
        Customer.objects.update(
            name="Michal Čihař",
            address="Zdiměřická 1439",
            city="149 00 Praha 4",
            country="CZ",
            vat="CZ8003280318",
        )
        backend.complete(None)

        self.assertRedirects(
            self.client.get(reverse("create-billing"), {"payment": payment.uuid}),
            reverse("billing"),
        )

        # Check recurrence is stored
        bill = Billing.objects.all()[0]
        invoices = bill.invoice_set.count()

        # Fake end of last invoice
        last_invoice = bill.invoice_set.order_by("-start")[0]
        last_invoice.end = timezone.now() - relativedelta(days=7)
        last_invoice.save()

        return payment, bill, invoices

    def run_recurring(self):
        # Invoke recurring payment
        httpretty.register_uri(httpretty.POST, "http://example.com/payment", body="")
        recurring_payments()

    @override_settings(
        PAYMENT_DEBUG=True, PAYMENT_REDIRECT_URL="http://example.com/payment"
    )
    @httpretty.activate
    def test_recurring(self):
        """Test recurring payments."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        self.run_recurring()

        # Complete the payment (we've faked the payment server above)
        recure_payment = Payment.objects.exclude(pk=payment.pk)[0]
        backend = get_backend("pay")(recure_payment)
        backend.initiate(None, "", "")
        backend.complete(None)

        # Process pending payments
        pending_payments()

        # There should be additional invoice on the billing
        self.assertEqual(invoices + 1, bill.invoice_set.count())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_none(self):
        """Test method without support for recurring payments."""
        # The pending method does not support recurring payments
        payment, bill, invoices = self.prepare_recurring("pending")
        self.assertNotIn("recurring", bill.payment)

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_invalid(self):
        """Test handling of invalid (removed) method."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        # Fake payment menthod
        payment.details["backend"] = "invalid"
        payment.save()

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())
        # Recurrence should be disabled
        bill = Billing.objects.get(pk=bill.pk)
        self.assertNotIn("recurring", bill.payment)

    @override_settings(
        PAYMENT_DEBUG=True, PAYMENT_REDIRECT_URL="http://example.com/payment"
    )
    @httpretty.activate
    def test_recurring_one_error(self):
        """Test handling of single failed recurring payments."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )

        self.run_recurring()

        # Complete the payment (we've faked the payment server above)
        recure_payment = Payment.objects.exclude(pk=payment.pk).exclude(amount=1)[0]
        backend = get_backend("pay")(recure_payment)
        backend.initiate(None, "", "")
        backend.complete(None)

        # Process pending payments
        pending_payments()

        # There should be additional invoice on the billing
        self.assertEqual(invoices + 1, bill.invoice_set.count())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_more_error(self):
        """Test handling of more failed recurring payments."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.PROCESSED, amount=1
        )
        # Ensure rest is after procesed one
        sleep(1)
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(
            Payment.objects.exclude(pk=payment.pk).exclude(amount=1).exists()
        )
        # Recurrence should be disabled
        bill = Billing.objects.get(pk=bill.pk)
        self.assertNotIn("recurring", bill.payment)
