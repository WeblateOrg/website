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
from django.urls import reverse

from weblate_web.payments.models import Customer


class Command(BaseCommand):
    help = "migrate postcode"

    def add_arguments(self, parser):
        parser.add_argument(
            "--perform",
            default=False,
            action="store_true",
            help="Actually perform changes",
        )

    def handle(self, *args, **options):
        for customer in Customer.objects.filter(postcode="", city__contains=" "):
            url = reverse(
                "admin:payments_customer_change", kwargs={"object_id": customer.pk}
            )
            self.stdout.write(f"{url}: {customer.city}")
