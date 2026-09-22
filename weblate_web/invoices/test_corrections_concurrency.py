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

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature

from weblate_web.payments.backends import FioBank, ThePay2Card
from weblate_web.payments.models import Customer, CustomerFollowUp, Payment

from .corrections import issue_correction
from .models import Currency, Invoice, InvoiceCategory, InvoiceKind


@skipUnlessDBFeature("has_select_for_update")
class CorrectionConcurrencyTest(TransactionTestCase):
    def run_corrections(self, *, repeat: bool) -> None:
        user = User.objects.create_superuser(username="concurrent-corrections")
        customer = Customer.objects.create(name="Customer", country="CZ", user_id=-1)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            currency=Currency.CZK,
            vat_rate=21,
            prepaid=True,
        )
        invoice.invoiceitem_set.create(description="Service", unit_price=Decimal(100))
        barrier = Barrier(2)
        token = uuid4()

        def issue():
            try:
                barrier.wait(timeout=10)
                return issue_correction(
                    invoice=invoice,
                    user=user,
                    amount=Decimal(80),
                    reason="price",
                    note="Cancellation",
                    issue_date=invoice.issue_date,
                    tax_date=invoice.tax_date,
                    token=token if repeat else uuid4(),
                ).pk
            except ValidationError:
                return None
            finally:
                connections.close_all()

        with (
            patch.object(Invoice, "generate_files"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(issue) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        invoice.refresh_from_db()
        self.assertEqual(invoice.credited_amount, Decimal(80))
        self.assertEqual(invoice.corrections.count(), 1)
        if repeat:
            self.assertEqual(results[0], results[1])
        else:
            self.assertEqual(results.count(None), 1)

    def test_concurrent_corrections_cannot_exceed_balance(self):
        self.run_corrections(repeat=False)

    def test_concurrent_replay_issues_one_document(self):
        self.run_corrections(repeat=True)

    @override_settings(FIO_TOKEN="test-token")  # ruff:ignore[hardcoded-password-func-arg]
    def test_bank_reconciliation_waits_for_correction(self):
        customer = Customer.objects.create(name="Customer", country="CZ", user_id=-1)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            currency=Currency.CZK,
            vat_rate=21,
        )
        invoice.invoiceitem_set.create(description="Service", unit_price=Decimal(100))
        entry = {
            "amount": Decimal(121),
            "recipient_message": invoice.number,
            "transaction_id": "concurrent-transfer",
            "date": invoice.issue_date,
            "account_number": "123456",
        }
        locking = Event()

        def signal_lock(execute, sql, params, many, context):
            if "FOR UPDATE" in sql and "invoices_invoice" in sql:
                locking.set()
            return execute(sql, params, many, context)

        def reconcile():
            try:
                with connection.execute_wrapper(signal_lock):
                    FioBank.fetch_payments()
            finally:
                connections.close_all()

        with (
            patch("weblate_web.payments.backends.fiobank.FioBank") as bank,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            bank.return_value.last_transactions.return_value = (
                {"currency": "CZK"},
                [entry],
            )
            with transaction.atomic():
                locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
                future = executor.submit(reconcile)
                self.assertTrue(locking.wait(timeout=10))
                locked.credited_amount = locked.total_amount
                locked.fully_credited = True
                locked.save(update_fields=["credited_amount", "fully_credited"])
            future.result(timeout=20)
        self.assertFalse(invoice.paid_payment_set.exists())
        self.assertFalse(invoice.draft_payment_set.exists())
        followup = customer.followups.get(type=CustomerFollowUp.Type.DUPLICATE_PAYMENT)
        self.assertEqual(Decimal(followup.details["excess_amount"]), Decimal(121))

    def run_duplicate_cancellation(self, *, correction_first: bool) -> None:
        user = User.objects.create_superuser(username="cancel-corrections")
        customer = Customer.objects.create(name="Customer", country="CZ", user_id=-1)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            currency=Currency.CZK,
        )
        invoice.invoiceitem_set.create(description="Service", unit_price=100)
        retained = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            currency=Currency.CZK,
            prepaid=True,
        )
        retained.invoiceitem_set.create(description="Service", unit_price=100)
        payment = invoice.create_payment(backend="thepay2-card")
        payment.state = Payment.PENDING
        payment.save()
        locking = Event()

        def correct():
            return issue_correction(
                invoice=invoice,
                user=user,
                amount=invoice.total_amount,
                reason="duplicate",
                note="",
                issue_date=invoice.issue_date,
                tax_date=invoice.tax_date,
                token=uuid4(),
                duplicate=retained,
                cancel_payments=True,
            )

        def complete():
            with transaction.atomic():
                return ThePay2Card(payment).complete(None)

        def signal_lock(execute, sql, params, many, context):
            if "FOR UPDATE" in sql and "invoices_invoice" in sql:
                locking.set()
            return execute(sql, params, many, context)

        def competing_action():
            try:
                with connection.execute_wrapper(signal_lock):
                    return complete() if correction_first else correct()
            except ValidationError:
                return None
            finally:
                connections.close_all()

        with (
            patch.object(Invoice, "generate_files"),
            patch.object(Invoice, "generate_receipt"),
            patch.object(ThePay2Card, "collect", return_value=True),
            patch.object(ThePay2Card, "send_notification"),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            with transaction.atomic():
                Invoice.objects.select_for_update().get(pk=invoice.pk)
                future = executor.submit(competing_action)
                self.assertTrue(locking.wait(timeout=10))
                if correction_first:
                    correct()
                else:
                    self.assertTrue(complete())
            result = future.result(timeout=20)
        payment.refresh_from_db()
        invoice.refresh_from_db()
        if correction_first:
            self.assertFalse(result)
            self.assertEqual(payment.state, Payment.CANCELLED)
            self.assertTrue(invoice.is_credited)
            self.assertTrue(
                customer.followups.filter(details__payment_id=str(payment.pk)).exists()
            )
        else:
            self.assertIsNone(result)
            self.assertEqual(payment.state, Payment.ACCEPTED)
            self.assertFalse(invoice.corrections.exists())

    def test_duplicate_cancellation_before_completion(self):
        self.run_duplicate_cancellation(correction_first=True)

    def test_duplicate_cancellation_after_completion(self):
        self.run_duplicate_cancellation(correction_first=False)
