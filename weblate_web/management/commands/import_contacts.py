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

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django_countries import countries
from fakturace.storage import InvoiceStorage

from weblate_web.payments.models import Customer


class Command(BaseCommand):
    help = "import contacts"

    def handle(self, *args, **options):
        invoice_storage = InvoiceStorage(settings.PAYMENT_FAKTURACE)
        contacts_path = Path(invoice_storage.path(invoice_storage.contacts))
        for match in contacts_path.glob("*.ini"):
            name = match.stem
            if name.startswith("web-"):
                # Web contacts are managed in web
                continue
            contact = dict(invoice_storage.parse_contact(name)["contact"])
            vat = contact.get("vat_reg")
            if vat and Customer.objects.filter(vat=vat).exists():
                self.stdout.write(f"{vat} already exists")
                continue
            try:
                obj, created = Customer.objects.get_or_create(
                    vat=vat,
                    name=contact["name"],
                    address=contact["address"],
                    city=contact["city"],
                    country=countries.by_name(contact["country"]),
                    user_id=-1,
                    origin="https://weblate.org/auto",
                    email=contact.get("email", ""),
                )
            except KeyError as error:
                self.stderr.write(f"{name}: failed with {error!r}")
            else:
                if created:
                    self.stdout.write(f"{obj} created")
                else:
                    self.stdout.write(f"{obj} already exists")
