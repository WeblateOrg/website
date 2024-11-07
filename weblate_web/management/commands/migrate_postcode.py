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

import re

from django.core.management.base import BaseCommand
from django.urls import reverse

from weblate_web.payments.models import Customer

POSTCODE_RE = re.compile(
    r"\b((?:(?:[A-Z]{2,} ?)?[0-9][0-9- ]{2,}[0-9])|[0-9]{4}|[A-Z0-9]{4} ?[A-Z0-9]{3})\b"
)


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

            matches = POSTCODE_RE.findall(customer.city)
            if not matches:
                continue
            if len(matches) > 1:
                self.stderr.write(
                    f"{url}: too many matches {customer.city!r}: {matches}"
                )
                continue
            postcode = matches[0]
            city = customer.city.strip().removeprefix(postcode).removesuffix(postcode)
            if city == customer.city:
                self.stderr.write(
                    f"{url}: too many matches {customer.city!r}: {postcode} in middle"
                )
                continue
            city = city.strip().strip(",").strip()

            self.stdout.write(f"{url}: {customer.city!r} -> {city!r} {postcode!r}")
