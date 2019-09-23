# -*- coding: utf-8 -*-
#
# Copyright © 2012–2019 Michal Čihař <michal@cihar.com>
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

from django.core.management.base import BaseCommand
from django.utils import timezone
from wlhosted.payments.models import Payment

from weblate_web.models import Donation, Subscription


class Command(BaseCommand):
    help = "issues recurring payments"

    def handle(self, *args, **options):
        # Issue recurring payments
        self.handle_donations()
        self.handle_subscriptions()

    @staticmethod
    def handle_subscriptions():
        subscriptions = Subscription.objects.filter(
            expires__lte=timezone.now() + timedelta(days=3)
        ).exclude(
            payment=None
        )
        for subscription in subscriptions:
            payment = subscription.payment_obj
            if not payment.recurring:
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
        ).exclude(
            payment=None
        )
        for donation in donations:
            payment = donation.payment_obj
            if not payment.recurring:
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
