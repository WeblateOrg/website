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

from django.core.management.base import BaseCommand

from weblate_web.models import Donation, Subscription


class Command(BaseCommand):
    help = "lists payments using obsolete payment method"

    def handle(self, *args, **options):
        # Expiring subscriptions
        subscriptions = Subscription.objects.exclude(payment=None)
        for subscription in subscriptions:
            # Skip one-time payments and the ones with recurrence configured
            if not subscription.get_repeat():
                continue
            payment = subscription.payment_obj
            if payment.backend != "thepay-card":
                continue
            if payment.details["methodId"] != "21":
                continue
            self.stdout.write(
                "{}, expires {} [{}]: {}".format(
                    subscription,
                    subscription.expires.date(),
                    subscription.get_repeat(),
                    ", ".join(
                        subscription.service.users.values_list("email", flat=True)
                    ),
                )
            )

        # Expiring donations
        donations = Donation.objects.filter(active=True).exclude(payment=None)
        for donation in donations:
            payment = donation.payment_obj
            if payment.backend != "thepay-card":
                continue
            if payment.details["methodId"] != "21":
                continue
            self.stdout.write(
                "{}, expires {} [{}]: {}".format(
                    donation.get_payment_description(),
                    donation.expires.date(),
                    "y",
                    donation.user.email,
                )
            )
