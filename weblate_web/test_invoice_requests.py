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

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.db import OperationalError, close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from requests import HTTPError, Response, Timeout

from weblate_web.crm.models import Interaction
from weblate_web.invoice_requests import submit_invoice_request
from weblate_web.invoices.models import Invoice
from weblate_web.models import Package, Service, Subscription
from weblate_web.payments.models import Customer, Payment
from weblate_web.zammad import (
    create_invoice_request_article,
    create_invoice_request_ticket,
)


@override_settings(LANGUAGE_CODE="en")
class InvoiceRequestTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "invoice-request", email="owner@example.com"
        )
        self.customer = Customer.objects.create(
            name="Invoice customer", country="US", user_id=-1
        )
        self.customer.owners.add(self.user)
        self.service = Service.objects.create(customer=self.customer)
        Package.objects.create(name="community", verbose="Community support", price=0)
        self.package = Package.objects.create(
            name="basic", verbose="Basic support", price=100
        )
        self.subscription = Subscription.objects.create(
            service=self.service,
            package=self.package,
            payment=Payment.objects.create(
                amount=100, customer=self.customer, state=Payment.PROCESSED
            ),
            expires=timezone.now() + timedelta(days=30),
        )
        self.url = reverse(
            "subscription-invoice-request", kwargs={"pk": self.subscription.pk}
        )
        self.client.force_login(self.user)

    @patch("weblate_web.invoice_requests.create_invoice_request_article")
    @patch(
        "weblate_web.invoice_requests.create_invoice_request_ticket",
        return_value={"id": 42, "number": "10042"},
    )
    def test_submit_and_shared_cooldown(self, ticket, article) -> None:
        before = (
            Invoice.objects.count(),
            Payment.objects.count(),
            self.subscription.expires,
        )
        response = self.client.post(self.url, {"customer_reference": "PO-42"})
        self.assertRedirects(response, reverse("user"), fetch_redirect_response=False)
        interaction = Interaction.objects.get(origin=Interaction.Origin.INVOICE_REQUEST)
        self.assertEqual(interaction.customer, self.customer)
        self.assertEqual(interaction.user, self.user)
        self.assertEqual(interaction.remote_id, 42)
        self.assertEqual(interaction.details["customer_reference"], "PO-42")
        self.assertEqual(interaction.details["status"], "submitted")
        self.assertEqual(interaction.display_summary, "Requested renewal invoice")
        self.assertTrue(any(row.url.endswith("/42") for row in interaction.detail_rows))
        other = User.objects.create_user("second-owner", email="second@example.com")
        self.customer.owners.add(other)
        self.client.force_login(other)
        self.client.post(self.url, {})
        ticket.assert_called_once()
        article.assert_called_once()
        self.assertEqual(Interaction.objects.count(), 1)
        self.subscription.refresh_from_db()
        self.assertEqual(
            before,
            (
                Invoice.objects.count(),
                Payment.objects.count(),
                self.subscription.expires,
            ),
        )
        Interaction.objects.filter(pk=interaction.pk).update(
            timestamp=timezone.now() - timedelta(hours=25)
        )
        self.client.post(self.url, {})
        self.assertEqual(ticket.call_count, 2)
        self.assertEqual(Interaction.objects.count(), 2)

    @patch("weblate_web.invoice_requests.create_invoice_request_ticket")
    def test_access_validation_and_get(self, ticket) -> None:
        self.assertContains(self.client.get(self.url), "purchase order number")
        self.client.post(self.url, {"customer_reference": "x" * 101})
        self.assertFalse(Interaction.objects.exists())
        self.assertEqual(
            Client(enforce_csrf_checks=True).post(self.url, {}).status_code, 403
        )
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        outsider = User.objects.create_user("outsider")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {}).status_code, 404)
        self.client.force_login(self.user)
        self.user.email = ""
        self.user.save()
        self.assertContains(self.client.post(self.url, {}), "email address is required")
        self.package.name = "nonrecurring"
        self.package.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        ticket.assert_not_called()

    @patch("weblate_web.invoice_requests.create_invoice_request_article")
    @patch(
        "weblate_web.invoice_requests.create_invoice_request_ticket",
        return_value={"id": 42, "number": "10042"},
    )
    def test_definite_article_failure_retries_same_interaction(
        self, ticket, article
    ) -> None:
        response = Response()
        response.status_code = 422
        article.side_effect = HTTPError(response=response)
        with self.assertRaises(HTTPError):
            submit_invoice_request(self.subscription, self.user, "ORIGINAL")
        article.side_effect = None
        self.assertEqual(
            submit_invoice_request(self.subscription, self.user, "CHANGED"), "submitted"
        )
        interaction = Interaction.objects.get()
        self.assertEqual(interaction.details["customer_reference"], "ORIGINAL")
        self.assertEqual(interaction.details["status"], "submitted")
        ticket.assert_called_once()
        self.assertEqual(article.call_count, 2)

    @patch(
        "weblate_web.invoice_requests.create_invoice_request_ticket",
        side_effect=Timeout,
    )
    def test_ambiguous_failure_blocks_recreation(self, ticket) -> None:
        with self.assertRaises(Timeout):
            submit_invoice_request(self.subscription, self.user, "")
        self.assertEqual(
            submit_invoice_request(self.subscription, self.user, ""), "uncertain"
        )
        ticket.assert_called_once()
        self.assertEqual(Interaction.objects.get().details["status"], "uncertain")

    @patch("weblate_web.context_processors.get_activity", return_value=[])
    @patch("weblate_web.views.sentry_sdk.capture_exception")
    @patch("weblate_web.invoice_requests.create_invoice_request_article")
    @patch("weblate_web.invoice_requests.create_invoice_request_ticket")
    def test_rejected_ticket_preserves_input_and_can_retry(
        self, ticket, article, capture_exception, _get_activity
    ) -> None:
        response = Response()
        response.status_code = 422
        ticket.side_effect = HTTPError(response=response)
        result = self.client.post(self.url, {"customer_reference": "PO-RETRY"})
        self.assertContains(result, "Could not send your invoice request")
        self.assertContains(result, 'value="PO-RETRY"')
        self.assertEqual(Interaction.objects.get().details["status"], "failed")
        article.assert_not_called()
        capture_exception.assert_called_once()
        ticket.side_effect = None
        ticket.return_value = {"id": 42, "number": "10042"}
        self.client.post(self.url, {"customer_reference": "PO-RETRY"})
        interaction = Interaction.objects.get()
        self.assertEqual(interaction.details["status"], "submitted")
        self.assertEqual(interaction.remote_id, 42)
        article.assert_called_once()

    @patch("weblate_web.invoice_requests.create_invoice_request_article")
    @patch("weblate_web.invoice_requests.create_invoice_request_ticket")
    def test_pending_blocks_submission(self, ticket, article) -> None:
        Interaction.objects.create(
            customer=self.customer,
            origin=Interaction.Origin.INVOICE_REQUEST,
            summary="renewal-invoice-request",
            details={"subscription_id": self.subscription.pk, "status": "pending"},
        )
        self.assertEqual(
            submit_invoice_request(self.subscription, self.user, ""), "pending"
        )
        ticket.assert_not_called()
        article.assert_not_called()

    @patch("weblate_web.invoice_requests.create_invoice_request_article")
    @patch(
        "weblate_web.invoice_requests.create_invoice_request_ticket",
        return_value={"id": 42, "number": "10042"},
    )
    def test_expired_subscription_and_empty_reference(self, ticket, article) -> None:
        self.subscription.expires = timezone.now() - timedelta(days=2)
        self.subscription.save()
        self.client.post(self.url, {})
        self.assertEqual(Interaction.objects.get().details["customer_reference"], "")
        ticket.assert_called_once()
        article.assert_called_once()

    @patch("weblate_web.zammad.get_zammad_client")
    def test_zammad_payload_and_preserved_rejection(self, get_client) -> None:
        client = get_client.return_value
        client.url = "https://care.weblate.org/api/v1/"
        client.session.post.return_value.json.return_value = {
            "id": 42,
            "number": "10042",
        }
        interaction = Interaction(
            customer=self.customer,
            content="Request invoice",
            remote_id=42,
            details={
                "service_id": self.service.pk,
                "subscription_id": self.subscription.pk,
                "package": "Basic support",
                "requester_email": self.user.email,
                "customer_reference": "PO & # é",
            },
        )
        self.assertEqual(create_invoice_request_ticket(interaction)["id"], 42)
        params = client.session.post.call_args.kwargs["json"]
        self.assertEqual(params["customer_id"], "guess:owner@example.com")
        self.assertTrue(params["article"]["internal"])
        self.assertEqual(params["article"]["type"], "note")
        url = urlsplit(params["article"]["body"].removeprefix("CRM: "))
        self.assertEqual(parse_qs(url.query)["customer_reference"], ["PO & # é"])
        self.assertEqual(url.fragment, f"subscription-{self.subscription.pk}")
        create_invoice_request_article(interaction)
        params = client.session.post.call_args.kwargs["json"]
        self.assertFalse(params["internal"])
        self.assertEqual(params["type"], "web")
        self.assertNotIn("CRM:", params["body"])
        self.assertEqual(client.session.post.call_args.kwargs["timeout"], 30)
        response = Response()
        response.status_code = 422
        client.session.post.return_value.raise_for_status.side_effect = HTTPError(
            response=response
        )
        with self.assertRaises(HTTPError) as raised:
            create_invoice_request_ticket(interaction)
        self.assertIs(raised.exception.response, response)

    def test_crm_reference_only_prefills_matching_renewal(self) -> None:
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        second = Subscription.objects.create(
            service=self.service,
            package=self.package,
            expires=self.subscription.expires,
        )
        url = reverse("crm:service-detail", kwargs={"pk": self.service.pk})
        response = self.client.get(
            url,
            {
                "subscription": str(self.subscription.pk),
                "customer_reference": "PO & # é",
            },
        )
        subscriptions = response.context["renewal_subscriptions"]
        forms = {sub.pk: form for sub, form in subscriptions}
        self.assertEqual(
            forms[self.subscription.pk].initial["customer_reference"], "PO & # é"
        )
        self.assertNotIn("customer_reference", forms[second.pk].initial)
        self.assertContains(response, f'id="subscription-{self.subscription.pk}"')
        self.assertFalse(Invoice.objects.exists())
        for params in (
            {"subscription": "invalid", "customer_reference": "PO"},
            {
                "subscription": str(self.subscription.pk),
                "customer_reference": "x" * 101,
            },
        ):
            response = self.client.get(url, params)
            for _sub, form in response.context["renewal_subscriptions"]:
                self.assertFalse(form.initial.get("customer_reference"))


class InvoiceRequestConcurrencyTest(TransactionTestCase):
    def test_concurrent_requests_create_one_ticket(self) -> None:
        user = User.objects.create_user("concurrent-owner", email="owner@example.com")
        customer = Customer.objects.create(name="Customer", country="US", user_id=-1)
        customer.owners.add(user)
        Package.objects.create(name="community", verbose="Community support", price=0)
        package = Package.objects.create(
            name="basic", verbose="Basic support", price=100
        )
        service = Service.objects.create(customer=customer)
        subscription = Subscription.objects.create(
            service=service,
            package=package,
            expires=timezone.now() + timedelta(days=30),
        )
        start = Barrier(2)
        finish = Event()
        sqlite = connection.vendor == "sqlite"

        def submit() -> str:
            close_old_connections()
            try:
                start.wait(timeout=10)
                return submit_invoice_request(subscription, user, "PO")
            except OperationalError:
                if not sqlite:
                    raise
                # Shared in-memory SQLite rejects concurrent writes immediately.
                # The view handles this failure without creating another ticket.
                return "locked"
            finally:
                close_old_connections()

        def create_ticket(_interaction):
            finish.wait(timeout=10)
            return {"id": 42, "number": "10042"}

        with (
            patch(
                "weblate_web.invoice_requests.create_invoice_request_ticket",
                side_effect=create_ticket,
            ) as ticket,
            patch(
                "weblate_web.invoice_requests.create_invoice_request_article"
            ) as article,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(submit) for _ in range(2)]
            done, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            finish.set()
            self.assertTrue(done)
            results = [future.result(timeout=10) for future in futures]
            self.assertEqual(results.count("submitted"), 1)
            ticket.assert_called_once()
            article.assert_called_once()
        self.assertEqual(Interaction.objects.count(), 1)
