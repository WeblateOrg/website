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
    help = "lists contacts"

    def handle(self, *args, **options):
        emails = set()
        for service in Service.objects.filter(
            status__in={"hosted", "shared", "basic", "extended", "premium"}
        ).prefetch_related("customer__users"):
            emails.update(service.customer.get_notify_emails())

        emails.discard("")

        for email in sorted(emails):
            self.stdout.write(email)
