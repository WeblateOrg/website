#
# Copyright © 2012–2021 Michal Čihař <michal@cihar.com>
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

from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from payments.models import Payment
from payments.utils import send_notification
from weblate_web.models import Donation, Service, Subscription


class Command(BaseCommand):
    help = "issues recurring payments"

    def handle(self, *args, **options):
        # Issue recurring payments
        self.handle_donations()
        self.handle_subscriptions()
        # Update services status
        self.handle_services()
        # Notify about upcoming expiry on Monday and Thursday
        weekday = datetime.today().weekday()
        if weekday in (0, 3):
            self.notify_expiry(weekday)

    @staticmethod
    def notify_expiry(weekday=0):
        expiry = []

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
            expires__lte=expires_notify
        ).exclude(payment=None)
        for subscription in subscriptions:
            payment = subscription.payment_obj
            # Skip one-time payments and the ones with recurrence configured
            if not subscription.get_repeat():
                continue
            notify_user = (
                payment_notify_start <= subscription.expires <= payment_notify_end
            )
            if payment.recurring:
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
                        subscription.service.users.values_list("email", flat=True),
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
                    f"{donation.user}: {donation.get_payment_description()}",
                    [donation.user.email],
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
    def peform_payment(payment, past_payments):
        # Alllow at most three failures of current payment method
        rejected_payments = past_payments.filter(
            state=Payment.REJECTED, repeat=payment.repeat or payment
        )
        if rejected_payments.count() > 3:
            payment.recurring = ""
            payment.save()
            return

        # Create repeated payment
        repeated = payment.repeat_payment()

        # Backend does not support it
        if not repeated:
            # Remove recurring flag
            payment.recurring = ""
            payment.save()
            return

        # Remote trigger of the payment
        repeated.trigger_remotely()

    @classmethod
    def handle_subscriptions(cls):
        now = timezone.now()
        subscriptions = Subscription.objects.filter(
            expires__range=(now - timedelta(days=10), now + timedelta(days=3))
        ).exclude(payment=None)
        for subscription in subscriptions:
            # Is this repeating subscription?
            if not subscription.get_repeat():
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
            cls.peform_payment(payment, subscription.list_payments())

    @classmethod
    def handle_donations(cls):
        donations = Donation.objects.filter(
            active=True, expires__lte=timezone.now() + timedelta(days=3)
        ).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            if not payment.recurring:
                donation.send_notification("payment_expired")
                continue

            cls.peform_payment(payment, donation.list_payments())
