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

from weblate_web.remote import fetch_vat_info


class Command(BaseCommand):
    help = "refreshes VAT caches"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--all",
            default=False,
            action="store_true",
            help="Fetch all VAT caches",
        )
        parser.add_argument(
            "--delay",
            default=30,
            type=int,
            help="Delay between API requests",
        )

    def handle(self, *args, **options) -> None:
        fetch_vat_info(fetch_all=options["all"], delay=options["delay"])
