#
# Copyright © 2012–2020 Michal Čihař <michal@cihar.com>
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

from datetime import timedelta

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
        # Notify about upcoming expiry
        self.notify_expiry()

    @staticmethod
    def notify_expiry():
        expiry = []

        # Expiring subscriptions
        subscriptions = Subscription.objects.filter(
            expires__lte=timezone.now() + timedelta(days=30)
        ).exclude(payment=None)
        for subscription in subscriptions:
            payment = subscription.payment_obj
            if not payment.recurring:
                expiry.append(
                    (
                        str(subscription),
                        subscription.service.users.values_list("email", flat=True),
                    )
                )

        # Expiring donations
        donations = Donation.objects.filter(
            active=True, expires__lte=timezone.now() + timedelta(days=3)
        ).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            if not payment.recurring:
                expiry.append(
                    (
                        str(subscription),
                        subscription.service.users.values_list("email", flat=True),
                    )
                )

        # Notify admins
        if expiry:
            send_notification(
                "expiring_subscriptions", settings.NOTIFY_SUBSCRIPTION, expiry=expiry,
            )

    @staticmethod
    def handle_services():
        for service in Service.objects.all():
            service.update_status()
            service.create_backup()

    @staticmethod
    def handle_subscriptions():
        subscriptions = Subscription.objects.filter(
            expires__lte=timezone.now() + timedelta(days=3)
        ).exclude(payment=None)
        for subscription in subscriptions:
            payment = subscription.payment_obj
            if not payment.recurring:
                if subscription.get_repeat():
                    subscription.send_notification("payment_expired")
                continue

            # Alllow at most three failures
            rejected_payments = subscription.list_payments().filter(
                state=Payment.REJECTED
            )
            if rejected_payments.count() > 3:
                payment.recurring = ""
                payment.save()
                continue

            repeated = payment.repeat_payment()
            if not repeated:
                # Remove recurring flag
                payment.recurring = ""
                payment.save()
            else:
                repeated.trigger_remotely()

    @staticmethod
    def handle_donations():
        donations = Donation.objects.filter(
            active=True, expires__lte=timezone.now() + timedelta(days=3)
        ).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            if not payment.recurring:
                donation.send_notification("payment_expired")
                continue

            # Alllow at most three failures
            rejected_payments = donation.list_payments().filter(state=Payment.REJECTED)
            if rejected_payments.count() > 3:
                payment.recurring = ""
                payment.save()
                continue

            repeated = payment.repeat_payment()
            if not repeated:
                # Remove recurring flag
                payment.recurring = ""
                payment.save()
            else:
                repeated.trigger_remotely()
