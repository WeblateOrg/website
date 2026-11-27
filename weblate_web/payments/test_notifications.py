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
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from smtplib import SMTPException
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import responses
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError, connections
from django.test import (
    Client,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import get_language, override
from pypdf import PdfReader

from weblate_web.categories import ServiceCategory
from weblate_web.invoices.models import Invoice, InvoiceKind
from weblate_web.models import Subscription
from weblate_web.tests import (
    THEPAY2_MOCK_SETTINGS,
    thepay_mock_create_payment,
    thepay_mock_payment,
)
from weblate_web.utils import FOSDEM_ORIGIN, PAYMENTS_ORIGIN

from .backends import FioBank, ThePay2Card
from .models import Customer, Payment, ThePayNotification
from .notifications import process_thepay_notifications
from .utils import NotificationEmail


def create_payment() -> Payment:
    customer = Customer.objects.create(
        user_id=-1,
        name="Customer",
        country="BE",
        origin=PAYMENTS_ORIGIN,
        email="test@example.com",
    )
    return Payment.objects.create(
        customer=customer,
        amount=100,
        description="Test donation",
        backend="thepay2-card",
        state=Payment.PENDING,
        extra={"category": "donate"},
    )


def notification_query(payment: Payment) -> dict[str, str]:
    return {"project_id": "42", "payment_uid": str(payment.pk), "type": "state_changed"}


def thepay_calls(http: responses.RequestsMock) -> list[responses.Call]:
    return [
        call
        for call in http.calls
        if (call.request.url or "").startswith("https://demo.api.thepay.cz/")
    ]


@override_settings(**THEPAY2_MOCK_SETTINGS, LANGUAGE_CODE="en", FIO_TOKEN=[])
class ThePayNotificationTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.payment = create_payment()
        self.url = reverse("thepay-notification", kwargs={"pk": self.payment.pk})
        self.query = notification_query(self.payment)
        self.detail_url = (
            f"https://demo.api.thepay.cz/v1/projects/42/payments/{self.payment.pk}"
            "?merchant_id=00000000-0000-0000-0000-000000000000"
        )

    @responses.activate
    def test_creation_urls(self) -> None:
        with override("cs"):
            complete_url = self.payment.get_complete_url()
            backend = self.payment.get_payment_backend()
            thepay_mock_create_payment()
            backend.perform(None, "", complete_url)
            parent = create_payment()
            backend.payment.repeat = parent
            responses.post(
                f"https://demo.api.thepay.cz/v2/projects/42/payments/{parent.pk}/savedauthorization",
                json={"state": "waiting_for_confirmation"},
            )
            backend.perform(None, "", complete_url)

        checkout, recurring = [
            json.loads(call.request.body or "") for call in responses.calls
        ]
        notification_url = f"{settings.SITE_URL}/api/thepay/notify/{self.payment.pk}/"
        self.assertEqual(checkout["notif_url"], notification_url)
        self.assertEqual(recurring["notif_url"], notification_url)
        self.assertEqual(checkout["return_url"], complete_url)
        self.assertIn("/cs/payment/", complete_url)
        self.assertEqual(backend.payment.details["thepay_language"], "cs")

    @responses.activate
    def test_duplicate_delivery(self) -> None:
        thepay_mock_payment(self.payment.pk)
        for _ in range(2):
            response = self.client.get(self.url, self.query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"")
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(ThePayNotification.objects.count(), 1)
        self.assertEqual(len(responses.calls), 0)

        call_command("process_payments")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PROCESSED)
        self.assertIsNotNone(invoice := self.payment.paid_invoice_id)
        self.assertTrue(responses.calls[0].request.headers["Signature"])
        self.assertTrue(responses.calls[0].request.headers["SignatureDate"])

        self.assertEqual(self.client.get(self.url, self.query).status_code, 200)
        call_command("process_payments")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.paid_invoice_id, invoice)
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Subscription.objects.filter(payment=self.payment).count(), 1)
        self.assertFalse(ThePayNotification.objects.exists())

    @responses.activate
    def test_retry_after_lookup_error(self) -> None:
        responses.get(self.detail_url, status=503)
        thepay_mock_payment(self.payment.pk)
        other = create_payment()
        other_url = reverse("thepay-notification", kwargs={"pk": other.pk})
        thepay_mock_payment(other.pk)
        self.assertEqual(self.client.get(self.url, self.query).status_code, 200)
        self.assertEqual(
            self.client.get(other_url, notification_query(other)).status_code, 200
        )

        call_command("process_payments")
        self.payment.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PENDING)
        self.assertEqual(other.state, Payment.PROCESSED)
        work = ThePayNotification.objects.get()
        self.assertEqual(work.pk, self.payment.pk)
        self.assertGreater(work.ready_at, timezone.now())

        ThePayNotification.objects.filter(pk=work.pk).update(
            ready_at=timezone.now() - timedelta(minutes=1)
        )
        call_command("process_payments")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PROCESSED)
        self.assertFalse(ThePayNotification.objects.exists())

    @responses.activate
    @override_settings(FIO_TOKEN=["placeholder"])
    def test_email_failure_does_not_stop_payment_processing(self) -> None:
        other = create_payment()
        other_url = reverse("thepay-notification", kwargs={"pk": other.pk})
        for payment, url in ((self.payment, self.url), (other, other_url)):
            thepay_mock_payment(payment.pk)
            self.assertEqual(
                self.client.get(url, notification_query(payment)).status_code, 200
            )

        with (
            patch.object(NotificationEmail, "send", side_effect=[SMTPException, 1]),
            patch.object(FioBank, "fetch_payments") as fetch_bank_payments,
        ):
            call_command("process_payments")

        self.payment.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PENDING)
        self.assertIsNone(self.payment.paid_invoice_id)
        self.assertEqual(other.state, Payment.PROCESSED)
        work = ThePayNotification.objects.get()
        self.assertEqual(work.pk, self.payment.pk)
        self.assertGreater(work.ready_at, timezone.now())
        fetch_bank_payments.assert_called_once_with(from_date=None)

    def prepare_saved_authorization_checkout(self) -> Payment:
        customer = self.payment.customer
        customer.address = "Test Street 1"
        customer.city = "Brussels"
        customer.postcode = "1000"
        customer.save(update_fields=["address", "city", "postcode"])
        return Payment.objects.create(
            customer=customer,
            amount=100,
            recurring="y",
            card_info={"number": "515735******2654", "expiration_date": "2030-05"},
        )

    def assert_saved_card_documents(self, payment: Payment) -> None:
        self.assertEqual(payment.state, Payment.ACCEPTED)
        self.assertEqual(payment.card_info, {"number": "515735******2654"})
        invoice = payment.paid_invoice
        assert invoice is not None
        for path in (invoice.path, invoice.receipt_path):
            pdf_text = " ".join(page.extract_text() for page in PdfReader(path).pages)
            self.assertIn("5157 35** **** 2654", " ".join(pdf_text.split()))

    def checkout_saved_card(
        self,
        http: responses.RequestsMock,
        payment: Payment,
        charge_response: dict[str, str | dict[str, bool]],
    ) -> None:
        http.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{payment.repeat_id}/savedauthorization",
            json=charge_response,
        )
        response = self.client.post(
            reverse("payment", kwargs={"pk": payment.pk}),
            {"method": self.payment.backend},
        )
        self.assertRedirects(
            response,
            f"{reverse('donate-process')}?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        payment.refresh_from_db()
        self.assertEqual([call.request.method for call in thepay_calls(http)], ["POST"])

    def test_checkout_uses_synchronous_saved_authorization_result(self) -> None:
        parent = self.prepare_saved_authorization_checkout()
        payment = Payment.objects.create(
            customer=parent.customer, amount=100, repeat=parent
        )
        with responses.RequestsMock() as http:
            self.checkout_saved_card(http, payment, {"state": "paid"})
        self.assert_saved_card_documents(payment)
        self.assertFalse(ThePayNotification.objects.exists())

    def test_checkout_verifies_draft_invoice_settlement(self) -> None:
        parent = self.prepare_saved_authorization_checkout()
        for currency, adjustment, reason in (
            ("EUR", 0, None),
            ("EUR", -1, "underpayment"),
            ("CZK", 0, "currency_mismatch"),
        ):
            with self.subTest(reason=reason), responses.RequestsMock() as http:
                invoice = Invoice.objects.create(
                    customer=parent.customer,
                    kind=InvoiceKind.DRAFT,
                    category=ServiceCategory.HOSTING,
                    vat_rate=21,
                )
                invoice.invoiceitem_set.create(description="Hosting", unit_price=100)
                payment = invoice.create_payment(repeat=parent)
                self.checkout_saved_card(http, payment, {"state": "paid"})
                self.assertEqual(payment.state, Payment.PENDING)
                self.assertIsNone(payment.paid_invoice_id)
                self.assertFalse(invoice.is_paid)

                checkout = self.client.post(
                    reverse("payment", args=[payment.pk]),
                    {"method": payment.backend},
                )
                self.assertRedirects(
                    checkout, payment.get_complete_url(), fetch_redirect_response=False
                )
                self.assertTemplateUsed(
                    self.client.get(payment.get_complete_url()), "payment/pending.html"
                )
                self.assertEqual(len(thepay_calls(http)), 1)
                detail_url = (
                    f"https://demo.api.thepay.cz/v1/projects/42/payments/{payment.pk}"
                )

                if reason is None:
                    http.get(detail_url, status=404)
                    process_thepay_notifications()
                    payment.refresh_from_db()
                    self.assertEqual(payment.state, Payment.PENDING)
                    self.assertIsNone(payment.paid_invoice_id)
                    self.make_notification_ready(payment)

                    http.get(detail_url, json={"state": "waiting_for_confirmation"})
                    process_thepay_notifications()
                    payment.refresh_from_db()
                    self.assertEqual(payment.state, Payment.PENDING)
                    self.make_notification_ready(payment)

                http.get(
                    detail_url,
                    json={
                        "state": "paid",
                        "paid_amount": int(invoice.total_amount * 100) + adjustment,
                        "currency": currency,
                    },
                )
                process_thepay_notifications()
                payment.refresh_from_db()
                self.assertFalse(
                    ThePayNotification.objects.filter(pk=payment.pk).exists()
                )
                self.assertNotIn("thepay_checkout_verification", payment.details)
                detail_request = thepay_calls(http)[-1].request
                self.assertEqual(detail_request.method, "GET")
                self.assertTrue(detail_request.headers["Signature"])
                if reason is None:
                    self.assert_saved_card_documents(payment)
                else:
                    self.assertEqual(payment.state, Payment.PENDING)
                    self.assertIsNone(payment.paid_invoice_id)
                    self.assertEqual(
                        payment.details["settlement_review"]["reason"], reason
                    )

    def make_notification_ready(self, payment: Payment) -> None:
        work = ThePayNotification.objects.get(pk=payment.pk)
        self.assertGreater(work.ready_at, timezone.now())
        ThePayNotification.objects.filter(pk=payment.pk).update(ready_at=timezone.now())

    def test_checkout_defers_failure_notice_until_authorization_is_verified(
        self,
    ) -> None:
        for available in (False, True):
            with self.subTest(available=available), responses.RequestsMock() as http:
                mail.outbox.clear()
                parent = self.prepare_saved_authorization_checkout()
                payment = Payment.objects.create(
                    customer=parent.customer, amount=100, repeat=parent
                )
                self.checkout_saved_card(
                    http,
                    payment,
                    {
                        "state": "error",
                        "parent": {"recurring_payments_available": True},
                    },
                )
                self.assertEqual(payment.state, Payment.PENDING)
                self.assertEqual(len(mail.outbox), 0)

                detail_url = (
                    f"https://demo.api.thepay.cz/v1/projects/42/payments/{payment.pk}"
                )
                http.get(detail_url, json={"state": "error", "parent": None})
                process_thepay_notifications()
                payment.refresh_from_db()
                self.assertEqual(payment.state, Payment.PENDING)
                self.assertEqual(len(mail.outbox), 0)
                self.make_notification_ready(payment)

                http.get(
                    detail_url,
                    json={
                        "state": "error",
                        "parent": {"recurring_payments_available": available},
                    },
                )
                process_thepay_notifications()
                payment.refresh_from_db()
                parent.refresh_from_db()
                self.assertEqual(payment.state, Payment.REJECTED)
                self.assertEqual(
                    payment.has_unavailable_recurring_authorization, not available
                )
                self.assertEqual(parent.recurring, "y" if available else "")
                self.assertEqual(len(mail.outbox), 1)
                self.assertEqual(
                    "Automatic renewal has stopped" in mail.outbox[0].body,
                    not available,
                )
                self.assertFalse(
                    ThePayNotification.objects.filter(pk=payment.pk).exists()
                )

    @responses.activate
    def test_pending_saved_authorization_checkout_settles_on_notification(self) -> None:
        parent = self.prepare_saved_authorization_checkout()
        self.payment.repeat = parent
        self.payment.state = Payment.NEW
        self.payment.save(update_fields=["repeat", "state"])
        responses.post(
            f"https://demo.api.thepay.cz/v2/projects/42/payments/{parent.pk}/savedauthorization",
            json={"state": "waiting_for_confirmation"},
            status=202,
        )
        checkout_url = reverse("payment", kwargs={"pk": self.payment.pk})
        method = {"method": self.payment.backend}
        response = self.client.post(checkout_url, method)
        self.assertRedirects(
            response,
            f"{reverse('donate-process')}?payment={self.payment.pk}",
            fetch_redirect_response=False,
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PENDING)
        response = self.client.post(checkout_url, method)
        self.assertRedirects(
            response, self.payment.get_complete_url(), fetch_redirect_response=False
        )
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0].request.method, "POST")

        thepay_mock_payment(self.payment.pk)
        self.assertEqual(self.client.get(self.url, self.query).status_code, 200)
        process_thepay_notifications()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.ACCEPTED)
        self.assertEqual(len(responses.calls), 2)
        detail_request = responses.calls[1].request
        self.assertEqual(detail_request.method, "GET")
        self.assertTrue(detail_request.headers["Signature"])

    @responses.activate
    def test_recurring_notification_looks_up_latest_state(self) -> None:
        self.payment.repeat = create_payment()
        self.payment.details["repeat_response"] = {"state": "paid"}
        self.payment.save()
        responses.get(self.detail_url, json={"state": "waiting_for_confirmation"})

        query = {**self.query, "type": "offset_account_obtained", "state": "paid"}
        self.assertEqual(self.client.get(self.url, query).status_code, 200)
        process_thepay_notifications()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PENDING)
        self.assertEqual(len(responses.calls), 1)
        self.assertFalse(ThePayNotification.objects.exists())

    def test_invalid_notification(self) -> None:
        for query in (
            {},
            {**self.query, "project_id": "100"},
            {**self.query, "payment_uid": "invalid"},
            {**self.query, "payment_uid": str(uuid4())},
            {**self.query, "type": "unknown"},
            {**self.query, "type": ["state_changed", "state_changed"]},
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url, query).status_code, 400)
        self.assertEqual(self.client.post(self.url, self.query).status_code, 405)
        missing_url = reverse("thepay-notification", kwargs={"pk": uuid4()})
        self.assertEqual(self.client.get(missing_url, self.query).status_code, 404)
        Payment.objects.filter(pk=self.payment.pk).update(backend="fio-bank")
        self.assertEqual(self.client.get(self.url, self.query).status_code, 404)
        self.assertFalse(ThePayNotification.objects.exists())

    def test_failed_queue_write(self) -> None:
        with patch.object(
            ThePayNotification.objects, "update_or_create", side_effect=DatabaseError
        ):
            response = self.client.get(self.url, self.query)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(ThePayNotification.objects.exists())

    def test_legacy_notification_and_email_language(self) -> None:
        with (
            override("cs"),
            patch.object(ThePay2Card, "request") as request,
        ):
            complete_url = self.payment.get_complete_url()
            response = self.client.get(complete_url, self.query)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        request.assert_not_called()

        def payment_detail(*args):
            self.assertEqual(get_language(), "cs")
            return {"state": "waiting_for_payment"}

        with patch.object(
            ThePay2Card, "request", side_effect=payment_detail
        ) as request:
            call_command("process_payments")
        request.assert_called_once_with("get", f"payments/{self.payment.pk}")
        self.assertFalse(ThePayNotification.objects.exists())

    @responses.activate
    def test_browser_return_with_provider_identifiers(self) -> None:
        query = {key: self.query[key] for key in ("project_id", "payment_uid")}
        complete_url = self.payment.get_complete_url()
        responses.get(self.detail_url, json={"state": "waiting_for_payment"})
        responses.get(self.detail_url, json={"state": "error"})
        response = self.client.get(complete_url, query)
        self.assertTemplateUsed(response, "payment/pending.html")

        response = self.client.get(complete_url, query)
        self.assertRedirects(
            response,
            f"{reverse('donate-process')}?payment={self.payment.pk}",
            fetch_redirect_response=False,
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.REJECTED)
        self.assertFalse(ThePayNotification.objects.exists())

    @responses.activate
    def test_fosdem_browser_return_after_worker(self) -> None:
        self.payment.customer.origin = FOSDEM_ORIGIN
        self.payment.customer.save(update_fields=["origin"])
        thepay_mock_payment(self.payment.pk)
        self.assertEqual(self.client.get(self.url, self.query).status_code, 200)
        call_command("process_payments")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, Payment.PROCESSED)
        self.assertFalse(Subscription.objects.filter(payment=self.payment).exists())

        response = self.client.get(self.payment.get_complete_url())
        self.assertRedirects(response, FOSDEM_ORIGIN, fetch_redirect_response=False)
        self.assertContains(
            self.client.get("/en/about/"),
            "Thank you for your donation and enjoy FOSDEM.",
        )
        responses.assert_call_count(self.detail_url, 1)


@override_settings(**THEPAY2_MOCK_SETTINGS, LANGUAGE_CODE="en")
@skipUnlessDBFeature("has_select_for_update")
class ThePayNotificationConcurrencyTest(TransactionTestCase):
    def test_delivery_during_lookup(self) -> None:
        payment = create_payment()
        url = reverse("thepay-notification", kwargs={"pk": payment.pk})
        query = notification_query(payment)
        self.assertEqual(self.client.get(url, query).status_code, 200)
        first_revision = ThePayNotification.objects.get(pk=payment.pk).revision
        lookup_started = Event()
        delivery_committed = Event()

        def payment_detail(*args):
            lookup_started.set()
            if not delivery_committed.wait(timeout=10):
                raise TimeoutError("Notification delivery did not commit during lookup")
            return {"state": "waiting_for_payment"}

        def process() -> None:
            try:
                process_thepay_notifications()
            finally:
                connections.close_all()

        def deliver() -> int:
            try:
                return Client().get(url, query).status_code
            finally:
                connections.close_all()

        with (
            patch.object(ThePay2Card, "request", side_effect=payment_detail),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            worker = executor.submit(process)
            try:
                self.assertTrue(lookup_started.wait(timeout=10))
                self.assertEqual(executor.submit(deliver).result(timeout=5), 200)
            finally:
                delivery_committed.set()
            worker.result(timeout=10)

        revision = ThePayNotification.objects.get(pk=payment.pk).revision
        self.assertNotEqual(revision, first_revision)

        with patch.object(
            ThePay2Card, "request", return_value={"state": "waiting_for_payment"}
        ):
            process_thepay_notifications()
        self.assertFalse(ThePayNotification.objects.exists())
