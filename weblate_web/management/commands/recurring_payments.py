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

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from weblate_web.invoices.models import InvoiceKind
from weblate_web.models import Donation, Service, Subscription, get_period_delta
from weblate_web.payments.models import Payment
from weblate_web.payments.utils import send_notification

if TYPE_CHECKING:
    from collections.abc import Iterable


class Command(BaseCommand):
    help = "issues recurring payments"

    def handle(self, *args, **options):
        # Issue recurring payments
        self.handle_donations()
        self.handle_subscriptions()
        # Update services status
        self.handle_services()
        # Notify about upcoming expiry on Monday and Thursday
        weekday = timezone.now().date().weekday()
        if weekday in {0, 3}:
            self.notify_expiry(weekday)

    @staticmethod
    def notify_expiry(weekday=0):
        expiry: list[tuple[str, Iterable[str], datetime]] = []

        expires_notify = timezone.now() + timedelta(days=30)
        payment_notify_start = timezone.now() + timedelta(days=7)
        if weekday == 0:
            # Monday - Wednesday next week
            payment_notify_end = timezone.now() + timedelta(days=10)
        else:
            # Thursday - Sunday next week
            payment_notify_end = timezone.now() + timedelta(days=11)

        # Expiring subscriptions
        subscriptions = Subscription.objects.filter(
            expires__lte=expires_notify, enabled=True
        ).exclude(payment=None)
        for subscription in subscriptions:
            # Skip one-time payments and the ones with recurrence configured
            if not subscription.package.get_repeat():
                continue
            try:
                payment = subscription.payment_obj
            except Payment.DoesNotExist:
                payment = None
            notify_user = (
                payment_notify_start <= subscription.expires <= payment_notify_end
            )
            if payment is None or payment.recurring:
                if notify_user:
                    subscription.send_notification("payment_upcoming")
                continue
            if notify_user:
                subscription.send_notification("payment_missing")
            if not subscription.could_be_obsolete():
                name = f"{subscription}"
                if subscription.service.note:
                    name = f"{name} ({subscription.service.note})"
                expiry.append(
                    (
                        name,
                        subscription.service.customer.get_notify_emails(),
                        subscription.expires,
                    )
                )

        # Expiring donations
        donations = Donation.objects.filter(
            active=True, expires__lte=expires_notify
        ).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            notify_user = payment_notify_start <= donation.expires <= payment_notify_end
            if payment.recurring:
                if notify_user:
                    donation.send_notification("payment_upcoming")
                continue
            if notify_user:
                donation.send_notification("payment_missing")
            expiry.append(
                (
                    f"{donation.customer}: {donation.get_payment_description()}",
                    donation.customer.get_notify_emails(),
                    donation.expires,
                )
            )

        # Notify admins
        if expiry:
            send_notification(
                "expiring_subscriptions",
                settings.NOTIFY_SUBSCRIPTION,
                expiry=expiry,
            )

    @staticmethod
    def handle_services():
        for service in Service.objects.all():
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
    ):
        # Alllow at most three failures of current payment method
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
    def handle_subscriptions(cls):
        now = timezone.now()
        subscriptions = Subscription.objects.filter(
            expires__range=(now - timedelta(days=10), now + timedelta(days=3)),
            enabled=True,
        ).exclude(payment=None)
        for subscription in subscriptions:
            # Is this repeating subscription?
            if not subscription.package.get_repeat():
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

            # Trigger recurring payment
            cls.peform_payment(
                payment,
                subscription.list_payments(),
                amount=subscription.package.price,
                recurring=subscription.package.get_repeat(),
                end_date=subscription.expires,
                extra={"subscription": subscription.pk},
            )

    @classmethod
    def handle_donations(cls):
        now = timezone.now()
        donations = Donation.objects.filter(
            active=True,
            expires__range=(now - timedelta(days=10), now + timedelta(days=3)),
        ).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            if not payment.recurring:
                donation.send_notification("payment_expired")
                continue

            cls.peform_payment(
                payment,
                donation.list_payments(),
                recurring=payment.recurring,
                end_date=donation.expires,
                extra={"donation": donation.pk},
            )
