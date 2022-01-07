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

from django.conf import settings
from django.core.management.base import BaseCommand
from zammad_py import ZammadAPI

HOSTED_ACCOUNT = "Hosted Weblate account"


class Command(BaseCommand):
    help = "synchronizes customer data to Zammad"
    client = None

    def handle(self, *args, **options):
        self.client = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        self.handle_hosted_account()

    def handle_hosted_account(self):
        """Define link to search account on Hosted Weblate for all users."""
        self.client.user.per_page = 100
        users = self.client.user.search(
            {"query": f'!hosted_account:"{HOSTED_ACCOUNT}"', "limit": 100}
        )
        # We intentionally ignore pagination here as the sync is expected to run
        # regularly and fetch remaining ones in next run
        for user in users:
            self.client.user.update(user["id"], {"hosted_account": HOSTED_ACCOUNT})
            self.stdout.write(f"Updating {user['login']}")
