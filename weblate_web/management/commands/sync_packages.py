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

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand

from weblate_web.models import Package, PackageCategory
from weblate_web.packages import (
    DEDICATED_LIMIT,
    DEDICATED_PREFIX,
    HOSTED_PREFIX,
    PACKAGE_NAMES,
    PACKAGES,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class Command(BaseCommand):
    help = "sync packages"

    def get_packages(self) -> Generator[tuple[PackageCategory, str, str, int, int]]:
        for limit, price in PACKAGES.items():
            name = PACKAGE_NAMES[limit]
            if limit >= DEDICATED_LIMIT:
                yield (
                    PackageCategory.PACKAGE_DEDICATED,
                    f"Weblate hosting ({name} strings, dedicated, yearly)",
                    f"{DEDICATED_PREFIX}{name.lower()}",
                    limit,
                    price,
                )
            yield (
                PackageCategory.PACKAGE_SHARED,
                f"Weblate hosting ({name} strings, yearly)",
                f"{HOSTED_PREFIX}{name.lower()}",
                limit,
                price,
            )

    def handle(self, *args, **options) -> None:
        for category, verbose, name, limit, price in self.get_packages():
            package, created = Package.objects.get_or_create(
                limit_hosted_strings=limit,
                category=category,
                defaults={
                    "verbose": verbose,
                    "name": name,
                    "price": price,
                },
            )
            if created:
                self.stdout.write(f"Created {verbose}")
            else:
                modified = False
                if package.verbose != verbose:
                    self.stdout.write(f"Updating {package.verbose!r} -> {verbose!r}")
                    package.verbose = verbose
                    modified = True
                if package.name != name:
                    self.stdout.write(
                        f"Updating {verbose}: {package.name!r} -> {name!r}"
                    )
                    package.name = name
                    modified = True
                if package.price != price:
                    self.stdout.write(
                        f"Updating {verbose}: {package.price!r} -> {price!r}"
                    )
                    package.price = price
                    modified = True
                if modified:
                    package.save()
