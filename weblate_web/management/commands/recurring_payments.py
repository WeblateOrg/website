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
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

from weblate_web.invoices.models import InvoiceKind
from weblate_web.models import Service, ServiceKind, Subscription, get_period_delta
from weblate_web.payments.models import Customer, Payment
from weblate_web.payments.utils import send_notification

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


class Command(BaseCommand):
    help = "issues recurring payments"

    def handle(self, *args, **options) -> None:
        # Issue recurring payments
        disable_donations = self.handle_recurring_payments(
            defer_one_time_donation_disable=True
        )
        # Update services status
        self.handle_services()
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
    def peform_payment(  # noqa: PLR0913
        payment,
        past_payments,
        *,
        recurring: str,
        end_date: datetime,
        amount: int | None = None,
        extra: dict[str, int],
    ) -> None:
        # Allow at most three failures of current payment method
        rejected_payments = past_payments.filter(
            state=Payment.REJECTED, repeat=payment.repeat or payment
        )
        if rejected_payments.count() > 3:
            payment.recurring = ""
            payment.save()
            return

        # Create repeated payment
        if payment.paid_invoice:
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
            return

        # Trigger of the payment
        repeated.trigger_recurring()

    @classmethod
    def handle_recurring_payments(
        cls,
        service_kind: ServiceKind | None = None,
        *,
        defer_one_time_donation_disable: bool = False,
    ) -> list[tuple[Subscription, Payment | None]]:
        now = timezone.now()
        disable_donations: list[tuple[Subscription, Payment | None]] = []
        subscriptions = Subscription.objects.payment_lifecycle().filter(
            expires__range=(now - timedelta(days=10), now + timedelta(days=3)),
        )
        if service_kind is not None:
            subscriptions = subscriptions.filter(service__kind=service_kind)
        for subscription in subscriptions:
            if not subscription.uses_payment_lifecycle():
                continue

            # Skip this in case there is another subscription, for example on service
            # upgrade on downgrade
            if subscription.could_be_obsolete():
                continue

            # Check recurring payment
            payment = subscription.payment_obj
            if not payment.recurring:
                subscription.send_notification("payment_expired")
                if subscription.service.is_donation:
                    if defer_one_time_donation_disable:
                        disable_donations.append((subscription, payment))
                    else:
                        cls.disable_expired_one_time_donation(
                            subscription, payment, now
                        )
                continue

            recurring = subscription.get_recurring_payment_period()
            if not recurring:
                subscription.send_notification("payment_expired")
                if subscription.service.is_donation:
                    if defer_one_time_donation_disable:
                        disable_donations.append((subscription, payment))
                    else:
                        cls.disable_expired_one_time_donation(
                            subscription, payment, now
                        )
                continue

            # Trigger recurring payment
            cls.peform_payment(
                payment,
                subscription.list_payments(),
                amount=subscription.get_renewal_amount(),
                recurring=recurring,
                end_date=subscription.expires,
                extra=subscription.get_renewal_extra(),
            )
        return disable_donations

    @classmethod
    def handle_subscriptions(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.SERVICE)

    @classmethod
    def handle_donations(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.DONATION)
