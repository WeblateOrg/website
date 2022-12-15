#
# Copyright © 2012–2022 Michal Čihař <michal@cihar.com>
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

from weblate_web.models import Service
from weblate_web.remote import get_activity, get_changes, get_contributors


class Command(BaseCommand):
    help = "refreshes remote data"  # noqa: A003

    def disable_stale_services(self):
        threshold = timezone.now() - timedelta(days=3)
        for service in Service.objects.filter(discoverable=True):
            if service.last_report.timestamp < threshold:
                service.discoverable = False
                service.save(update_fields=["discoverable"])
                self.stdout.write(f"Disabling disoverable for {service}")

    def handle(self, *args, **options):
        self.disable_stale_services()
        get_contributors(True)
        get_activity(True)
        get_changes(True)
