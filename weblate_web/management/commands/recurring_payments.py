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

from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import sentry_sdk
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import get_language, override

from weblate_web.crm.renewals import reconcile_renewal_follow_ups
from weblate_web.invoices.models import InvoiceKind
from weblate_web.models import (
    Service,
    ServiceKind,
    Subscription,
    get_period_delta,
    process_payment,
)
from weblate_web.payments.backends import PaymentError, ThePay2Card
from weblate_web.payments.models import Customer, Payment
from weblate_web.payments.utils import send_notification

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from decimal import Decimal
    from typing import Any

    from weblate_web.payments.models import PaymentQuerySet


def thepay_renewals() -> PaymentQuerySet:
    return Payment.objects.filter(
        backend__startswith="thepay2-",
        repeat__isnull=False,
        details__has_key="recurring_attempt",
    )


def unresolved_thepay_renewals() -> PaymentQuerySet:
    return thepay_renewals().filter(
        Q(state__in=(Payment.NEW, Payment.PENDING, Payment.ACCEPTED))
        | (
            Q(state=Payment.CANCELLED)
            & (
                Q(details__response__state__isnull=True)
                | ~Q(
                    details__response__state__in=(
                        ThePay2Card.failed_payment_states
                        | ThePay2Card.closed_preauthorization_states
                    )
                )
            )
        )
    )


@transaction.atomic
def process_accepted_renewal(payment: Payment) -> None:
    if (
        accepted := Payment.objects.select_for_update()
        .filter(pk=payment.pk, state=Payment.ACCEPTED)
        .first()
    ) is not None:
        process_payment(accepted)


@transaction.atomic
def reconcile_thepay_renewal(payment: Payment) -> None:
    backend = payment.get_payment_backend()
    current = backend.payment
    # Older runs left the child NEW when the POST raised a payment error.
    if current.state == Payment.NEW:
        current.state = Payment.PENDING
        current.save(update_fields=["state"])
    if current.state not in {Payment.PENDING, Payment.CANCELLED}:
        return
    try:
        with override(current.details.get("thepay_language") or settings.LANGUAGE_CODE):
            backend.complete_notification()
    except PaymentError as error:
        if error.status_code != HTTPStatus.NOT_FOUND:
            sentry_sdk.capture_exception(error)
        elif not current.details.get("thepay_renewal_not_found"):
            current.details["thepay_renewal_not_found"] = True
            current.save(update_fields=["details"])
            sentry_sdk.capture_message(
                "ThePay renewal requires review: payment detail returned 404",
                level="warning",
                extras={"payment_id": str(current.pk)},
            )


def submit_renewal(payment: Payment, payload: dict[str, Any] | None) -> None:
    if payload is None:
        payment.trigger_recurring()
        return
    with transaction.atomic():
        backend = payment.get_payment_backend()
        if backend.payment.state == Payment.PENDING:
            backend.charge_saved_authorization(payload)
            backend.complete(None)
    process_accepted_renewal(payment)


def reserve_thepay_charge(backend: ThePay2Card, attempt: int) -> dict[str, Any]:
    child = backend.payment
    if child.amount_fixed and not child.draft_invoice_id and not child.paid_invoice_id:
        child.normalize_fixed_amount()
    payload = backend.saved_authorization_payload()
    child.details["recurring_attempt"] = attempt
    child.details["thepay_language"] = get_language() or settings.LANGUAGE_CODE
    child.state = Payment.PENDING
    child.save(update_fields=["amount", "requested_amount", "state", "details"])
    return payload


class Command(BaseCommand):
    help = "issues recurring payments"

    def handle(self, *args, **options) -> None:
        # Issue recurring payments
        disable_donations = self.handle_recurring_payments(
            defer_one_time_donation_disable=True
        )
        # Update services status
        self.handle_services()
        reconcile_renewal_follow_ups()
        # Notify about upcoming expiry
        self.notify_expiry(disable_one_time_donations=disable_donations)

    @staticmethod
    def disable_expired_one_time_donation(
        subscription: Subscription, payment: Payment | None, timestamp: datetime
    ) -> None:
        if subscription.expires < timestamp and (
            payment is None or not payment.recurring
        ):
            subscription.enabled = False
            subscription.save(update_fields=["enabled"])

    @classmethod
    def notify_expiry(
        cls,
        *,
        force_summary: bool = False,
        disable_one_time_donations: Iterable[tuple[Subscription, Payment | None]] = (),
    ) -> None:
        expiry: dict[int | None, tuple[str, Iterable[str], datetime]] = {}
        timestamp = timezone.now()
        deferred_payments = {
            subscription.pk: payment
            for subscription, payment in disable_one_time_donations
        }
        deferred_disable: dict[int | None, tuple[Subscription, Payment | None]] = {}

        summary_notify = timestamp + timedelta(days=31)
        customer_notification_days = (
            Customer.objects.aggregate(Max("upcoming_payment_notification_days"))[
                "upcoming_payment_notification_days__max"
            ]
            or 0
        )
        expires_notify = timestamp + timedelta(days=max(31, customer_notification_days))

        subscriptions = Subscription.objects.payment_lifecycle().filter(
            expires__lte=expires_notify,
        )
        for subscription in subscriptions:
            if not subscription.uses_payment_lifecycle():
                continue
            if (
                not subscription.service.is_donation
                and subscription.has_accepted_renewal_payment()
            ):
                continue
            try:
                payment = subscription.payment_obj
            except Payment.DoesNotExist:
                payment = None
            notify_user = subscription.should_notify(timestamp)
            disable_after_summary = subscription.pk in deferred_payments
            if payment is not None and payment.recurring:
                if notify_user:
                    subscription.send_notification("payment_upcoming")
                continue
            if notify_user and not disable_after_summary:
                subscription.send_notification("payment_missing")
            if (
                subscription.expires <= summary_notify
                and not subscription.could_be_obsolete()
            ):
                expiry[subscription.pk] = subscription.get_expiry_summary()
            if subscription.service.is_donation and disable_after_summary:
                deferred_disable[subscription.pk] = (
                    subscription,
                    deferred_payments[subscription.pk],
                )
            elif subscription.service.is_donation and (
                notify_user or subscription.expires < timestamp - timedelta(days=10)
            ):
                cls.disable_expired_one_time_donation(subscription, payment, timestamp)

        # Notify admins
        if expiry and (timestamp.day == 1 or force_summary):
            send_notification(
                "expiring_subscriptions",
                settings.NOTIFY_SUBSCRIPTION,
                expiry=list(expiry.values()),
            )

        for subscription, payment in deferred_disable.values():
            cls.disable_expired_one_time_donation(subscription, payment, timestamp)

    @staticmethod
    def handle_services() -> None:
        for service in Service.objects.customer_services():
            service.update_status()
            service.create_backup()

    @staticmethod
    def peform_payment(  # ruff:ignore[too-many-arguments]
        payment: Payment,
        past_payments: PaymentQuerySet,
        *,
        recurring: str,
        end_date: datetime,
        amount: Decimal | int | None = None,
        extra: dict[str, int],
    ) -> tuple[Payment, dict[str, Any] | None] | None:
        # Allow at most three failures of current payment method
        repeated_payments = past_payments.filter(repeat=payment.repeat or payment)
        rejected_payments = repeated_payments.filter(state=Payment.REJECTED)
        last_processed = (
            repeated_payments.filter(state=Payment.PROCESSED)
            .order_by("-created")
            .first()
        )
        if last_processed is not None:
            rejected_payments = rejected_payments.filter(
                created__gt=last_processed.created
            )
        attempt = rejected_payments.count() + 1
        if attempt > Payment.RECURRING_ATTEMPT_LIMIT:
            if last_failure := rejected_payments.order_by("-created").first():
                payment.disable_recurring(
                    last_failure,
                    reason=Payment.RECURRING_ATTEMPTS_EXHAUSTED,
                    failed=True,
                )
            return None

        if payment.customer.vat:
            validation_failed = False
            validation_detail = ""
            try:
                payment.customer.prepayment_validation(automated=True)
            except ValidationError as error:
                validation_failed = True
                validation_detail = strip_tags(str(error.message))
            if payment.customer.vat_recently_invalid:
                validation_failed = True
                validation_detail = strip_tags(
                    payment.customer.vat_validation_error["message"]
                )
            if validation_failed:
                repeated = payment.repeat_payment(
                    skip_previous=True, amount=amount, extra=extra
                )
                if not repeated:
                    payment.recurring = ""
                    payment.save()
                    return None
                repeated.state = Payment.REJECTED
                repeated.details["recurring_attempt"] = attempt
                repeated.details["failure_code"] = Payment.VAT_VALIDATION_FAILURE
                if validation_detail:
                    repeated.details["failure_detail"] = validation_detail
                repeated.save(update_fields=["state", "details"])
                repeated.customer.send_notification("payment_failed", payment=repeated)
                return None

        # Create repeated payment
        if payment.paid_invoice and "donation_service" not in extra:
            if subscription_id := extra.get("subscription"):
                subscription = Subscription.objects.get(pk=subscription_id)
                invoice = subscription.create_invoice(kind=InvoiceKind.DRAFT)
            else:
                invoice = payment.paid_invoice.duplicate(
                    kind=InvoiceKind.DRAFT,
                    extra=extra,
                    start_date=end_date + timedelta(days=1),
                    end_date=end_date + get_period_delta(recurring),
                )
            repeated = invoice.create_payment(
                recurring=recurring, backend=payment.backend, repeat=payment
            )
        else:
            repeated = payment.repeat_payment(amount=amount, extra=extra)

        # Backend does not support it
        if not repeated:
            # Remove recurring flag
            payment.recurring = ""
            payment.save()
            return None

        backend_class = repeated.get_payment_backend_class()
        if issubclass(backend_class, ThePay2Card):
            backend = backend_class(repeated)
            return backend.payment, reserve_thepay_charge(backend, attempt)

        repeated.details["recurring_attempt"] = attempt
        repeated.save(update_fields=["details"])
        return repeated, None

    @classmethod
    @transaction.atomic(durable=True)
    def prepare_renewal(
        cls,
        candidate: Subscription,
        timestamp: datetime,
        disable_donations: list[tuple[Subscription, Payment | None]] | None,
    ) -> tuple[Payment, dict[str, Any] | None] | None:
        # Recheck the renewal under the lock, and require the UID to commit
        # before the caller is allowed to submit the charge.
        subscription = (
            Subscription.objects.payment_lifecycle()
            .select_for_update()
            .filter(pk=candidate.pk, expires=candidate.expires)
            .first()
        )
        if (
            subscription is None
            or subscription.payment_id != candidate.payment_id
            or not subscription.uses_payment_lifecycle()
            or subscription.could_be_obsolete()
        ):
            return None

        extra = subscription.get_renewal_extra()
        target = {f"extra__{key}": value for key, value in extra.items()}
        if (
            unresolved_thepay_renewals().filter(**target).exists()
            or thepay_renewals().filter(created__gte=timestamp, **target).exists()
        ):
            return None

        # Match the renewal period independently of payment state.
        if (
            not subscription.service.is_donation
            and next(subscription.get_renewal_invoices(), None) is not None
        ):
            subscription.send_notification("payment_expired")
            return None

        payment = subscription.payment_obj
        if not payment.recurring or not (
            recurring := subscription.get_recurring_payment_period()
        ):
            subscription.send_notification("payment_expired")
            if subscription.service.is_donation:
                if disable_donations is None:
                    cls.disable_expired_one_time_donation(
                        subscription, payment, timestamp
                    )
                else:
                    disable_donations.append((subscription, payment))
            return None

        return cls.peform_payment(
            payment,
            subscription.list_payments(),
            amount=subscription.get_renewal_amount(),
            recurring=recurring,
            end_date=subscription.expires,
            extra=extra,
        )

    @classmethod
    def handle_recurring_payments(
        cls,
        service_kind: ServiceKind | None = None,
        *,
        defer_one_time_donation_disable: bool = False,
    ) -> list[tuple[Subscription, Payment | None]]:
        now = timezone.now()
        disable_donations: list[tuple[Subscription, Payment | None]] = []
        unresolved = unresolved_thepay_renewals()
        if service_kind == ServiceKind.DONATION:
            unresolved = unresolved.filter(extra__has_key="donation_service")
        elif service_kind == ServiceKind.SERVICE:
            unresolved = unresolved.filter(extra__has_key="subscription")
        else:
            unresolved = unresolved.filter(
                extra__has_any_keys=["subscription", "donation_service"]
            )
        reconciled_targets = set()
        for payment in list(unresolved):
            target_key = (
                "donation_service"
                if "donation_service" in payment.extra
                else "subscription"
            )
            reconciled_targets.add((target_key, payment.extra[target_key]))
            try:
                if payment.state != Payment.ACCEPTED:
                    reconcile_thepay_renewal(payment)
                process_accepted_renewal(payment)
            except Exception as error:  # pylint: disable=broad-exception-caught
                # A failed settlement keeps this renewal on hold without
                # preventing the rest of the batch from running.
                sentry_sdk.capture_exception(error)

        subscriptions = Subscription.objects.payment_lifecycle().filter(
            expires__range=(now - timedelta(days=10), now + timedelta(days=3)),
        )
        if service_kind is not None:
            subscriptions = subscriptions.filter(service__kind=service_kind)
        for subscription in subscriptions:
            if reconciled_targets.intersection(
                subscription.get_renewal_extra().items()
            ):
                continue
            try:
                prepared = cls.prepare_renewal(
                    subscription,
                    now,
                    disable_donations if defer_one_time_donation_disable else None,
                )
                if prepared is not None:
                    submit_renewal(*prepared)
            except PaymentError as error:
                sentry_sdk.capture_exception(error)
        return disable_donations

    @classmethod
    def handle_subscriptions(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.SERVICE)

    @classmethod
    def handle_donations(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.DONATION)
