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

from weblate_web.models import Service


class Command(BaseCommand):
    help = "cleanups services"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--delete",
            default=False,
            action="store_true",
            help="Delete stale services",
        )

    def handle(self, delete: bool, **kwargs):
        for service in Service.objects.filter(
            subscription__isnull=True, report__isnull=True
        ):
            print(service, service.subscription_set.all(), service.report_set.all())  # noqa: T201
