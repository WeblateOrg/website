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

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand

from weblate_web.hetzner import (
    generate_ssh_url,
    generate_subaccount_data,
    get_storage_subaccounts,
    modify_storage_subaccount,
)
from weblate_web.models import Service

if TYPE_CHECKING:
    from datetime import datetime


class Command(BaseCommand):
    help = "syncrhonizes backup API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--delete",
            default=False,
            action="store_true",
            help="Delete stale backup repositories",
        )

    def handle(self, *args, **options):
        backup_services: dict[str, Service] = {
            service.backup_repository: service
            for service in Service.objects.exclude(backup_repository="")
        }
        processed_repositories = set()
        backup_storages = get_storage_subaccounts()

        for storage in backup_storages:
            # Skip non-weblate subaccounts
            homedirectory: str = storage["subaccount"]["homedirectory"]
            if not homedirectory.startswith("weblate/"):
                continue
            # Generate SSH URL used for borg
            ssh_url = generate_ssh_url(storage)
            processed_repositories.add(ssh_url)

            # Fetch matching service
            try:
                service = backup_services[ssh_url]
            except KeyError:
                self.stderr.write(f"unused URL: {ssh_url}")
                continue

            # Skip Hosted Weblate
            if service.site_url == "https://hosted.weblate.org":
                continue

            # Validate service
            customer = service.customer
            if customer is None:
                self.stderr.write(f"missing customer: {service.pk}")
                continue

            # Sync our data
            update = False
            if not service.backup_box:
                service.backup_box = settings.STORAGE_BOX
                update = True
            dirname = homedirectory.removeprefix("weblate/")
            if service.backup_directory != dirname:
                service.backup_directory = dirname
                update = True
            if update:
                self.stdout.write(f"Updating data for {service.pk} ({customer.name})")
                service.save(update_fields=["backup_box", "backup_directory"])

            # Sync Hetzner data
            storage_data = generate_subaccount_data(dirname, service, customer)
            if storage["subaccount"]["comment"] != storage_data["comment"]:
                username: str = storage["subaccount"]["username"]
                self.stdout.write(
                    f"Updating Hetzner data for {username} for {service.pk} ({customer.name})"
                )
                modify_storage_subaccount(username, storage_data)

        for service in backup_services.values():
            if service.has_paid_backup():
                continue
            kind: str = "UNKNOWN"
            expires: datetime | None = None
            if service.hosted_subscriptions:
                kind = "hosted"
                expires = service.hosted_subscriptions[0].expires
            elif service.backup_subscriptions:
                kind = "backup"
                expires = service.backup_subscriptions[0].expires

            self.stderr.write(
                f"not paid {kind}: {service.pk} ({service.customer}) {expires}: {service.backup_directory}"
            )

        for extra in set(backup_services) - processed_repositories:
            self.stderr.write(f"unused: {extra}")
