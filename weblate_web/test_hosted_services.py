# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.core.signing import dumps
from django.db import connections, transaction
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from weblate_web.categories import ServiceCategory
from weblate_web.hosted_services import USAGE_FIELDS, apply_pending_hosted_owners
from weblate_web.hosted_users import sync_hosted_identity
from weblate_web.invoices.models import (
    Currency,
    HostedRenewalValidationError,
    Invoice,
    InvoiceKind,
)
from weblate_web.models import HostedIdentity, Package, Report, Service, Subscription
from weblate_web.payments.backends import FioBank
from weblate_web.payments.invitations import accept_invitation, get_invitation_token
from weblate_web.payments.models import Customer, Payment
from weblate_web.utils import HOSTED_ORIGIN, PAYMENTS_ORIGIN


def post_snapshot(client: Client, **changes):
    payload = {
        "billing": 42,
        "payment_ids": [],
        "workspace_id": "a8171a76-f3fa-48aa-8082-89e010185d82",
        "workspace_name": "Example Workspace",
        "customer_name": "Example Customer",
        "plan": "hosted-10k",
        "administrators": ["123"],
        "state": "trial",
        "payment_mode": "local",
        "paid": True,
        "version": "2026.10.dev0",
        "snapshot_at": "2026-10-01T00:00:00.000001Z",
        "usage": dict.fromkeys(USAGE_FIELDS, 10),
        **changes,
    }
    return client.post(
        "/api/hosted/",
        {"payload": dumps(payload, key=settings.PAYMENT_SECRET, salt="weblate.hosted")},
    )


class HostedServiceTest(TestCase):
    def test_registration_and_ordering(self) -> None:
        response = post_snapshot(self.client)
        self.assertEqual(response.status_code, 200)
        service = Service.objects.get(hosted_billing=42)
        self.assertEqual(response.json()["service_id"], service.pk)
        self.assertEqual(response.json()["customer_id"], service.customer_id)
        self.assertEqual(service.customer.origin, HOSTED_ORIGIN)
        self.assertEqual(
            response.json()["snapshot_at"], "2026-10-01T00:00:00.000001+00:00"
        )
        self.assertEqual(service.customer.name, "Example Customer")
        self.assertEqual(service.site_title, "Example Workspace")
        self.assertIn(
            service.hosted_snapshot["workspace_id"], service.hosted_workspace_url
        )
        report = service.report_set.get()
        self.assertEqual(report.timestamp, service.hosted_snapshot_at)
        self.assertGreater(
            service.hosted_synced_at or report.timestamp, report.timestamp
        )
        self.assertEqual(report.version, service.site_version)
        self.assertEqual([getattr(report, field) for field in USAGE_FIELDS], [10] * 6)
        self.assertEqual(post_snapshot(self.client).json(), response.json())
        self.assertEqual(
            post_snapshot(
                self.client, snapshot_at="2026-09-01T00:00:00Z", administrators=[]
            ).json(),
            response.json(),
        )
        self.assertEqual(service.report_set.count(), 1)
        self.assertEqual(service.hosted_administrators.get().external_id, "123")

        self.assertEqual(
            post_snapshot(
                self.client,
                snapshot_at="2026-10-02T00:00:00Z",
                workspace_name="Renamed Workspace",
                state="terminated",
                paid=False,
                usage=dict.fromkeys(USAGE_FIELDS, 20),
            ).status_code,
            200,
        )
        service.refresh_from_db()
        self.assertEqual(service.site_title, "Renamed Workspace")
        self.assertEqual(service.hosted_snapshot["state"], "terminated")
        self.assertFalse(service.hosted_snapshot["paid"])
        self.assertEqual(service.site_projects, 20)
        self.assertEqual(service.report_set.count(), 2)
        self.assertEqual(service.report_set.latest("timestamp").hosted_words, 20)

    def test_preserve_financial_records(self) -> None:
        customer = Customer.objects.create(
            user_id=-1, name="Original", origin=PAYMENTS_ORIGIN
        )
        service = Service.objects.create(customer=customer, hosted_billing=42)
        payment = Payment.objects.create(
            customer=customer, amount=100, recurring="y", extra={"billing": 42}
        )
        Package.objects.create(name="community", price=0)
        package = Package.objects.create(name="hosted:10k", price=100)
        subscription = Subscription.objects.create(
            service=service, package=package, payment=payment, expires=timezone.now()
        )
        before = Subscription.objects.values().get(pk=subscription.pk)
        payment_before = Payment.objects.values().get(pk=payment.pk)
        response = post_snapshot(self.client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customer_id"], customer.pk)
        self.assertEqual(post_snapshot(self.client, billing=43).status_code, 200)
        self.assertEqual(Subscription.objects.values().get(), before)
        self.assertEqual(Payment.objects.values().get(), payment_before)
        service.refresh_from_db()
        self.assertEqual(service.customer, customer)
        self.assertEqual(service.customer.name, "Original")
        self.assertEqual(service.customer.origin, PAYMENTS_ORIGIN)
        self.assertEqual(service.status, "community")
        self.assertEqual(service.limit_hosted_strings, 0)

    def test_unlinked_payment_reuses_customer(self) -> None:
        payer = Customer.objects.create(user_id=-1, origin=PAYMENTS_ORIGIN)
        Payment.objects.create(
            customer=payer, amount=20, extra={"billing": 42}, state=Payment.PROCESSED
        )
        response = post_snapshot(self.client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customer_id"], payer.pk)
        service = Service.objects.get()
        self.assertEqual(service.customer, payer)
        self.assertEqual(service.customer.origin, PAYMENTS_ORIGIN)
        self.assertFalse(service.subscription_set.exists())

    def test_trial_payment_uses_authoritative_customer(self) -> None:
        registration = post_snapshot(self.client).json()
        payment = Payment.objects.create(
            customer_id=registration["customer_id"],
            amount=20,
            state=Payment.ACCEPTED,
            extra={"billing": 42},
            recurring="y",
        )
        self.assertTrue(
            Payment.objects.filter(
                pk=payment.pk, customer__origin=HOSTED_ORIGIN, state=Payment.ACCEPTED
            ).exists()
        )
        call_command("process_payments")
        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.ACCEPTED)
        self.assertFalse(Subscription.objects.exists())
        Payment.objects.filter(pk=payment.pk).update(state=Payment.PROCESSED)
        result = post_snapshot(
            self.client,
            snapshot_at="2026-10-02T00:00:00Z",
            payment_ids=[str(payment.pk)],
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["customer_id"], registration["customer_id"])
        self.assertEqual(Customer.objects.count(), 1)

    def test_linked_payment_validation_and_replays(self) -> None:
        owner = User.objects.create_user(username="prior-owner")
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=owner, external_id="123"
        )
        registration = post_snapshot(self.client).json()
        payer = Customer.objects.create(user_id=-1)
        pending = Payment.objects.create(
            customer=payer, amount=20, state=Payment.ACCEPTED
        )
        references = [str(pending.pk), str(uuid4())]
        accepted = post_snapshot(
            self.client, snapshot_at="2026-10-02T00:00:00Z", payment_ids=references
        )
        self.assertEqual(accepted.status_code, 200)
        Payment.objects.filter(pk=pending.pk).update(state=Payment.PROCESSED)
        self.assertEqual(
            post_snapshot(self.client, payment_ids=references).json(), accepted.json()
        )
        service_before = Service.objects.values().get()
        payment_before = Payment.objects.values().get()
        conflict = post_snapshot(
            self.client,
            snapshot_at="2026-10-03T00:00:00Z",
            payment_ids=references,
            administrators=[],
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json(),
            {
                "billing": 42,
                "error": "payment_customer_conflict",
                "customer_id": registration["customer_id"],
                "payment_ids": [str(pending.pk)],
                "payer_ids": [payer.pk],
            },
        )
        self.assertEqual(Service.objects.values().get(), service_before)
        self.assertEqual(Payment.objects.values().get(), payment_before)
        self.assertEqual(Report.objects.count(), 2)
        self.assertEqual(
            Service.objects.get().hosted_administrators.get().external_id, "123"
        )

        customer = Customer.objects.get(pk=registration["customer_id"])
        self.assertIn(owner, customer.owners.all())
        retry = post_snapshot(
            self.client, snapshot_at="2026-10-03T00:00:00Z", administrators=[]
        )
        self.assertEqual(retry.status_code, 200)
        self.assertNotIn(owner, customer.owners.all())

    def test_new_snapshot_reconciles_pending_owners(self) -> None:
        provider = settings.HOSTED_IDENTITY_PROVIDER
        resolved, pending, removed = [
            User.objects.create_user(username=name)
            for name in ("resolved", "pending", "removed")
        ]
        HostedIdentity.objects.create(
            provider=provider, user=resolved, external_id="123"
        )
        self.assertEqual(
            post_snapshot(
                self.client, administrators=["123", "124", "125"]
            ).status_code,
            200,
        )
        customer = Service.objects.get().customer
        customer.owners.remove(resolved)
        HostedIdentity.objects.create(
            provider=provider, user=pending, external_id="124"
        )
        HostedIdentity.objects.create(
            provider=provider, user=removed, external_id="125"
        )
        self.assertEqual(
            post_snapshot(
                self.client, administrators=["123", "124", "125"]
            ).status_code,
            200,
        )
        self.assertFalse(customer.owners.exists())

        self.assertEqual(
            post_snapshot(
                self.client,
                snapshot_at="2026-10-02T00:00:00Z",
                administrators=["123", "124"],
            ).status_code,
            200,
        )
        self.assertEqual(list(customer.owners.all()), [pending])
        self.assertEqual(
            Service.objects.get().hosted_administrators.get(external_id="124").user,
            pending,
        )

    def test_initial_payment_and_recurring_history(self) -> None:
        payer = Customer.objects.create(user_id=-1)
        initial = Payment.objects.create(
            customer=payer, amount=20, state=Payment.PROCESSED
        )
        self.assertEqual(
            post_snapshot(self.client, payment_ids=[str(initial.pk)]).status_code, 200
        )
        renewal = Payment.objects.create(
            customer=payer,
            amount=20,
            state=Payment.PROCESSED,
            repeat=initial,
            recurring="y",
            extra={"billing": "42"},
        )
        response = post_snapshot(self.client, billing=43, payment_ids=[str(initial.pk)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["payment_id"], str(renewal.pk))
        independent = Payment.objects.create(
            customer=payer, amount=20, state=Payment.PROCESSED, extra={"billing": 44}
        )
        self.assertEqual(post_snapshot(self.client, billing=44).status_code, 200)
        self.assertEqual(Service.objects.filter(customer=payer).count(), 2)
        independent.refresh_from_db()
        self.assertEqual(independent.extra, {"billing": 44})

    def test_reuse_service_through_recurring_payment(self) -> None:
        payer = Customer.objects.create(user_id=-1)
        service = Service.objects.create(customer=payer)
        initial = Payment.objects.create(
            customer=payer, amount=20, state=Payment.PROCESSED
        )
        Payment.objects.create(
            customer=payer,
            amount=20,
            state=Payment.PROCESSED,
            repeat=initial,
            extra={"billing": 42},
        )
        Package.objects.create(name="community", price=0)
        package = Package.objects.create(
            name="hosted:10k", price=20, category=ServiceCategory.SHARED
        )
        subscription = Subscription.objects.create(
            service=service, package=package, payment=initial, expires=timezone.now()
        )
        original = Subscription.objects.values().get(pk=subscription.pk)
        result = post_snapshot(self.client).json()
        self.assertEqual(result["service_id"], service.pk)
        self.assertEqual(result["customer_id"], payer.pk)
        self.assertEqual(
            Subscription.objects.values().get(pk=subscription.pk), original
        )
        self.assertEqual(Service.objects.count(), 1)
        self.assertEqual(
            post_snapshot(
                self.client, billing=43, payment_ids=[str(initial.pk)]
            ).status_code,
            409,
        )

    def test_ambiguous_and_unavailable_payments(self) -> None:
        first = Customer.objects.create(user_id=-1)
        second = Customer.objects.create(user_id=-1)
        for payer in (first, second):
            Payment.objects.create(
                customer=payer,
                amount=20,
                state=Payment.PROCESSED,
                extra={"billing": 42},
            )
        response = post_snapshot(self.client)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["customer_ids"], [first.pk, second.pk])
        pending = Payment.objects.create(customer=first, amount=20)
        response = post_snapshot(self.client, billing=43, payment_ids=[str(pending.pk)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "payment_unavailable")
        self.assertFalse(Service.objects.exists())
        self.assertEqual(Customer.objects.count(), 2)

    def test_future_timestamp_and_clock_skew(self) -> None:
        now = datetime(2026, 10, 1, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=now):
            self.assertEqual(
                post_snapshot(
                    self.client, snapshot_at="2026-10-01T00:05:01Z"
                ).status_code,
                400,
            )
            self.assertFalse(Service.objects.exists())
            self.assertEqual(
                post_snapshot(
                    self.client, snapshot_at="2026-10-01T00:05:00Z"
                ).status_code,
                200,
            )
        service = Service.objects.get()
        self.assertEqual(service.hosted_synced_at, now)
        self.assertEqual(service.report_set.get().timestamp, now + timedelta(minutes=5))

    def test_regular_report_keeps_insert_time(self) -> None:
        customer = Customer.objects.create(user_id=-1)
        for status in ("hosted", "basic"):
            service = Service.objects.create(customer=customer, status=status)
            report = Report(service=service, timestamp=datetime(2025, 1, 1, tzinfo=UTC))
            now = datetime(2026, 10, 1, tzinfo=UTC)
            with patch("django.utils.timezone.now", return_value=now):
                report.save()
            report.refresh_from_db()
            self.assertEqual(report.timestamp, now)

    @override("en")
    def test_customer_billing_grants(self) -> None:
        customer = Customer.objects.create(user_id=-1)
        Service.objects.create(customer=customer, hosted_billing=42)
        Service.objects.create(customer=customer, hosted_billing=43)
        dedicated = Service.objects.create(customer=customer, status="hosted")
        payment = Payment.objects.create(
            customer=customer, amount=100, invoice="legacy-invoice"
        )
        user = User.objects.create_user(username="admin")
        unrelated = User.objects.create_user(username="123")
        HostedIdentity.objects.create(
            provider="other", user=unrelated, external_id="123"
        )
        self.client.force_login(user)
        dpa_url = reverse("customer-agreement", args=[customer.pk])
        self.assertEqual(post_snapshot(self.client).status_code, 200)
        self.assertEqual(self.client.get(dpa_url).status_code, 404)
        self.client.force_login(unrelated)
        self.assertEqual(self.client.get(dpa_url).status_code, 404)
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=user, external_id="123"
        )
        with self.captureOnCommitCallbacks(execute=True):
            sync_hosted_identity(
                provider=settings.HOSTED_IDENTITY_PROVIDER,
                external_id="123",
                profile={},
            )
        self.client.force_login(user)
        response = self.client.get(dpa_url)
        self.assertEqual(response.status_code, 200)
        with (
            TemporaryDirectory() as agreements_path,
            self.settings(
                AGREEMENTS_PATH=Path(agreements_path),
                AGREEMENTS_SIGNATURE_PATH=Path(__file__).parent
                / "static"
                / "weblate-black.svg",
            ),
        ):
            self.assertRedirects(self.client.post(dpa_url, {"consent": True}), dpa_url)
            agreement_url = reverse(
                "agreement-download", args=[customer.agreement_set.get().pk]
            )
            self.assertEqual(self.client.get(agreement_url).status_code, 200)
        invoice_url = reverse("user-invoice", args=[payment.pk])
        with (
            patch.object(
                Payment,
                "invoice_filename_valid",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch(
                "weblate_web.views.open", return_value=BytesIO(b"invoice"), create=True
            ),
        ):
            invoice = self.client.get(invoice_url)
            self.assertEqual(invoice.status_code, 200)
        response = self.client.get(reverse("user"))
        self.assertContains(response, "Example Workspace")
        self.assertIn(dedicated, response.context["user_services"])
        self.assertContains(response, invoice_url, count=1)
        self.assertEqual(
            self.client.post(
                reverse("customer-owner", args=[customer.pk]),
                {"email": "test@example.com"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("service-token", args=[dedicated.pk])).status_code,
            302,
        )
        customer.owners.add(unrelated)
        self.assertEqual(post_snapshot(self.client, billing=43).status_code, 200)
        later = "2026-10-02T00:00:00Z"
        self.assertEqual(
            post_snapshot(
                self.client, snapshot_at=later, administrators=[]
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(dpa_url).status_code, 200)
        self.assertEqual(
            post_snapshot(
                self.client, billing=43, snapshot_at=later, administrators=[]
            ).status_code,
            200,
        )
        self.assertTrue(customer.owners.filter(pk=unrelated.pk).exists())
        response = self.client.get(dpa_url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(invoice_url).status_code, 404)
        self.assertEqual(self.client.get(agreement_url).status_code, 404)
        customer.owners.add(user)
        self.assertEqual(self.client.get(dpa_url).status_code, 200)
        customer.owners.remove(user)
        self.assertEqual(
            post_snapshot(self.client, snapshot_at="2026-10-03T00:00:00Z").status_code,
            200,
        )
        self.assertEqual(self.client.get(dpa_url).status_code, 200)

    @override("en")
    def test_owner_controls_and_invoice_links(self) -> None:
        user = User.objects.create_user(username="owner")
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=user, external_id="123"
        )
        self.client.force_login(user)
        self.assertEqual(post_snapshot(self.client).status_code, 200)
        service = Service.objects.get()
        draft = Invoice.objects.create(
            customer=service.customer,
            kind=InvoiceKind.PROFORMA,
            category=ServiceCategory.SHARED,
        )
        paid = Invoice.objects.create(
            customer=service.customer,
            kind=InvoiceKind.INVOICE,
            category=ServiceCategory.SHARED,
        )
        draft_payment = Payment.objects.create(
            customer=service.customer, draft_invoice=draft, amount=10
        )
        paid_payment = Payment.objects.create(
            customer=service.customer, paid_invoice=paid, amount=20
        )
        response = self.client.get(reverse("user"))
        self.assertContains(response, "/access/")
        self.assertNotContains(response, "Invite owner")
        self.assertContains(response, draft.filename)
        self.assertContains(response, paid.filename)
        self.assertContains(
            response, reverse("user-invoice", args=[draft_payment.pk]), count=1
        )
        self.assertContains(
            response, reverse("user-invoice", args=[paid_payment.pk]) + "?receipt=1"
        )
        package = Package.objects.create(name="community", price=0)
        subscription = Subscription.objects.create(
            service=service,
            package=package,
            payment=paid_payment,
            expires=timezone.now(),
        )
        for action in (
            "subscription-pay",
            "subscription-disable",
            "subscription-upgrade",
            "subscription-invoice-request",
        ):
            self.assertRedirects(
                self.client.post(reverse(action, args=[subscription.pk])),
                "https://hosted.weblate.org/billing/42/",
                fetch_redirect_response=False,
            )
        paid.extra = {"subscription": subscription.pk}
        paid.save(update_fields=["extra"])
        for action, url in (
            ("invoice payment", reverse("invoice-pay", args=[paid.pk])),
            ("subscription checkout", reverse("subscription-new")),
        ):
            with self.subTest(action=action):
                self.assertRedirects(
                    self.client.post(
                        url, {"service": service.pk, "plan": package.name}
                    ),
                    "https://hosted.weblate.org/billing/42/",
                    fetch_redirect_response=False,
                )
        service.customer.owners.remove(user)
        self.assertEqual(
            post_snapshot(self.client, snapshot_at="2026-10-02T00:00:00Z").status_code,
            200,
        )
        self.assertFalse(service.customer.owners.exists())

    @override("en")
    def test_existing_owner_and_pending_invitation(self) -> None:
        owner = User.objects.create_user(username="prior-owner")
        invitee = User.objects.create_user(
            username="invitee", email="invitee@example.com"
        )
        customer = Customer.objects.create(user_id=-1)
        customer.owners.add(owner)
        invitation = customer.owner_invitations.create(
            email=invitee.email,
            invited_by=owner,
            expires_at=timezone.now() + timedelta(days=1),
        )
        Service.objects.create(customer=customer, hosted_billing=42)
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=owner, external_id="123"
        )
        self.assertEqual(post_snapshot(self.client).status_code, 200)
        self.client.force_login(invitee)
        url = reverse(
            "customer-owner-invitation", args=[get_invitation_token(invitation)]
        )
        self.assertRedirects(self.client.post(url), reverse("user"))
        self.assertIsNone(accept_invitation(invitation, invitee))
        self.assertEqual(
            post_snapshot(
                self.client, snapshot_at="2026-10-02T00:00:00Z", administrators=[]
            ).status_code,
            200,
        )
        self.assertFalse(customer.owners.exists())

    @override("en")
    def test_existing_staff_agreement_is_visible_after_sync(self) -> None:
        customer = Customer.objects.create(user_id=-1)
        Service.objects.create(customer=customer, hosted_billing=42)
        user = User.objects.create_user(username="workspace-admin")
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=user, external_id="123"
        )
        self.client.force_login(user)
        with (
            TemporaryDirectory() as agreements_path,
            self.settings(
                AGREEMENTS_PATH=Path(agreements_path),
                AGREEMENTS_SIGNATURE_PATH=Path(__file__).parent
                / "static"
                / "weblate-black.svg",
            ),
        ):
            agreement = customer.agreement_set.create()
            download = reverse("agreement-download", args=[agreement.pk])
            self.assertEqual(self.client.get(download).status_code, 404)
            self.assertEqual(post_snapshot(self.client).status_code, 200)
            self.assertEqual(self.client.get(download).status_code, 200)

    @override("en")
    def test_hosted_owner_link_before_snapshot(self) -> None:
        user = User.objects.create_user(username="owner")
        customer = Customer.objects.create(user_id=-1)
        customer.owners.add(user)
        Service.objects.create(customer=customer, hosted_billing=42)
        self.client.force_login(user)

        response = self.client.get(reverse("user"))
        self.assertContains(response, "Manage billing access on Hosted Weblate.")
        self.assertContains(response, ">Hosted billing #42</a>")
        self.assertNotContains(response, "Invite owner")
        self.assertNotContains(response, "/access/")

    @override("en")
    def test_hosted_upgrade_invoice_redirect(self) -> None:
        customer = Customer.objects.create(user_id=-1)
        service = Service.objects.create(customer=customer, hosted_billing=42)
        package = Package.objects.create(name="community", price=0)
        subscription = Subscription.objects.create(
            service=service, package=package, expires=timezone.now()
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=ServiceCategory.SHARED,
            extra={"subscription_upgrade": subscription.pk},
        )
        self.assertRedirects(
            self.client.get(reverse("invoice-pay", args=[invoice.pk])),
            "https://hosted.weblate.org/billing/42/",
            fetch_redirect_response=False,
        )
        self.assertFalse(Payment.objects.exists())

    @override("en")
    def test_hosted_renewal_rejects_incompatible_checkout(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin=HOSTED_ORIGIN)
        service = Service.objects.create(customer=customer, hosted_billing=42)
        extra = {
            "hosted_renewal": True,
            "billing": 42,
            "service": service.pk,
            "period": "m",
        }
        for changes in (
            {"currency": Currency.USD},
            {"extra": {**extra, "billing": 43}},
            {"customer": Customer.objects.create(user_id=-1, origin=PAYMENTS_ORIGIN)},
        ):
            with self.subTest(changes=changes):
                fields = {
                    "customer": customer,
                    "kind": InvoiceKind.INVOICE,
                    "category": ServiceCategory.SHARED,
                    "extra": extra,
                    **changes,
                }
                invoice = Invoice.objects.create(**fields)
                self.assertEqual(
                    self.client.get(
                        reverse("invoice-pay", args=[invoice.pk])
                    ).status_code,
                    404,
                )
        self.assertFalse(Payment.objects.exists())

    @override("en")
    def test_hosted_renewal_rejects_mismatched_reused_payment(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin=HOSTED_ORIGIN)
        service = Service.objects.create(customer=customer, hosted_billing=42)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=ServiceCategory.SHARED,
            extra={
                "hosted_renewal": True,
                "billing": 42,
                "service": service.pk,
                "period": "y",
            },
        )
        payment = Payment.objects.create(
            customer=customer,
            draft_invoice=invoice,
            amount=10,
            extra={**invoice.extra, "period": "m"},
            backend="fio-bank",
        )
        self.assertEqual(
            self.client.get(reverse("invoice-pay", args=[invoice.pk])).status_code, 404
        )
        with self.assertRaisesMessage(
            HostedRenewalValidationError,
            "Payment does not match Hosted renewal invoice",
        ):
            FioBank.get_invoice_payment(invoice)
        self.assertEqual(list(Payment.objects.all()), [payment])

    @override("en")
    def test_hosted_renewal_completion_returns_to_account(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin=HOSTED_ORIGIN)
        for state in (Payment.ACCEPTED, Payment.PROCESSED):
            with self.subTest(state=state):
                payment = Payment.objects.create(
                    customer=customer,
                    amount=10,
                    state=state,
                    extra={"hosted_renewal": True, "billing": 42},
                    backend="fio-bank",
                )
                response = Client().get(payment.get_complete_url())
                self.assertRedirects(
                    response, reverse("user"), fetch_redirect_response=False
                )
                self.assertEqual(
                    [str(message) for message in get_messages(response.wsgi_request)],
                    ["Payment received. Hosted Weblate is processing your renewal."]
                    if state == Payment.ACCEPTED
                    else [],
                )

    def test_hosted_renewal_bank_payment_uses_invoice_period(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin=HOSTED_ORIGIN)
        service = Service.objects.create(customer=customer, hosted_billing=42)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=ServiceCategory.SHARED,
            extra={
                "hosted_renewal": True,
                "billing": 42,
                "service": service.pk,
                "period": "y",
            },
        )
        payment = FioBank.get_invoice_payment(invoice)
        self.assertEqual(payment.recurring, "y")
        self.assertEqual(payment.extra, invoice.extra)

    def test_only_missing_repairs_ownership_without_updating_profile(self) -> None:
        self.assertEqual(post_snapshot(self.client).status_code, 200)
        user = User.objects.create_user(username="unchanged")
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, user=user, external_id="123"
        )
        with patch(
            "weblate_web.management.commands.sync_hosted_users.Command.fetch_sync_payload",
            return_value={
                "users": [{"external_id": "123", "profile": {"username": "new"}}]
            },
        ):
            call_command("sync_hosted_users", only_missing=True)
        user.refresh_from_db()
        self.assertEqual(user.username, "unchanged")
        self.assertEqual(list(Service.objects.get().customer.owners.all()), [user])

    def test_bulk_user_sync_resolves_pending_owners_once(self) -> None:
        for billing, no_preload in ((42, False), (43, True)):
            external_ids = [str(billing * 10 + offset) for offset in range(3)]
            self.assertEqual(
                post_snapshot(
                    self.client, billing=billing, administrators=external_ids
                ).status_code,
                200,
            )
            payload = {
                "users": [
                    {
                        "external_id": external_id,
                        "profile": {
                            "username": f"hosted-{external_id}",
                            "is_active": offset != 2,
                        },
                    }
                    for offset, external_id in enumerate(external_ids)
                ],
            }
            with (
                patch(
                    "weblate_web.management.commands.sync_hosted_users.Command.fetch_sync_payload",
                    return_value=payload,
                ),
                patch(
                    "weblate_web.management.commands.sync_hosted_users.apply_pending_hosted_owners",
                    wraps=apply_pending_hosted_owners,
                ) as resolve_batch,
                self.captureOnCommitCallbacks(execute=True) as callbacks,
            ):
                call_command("sync_hosted_users", no_preload=no_preload)
            resolve_batch.assert_called_once_with(set(external_ids[:2]))
            self.assertFalse(callbacks)
            service = Service.objects.get(hosted_billing=billing)
            self.assertEqual(service.customer.owners.count(), 2)
            self.assertEqual(
                service.hosted_administrators.get(user=None).external_id,
                external_ids[2],
            )

    def test_invalid_snapshot_and_rollback(self) -> None:
        for changes in (
            {"paid": 1},
            {"workspace_id": "invalid"},
            {"usage": {"projects": -1}},
            {"administrators": ["123", "123"]},
            {"snapshot_at": "2026-10-01T00:00:00"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(post_snapshot(self.client, **changes).status_code, 400)
        with (
            patch.object(
                Report.objects, "bulk_create", side_effect=RuntimeError("report failed")
            ),
            self.assertRaisesMessage(RuntimeError, "report failed"),
        ):
            post_snapshot(self.client)
        self.assertFalse(Service.objects.exists())
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(Report.objects.exists())


class ConcurrentHostedServiceTest(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_pending_owner_chunks_commit_separately(self) -> None:
        commits = []

        def record_lock() -> None:
            transaction.on_commit(lambda: commits.append(True))
            self.assertEqual(len(commits), lock.call_count - 1)

        with (
            patch(
                "weblate_web.hosted_services.iter_chunks", return_value=[["1"], ["2"]]
            ),
            patch(
                "weblate_web.hosted_services.lock_hosted_sync", side_effect=record_lock
            ) as lock,
        ):
            apply_pending_hosted_owners({"1", "2"})
        self.assertEqual(len(commits), 2)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_registration(self) -> None:
        barrier = Barrier(2, timeout=10)

        def synchronize() -> tuple[int, int]:
            try:
                barrier.wait()
                response = post_snapshot(Client())
                self.assertEqual(response.status_code, 200)
                result = response.json()
                return result["service_id"], result["customer_id"]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(synchronize)
            second = executor.submit(synchronize)
            first_ids = first.result(timeout=20)
            second_ids = second.result(timeout=20)
        self.assertEqual(first_ids, second_ids)
        service = Service.objects.get(hosted_billing=42)
        self.assertEqual(first_ids, (service.pk, service.customer_id))
        self.assertEqual(service.customer.origin, HOSTED_ORIGIN)
        self.assertEqual(Service.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Report.objects.count(), 1)
