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
        self.handle_recurring_payments()
        # Update services status
        self.handle_services()
        # Notify about upcoming expiry
        self.notify_expiry()

    @staticmethod
    def notify_expiry(*, force_summary: bool = False) -> None:
        expiry: list[tuple[str, Iterable[str], datetime]] = []
        timestamp = timezone.now()

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
            if payment is not None and payment.recurring:
                if notify_user:
                    subscription.send_notification("payment_upcoming")
                continue
            if notify_user:
                subscription.send_notification("payment_missing")
            if (
                subscription.expires <= summary_notify
                and not subscription.could_be_obsolete()
            ):
                expiry.append(subscription.get_expiry_summary())

        # Notify admins
        if expiry and (timestamp.day == 1 or force_summary):
            send_notification(
                "expiring_subscriptions",
                settings.NOTIFY_SUBSCRIPTION,
                expiry=expiry,
            )

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
            # TODO: use package from the current subscriptions instead of copying
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
    def handle_recurring_payments(cls, service_kind: ServiceKind | None = None) -> None:
        now = timezone.now()
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
                continue

            recurring = subscription.get_recurring_payment_period()
            if not recurring:
                subscription.send_notification("payment_expired")
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

    @classmethod
    def handle_subscriptions(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.SERVICE)

    @classmethod
    def handle_donations(cls) -> None:
        cls.handle_recurring_payments(ServiceKind.DONATION)
