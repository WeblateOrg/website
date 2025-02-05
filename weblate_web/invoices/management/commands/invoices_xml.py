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

from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import now

from weblate_web.invoices.models import Invoice, InvoiceKind


class Command(BaseCommand):
    help = "creates a XML export of invoices for previous month"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--refresh",
            default=False,
            action="store_true",
            help="Refresh individual XML files",
        )

    def handle(self, refresh: bool, **kwargs) -> None:
        if settings.INVOICES_COPY_PATH is None:
            raise CommandError("Invoices output path is not configured!")
        previous_month = now() - timedelta(days=28)
        date_start = previous_month.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        date_end = date_start + relativedelta(months=1)
        self.stdout.write(f"Dumping invoices from {date_start} to {date_end}")

        invoices = Invoice.objects.filter(
            kind=InvoiceKind.INVOICE, issue_date__range=(date_start, date_end)
        ).order_by("number")

        output_file = (
            settings.INVOICES_COPY_PATH
            / f"{date_start.year:d}"
            / f"{date_start.month:02d}"
            / "faktury.xml"
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)

        document, invoices_root = Invoice.get_invoice_xml_root()
        for invoice in invoices:
            invoice.get_xml_tree(invoices_root)
            if refresh:
                invoice.generate_xml()
                invoice.sync_files()

        Invoice.save_invoice_xml(document, output_file)
