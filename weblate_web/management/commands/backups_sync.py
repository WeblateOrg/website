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

from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from weblate_web.hetzner import (
    generate_ssh_url,
    generate_subaccount_data,
    get_directory_summary,
    get_service_backup_readme,
    get_storage_subaccounts,
    modify_storage_subaccount,
    sftp_client,
)
from weblate_web.models import Service


class Command(BaseCommand):
    help = "synchronizes backup API"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--delete",
            default=False,
            action="store_true",
            help="Delete stale backup repositories",
        )
        parser.add_argument(
            "--skip-scan",
            default=False,
            action="store_true",
            help="Skip SFTP scan",
        )

    def scan_directories(self, backup_services: dict[str, Service]) -> None:
        with sftp_client() as ftp:
            for service in backup_services.values():
                size, mtime = get_directory_summary(ftp, service.backup_directory)
                timestamp = datetime.fromtimestamp(mtime, tz=UTC)
                if service.backup_size != size or service.backup_timestamp != timestamp:
                    service.backup_size = size
                    service.backup_timestamp = timestamp
                    service.save(update_fields=["backup_size", "backup_timestamp"])

    def sync_data(self, backup_services: dict[str, Service]) -> set[str]:
        processed_repositories = set()
        backup_storages = get_storage_subaccounts()

        for storage in backup_storages:
            # Skip non-weblate subaccounts and admin account
            homedirectory: str = storage["home_directory"]
            if not homedirectory.startswith("weblate/") or homedirectory == "weblate/":
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

            customer = service.customer

            # Sync our data
            update = False
            if not service.backup_box:
                service.backup_box = settings.STORAGE_BOX
                update = True
            dirname = homedirectory.removeprefix("weblate/")
            if service.backup_directory != dirname:
                service.backup_directory = dirname
                update = True
            if service.backup_subaccount != storage["id"]:
                service.backup_subaccount = storage["id"]
                update = True
            if update:
                self.stdout.write(
                    f"Updating data for {service.pk} {service.site_domain} ({customer.name})"
                )
                service.save(
                    update_fields=[
                        "backup_box",
                        "backup_directory",
                        "backup_subaccount",
                    ]
                )

            # Sync Hetzner data
            storage_data = generate_subaccount_data(
                dirname, service, access=service.has_paid_backup()
            )
            if any(storage[field] != value for field, value in storage_data.items()):  # type: ignore[literal-required]
                username: str = storage["username"]
                self.stdout.write(
                    f"Updating Hetzner data for {username} for {service.pk} {service.site_domain} ({customer.name})"
                )
                modify_storage_subaccount(storage["id"], storage_data)

        return processed_repositories

    def remove_disabled(self, backup_services: dict[str, Service]) -> None:
        for service in backup_services.values():
            # Skip services with paid backup
            if service.has_paid_backup():
                continue

            # Skip still enabled subscriptions
            if (
                service.hosted_subscriptions and service.hosted_subscriptions[0].enabled
            ) or (
                service.backup_subscriptions and service.backup_subscriptions[0].enabled
            ):
                continue

            self.stdout.write(f"Removing backup repository for {service}...")

    def check_unpaid(self, backup_services: dict[str, Service]) -> None:
        for service in backup_services.values():
            if service.has_paid_backup():
                continue
            kind: str = "UNKNOWN"
            expires: datetime | None = None
            enabled = False
            if service.hosted_subscriptions:
                kind = "hosted"
                expires = service.hosted_subscriptions[0].expires
                enabled = service.hosted_subscriptions[0].enabled
            elif service.backup_subscriptions:
                kind = "backup"
                expires = service.backup_subscriptions[0].expires
                enabled = service.backup_subscriptions[0].enabled

            status = "disabled" if not enabled else "not paid"

            self.stderr.write(f"{status} {kind}: {service.pk} {service.customer}")
            self.stderr.write(f"  url: {service.site_url}")
            self.stderr.write(f"  directory: {service.backup_directory}")
            self.stderr.write(f"  expires: {expires}")
            self.stderr.write(f"  size: {service.backup_size}")
            self.stderr.write(f"  mtime: {service.backup_timestamp}")

    def update_readme(self, backup_services: dict[str, Service]) -> None:
        with sftp_client() as ftp:
            for service in backup_services.values():
                filename = f"{service.backup_directory}/README.txt"
                try:
                    with ftp.open(filename, "r") as handle:
                        content = handle.read().decode()
                except OSError:
                    content = ""

                readme = get_service_backup_readme(service)
                if readme != content:
                    self.stdout.write(f"updating {filename} for {service.customer}")
                    with ftp.open(filename, "w") as handle:
                        handle.write(readme)

    def handle(self, delete: bool, skip_scan: bool, **kwargs) -> None:
        backup_services: dict[str, Service] = {
            service.backup_repository: service
            for service in Service.objects.exclude(backup_repository="")
        }

        processed_repositories = self.sync_data(backup_services)

        if not skip_scan:
            self.scan_directories(backup_services)

        for extra in set(backup_services) - processed_repositories:
            self.stderr.write(f"unused: {extra}")

        self.update_readme(backup_services)

        self.remove_disabled(backup_services)

        self.check_unpaid(backup_services)
