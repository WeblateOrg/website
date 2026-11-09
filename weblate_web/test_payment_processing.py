# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from typing import TYPE_CHECKING, Literal
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import (
    Client,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse

from weblate_web.categories import ServiceCategory
from weblate_web.models import (
    Package,
    Service,
    ServiceKind,
    Subscription,
    get_donation_package,
    process_donation,
    process_payment,
    process_subscription,
)
from weblate_web.payments.models import Customer, Payment
from weblate_web.utils import PAYMENTS_ORIGIN

if TYPE_CHECKING:
    from typing import Any

PaymentKind = Literal["renewal", "new_subscription", "donation", "donation_renewal"]
Processor = Literal["web", "worker"]
PAYMENT_KINDS: tuple[PaymentKind, ...] = (
    "renewal",
    "new_subscription",
    "donation",
    "donation_renewal",
)


@override_settings(LANGUAGE_CODE="en")
class PaymentProcessingTest(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user("payment-owner")
        self.customer = Customer.objects.create(
            name="Payment customer", country="US", user_id=-1, origin=PAYMENTS_ORIGIN
        )
        self.customer.owners.add(self.user)
        Package.objects.create(name="community", verbose="Community support", price=0)
        self.package = Package.objects.create(
            name="basic",
            verbose="Basic support",
            price=100,
            category=ServiceCategory.SUPPORT,
        )
        self.donation_package = get_donation_package(1)
        self.expiry = datetime(2027, 1, 1, tzinfo=UTC)
        self.notify = self.enterContext(patch("weblate_web.models.send_notification"))
        self.client.force_login(self.user)

    def prepare_payment(self, kind: PaymentKind) -> None:
        self.notify.reset_mock()
        self.subscription: Subscription | None = None
        extra: dict[str, Any]
        if kind in {"renewal", "donation_renewal"}:
            service = Service.objects.create(
                customer=self.customer,
                kind=ServiceKind.DONATION
                if kind == "donation_renewal"
                else ServiceKind.SERVICE,
            )
            self.subscription = Subscription.objects.create(
                service=service,
                package=self.donation_package
                if kind == "donation_renewal"
                else self.package,
                expires=self.expiry,
            )
            extra = (
                {"donation_service": service.pk}
                if kind == "donation_renewal"
                else {"subscription": self.subscription.pk}
            )
        elif kind == "new_subscription":
            extra = {"subscription": self.package.name, "skip_intro": True}
        else:
            extra = {"reward": 1, "category": "donate"}
        self.payment = Payment.objects.create(
            customer=self.customer,
            amount=100,
            state=Payment.ACCEPTED,
            recurring="y",
            extra=extra,
        )
        self.service_count = Service.objects.count()
        self.subscription_count = Subscription.objects.count()

    def apply_payment(self, processor: Processor, client: Client | None = None) -> None:
        if processor == "worker":
            # Deliberately retain the original accepted instance across retries.
            process_payment(self.payment)
        else:
            response = (client or self.client).get(
                reverse("donate-process"), {"payment": str(self.payment.pk)}
            )
            self.assertEqual(response.status_code, 302)

    def assert_applied_once(self, kind: PaymentKind) -> None:
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.state, Payment.PROCESSED)
        is_new = self.subscription is None
        self.assertEqual(Service.objects.count(), self.service_count + int(is_new))
        self.assertEqual(
            Subscription.objects.count(), self.subscription_count + int(is_new)
        )
        subscription = Subscription.objects.get(payment=payment)
        if not is_new:
            self.assertEqual(subscription.expires, self.expiry + relativedelta(years=1))
        self.assertEqual(self.notify.call_count, int(kind == "new_subscription"))

    def test_worker_retries_stale_payment(self) -> None:
        for kind in PAYMENT_KINDS:
            with self.subTest(kind=kind):
                self.prepare_payment(kind)
                self.apply_payment("worker")
                self.apply_payment("worker")
                self.assertEqual(self.payment.state, Payment.ACCEPTED)
                self.assert_applied_once(kind)

    def test_web_retries_processed_payment(self) -> None:
        for kind in PAYMENT_KINDS:
            with self.subTest(kind=kind):
                self.prepare_payment(kind)
                self.apply_payment("web")
                self.apply_payment("web")
                self.assert_applied_once(kind)

    def test_processing_rolls_back_on_failure(self) -> None:
        original_save = Payment.save

        def fail_processed_save(payment: Payment, *args, **kwargs):
            if payment.state == Payment.PROCESSED:
                raise ValueError("Processing interrupted")
            return original_save(payment, *args, **kwargs)

        processors: tuple[Processor, ...] = ("web", "worker")
        for processor in processors:
            for kind in PAYMENT_KINDS:
                with self.subTest(processor=processor, kind=kind):
                    self.prepare_payment(kind)
                    with (
                        patch.object(
                            Payment,
                            "save",
                            autospec=True,
                            side_effect=fail_processed_save,
                        ),
                        self.assertRaisesMessage(ValueError, "Processing interrupted"),
                    ):
                        self.apply_payment(processor)
                    self.assertEqual(
                        Payment.objects.get(pk=self.payment.pk).state, Payment.ACCEPTED
                    )
                    self.assertEqual(Service.objects.count(), self.service_count)
                    self.assertEqual(
                        Subscription.objects.count(), self.subscription_count
                    )
                    if self.subscription is not None:
                        self.subscription.refresh_from_db()
                        self.assertEqual(self.subscription.expires, self.expiry)
                    self.notify.reset_mock()
                    self.apply_payment(processor)
                    self.assert_applied_once(kind)

    def test_web_requires_ownership_and_local_origin(self) -> None:
        self.prepare_payment("donation")
        self.customer.owners.clear()
        self.apply_payment("web")
        self.customer.owners.add(self.user)
        self.customer.origin = "https://other.example"
        self.customer.save(update_fields=["origin"])
        self.apply_payment("web")
        self.assertEqual(
            Payment.objects.get(pk=self.payment.pk).state, Payment.ACCEPTED
        )
        self.assertEqual(Service.objects.count(), self.service_count)

    def test_nonaccepted_payment_is_not_applied(self) -> None:
        self.prepare_payment("donation")
        for state in (
            Payment.NEW,
            Payment.PENDING,
            Payment.REJECTED,
            Payment.CANCELLED,
        ):
            with self.subTest(state=state):
                Payment.objects.filter(pk=self.payment.pk).update(state=state)
                self.apply_payment("web")
                with self.assertRaises(ValueError):
                    self.apply_payment("worker")
                self.assertEqual(Payment.objects.get(pk=self.payment.pk).state, state)
                self.assertEqual(Service.objects.count(), self.service_count)

    def run_concurrent_processing(self, first: Processor, second: Processor) -> None:
        for kind in PAYMENT_KINDS:
            with self.subTest(kind=kind):
                self.check_concurrent_processing(kind, first, second)

    def check_concurrent_processing(
        self, kind: PaymentKind, first: Processor, second: Processor
    ) -> None:
        self.prepare_payment(kind)
        client = Client()
        client.force_login(self.user)
        entered = Event()
        waiting = Event()
        release = Event()
        process = (
            process_subscription
            if kind in {"renewal", "new_subscription"}
            else process_donation
        )

        def gated_process(payment: Payment):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Payment processing was not released")
            return process(payment)

        def signal_waiting(execute, sql, params, many, context):
            if "FOR UPDATE" in sql and "payments_payment" in sql:
                waiting.set()
            return execute(sql, params, many, context)

        def apply(processor: Processor, *, contender: bool = False) -> None:
            try:
                if contender:
                    with connection.execute_wrapper(signal_waiting):
                        self.apply_payment(processor, client)
                else:
                    self.apply_payment(processor)
            finally:
                connections.close_all()

        helper = (
            "process_subscription"
            if kind in {"renewal", "new_subscription"}
            else "process_donation"
        )
        with (
            patch(f"weblate_web.models.{helper}", side_effect=gated_process) as applied,
            patch(f"weblate_web.views.{helper}", new=applied),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            leader = executor.submit(apply, first)
            try:
                self.assertTrue(entered.wait(timeout=10))
                contender = executor.submit(apply, second, contender=True)
                self.assertTrue(waiting.wait(timeout=10))
                self.assertFalse(contender.done())
            finally:
                release.set()
            leader.result(timeout=20)
            contender.result(timeout=20)
            applied.assert_called_once()
        self.assert_applied_once(kind)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_web_requests(self) -> None:
        self.run_concurrent_processing("web", "web")

    @skipUnlessDBFeature("has_select_for_update")
    def test_web_then_background_processing(self) -> None:
        self.run_concurrent_processing("web", "worker")

    @skipUnlessDBFeature("has_select_for_update")
    def test_background_then_web_processing(self) -> None:
        self.run_concurrent_processing("worker", "web")

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_background_processing(self) -> None:
        self.run_concurrent_processing("worker", "worker")
