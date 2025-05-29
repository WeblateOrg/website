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

from django.conf import settings
from django.core.management.base import BaseCommand
from zammad_py import ZammadAPI

from weblate_web.crm.models import ZammadSyncLog
from weblate_web.payments.models import Customer


class Command(BaseCommand):
    help = "synchronizes attachments from Zammad"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def process_article(self, article_id: int, customer: Customer) -> None:
        self.stdout.write(f"Processing article {article_id}")

    def handle(self, *args, **options) -> None:
        client = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        customers = Customer.objects.exclude(zammad_id=0)

        processed_articles: set[int] = set(
            ZammadSyncLog.objects.values_list("article_id", flat=True)
        )
        self.stdout.write(f"{processed_articles=}")

        for customer in customers:
            # Search for tickets with attachments from this customer
            results = client.ticket.search(
                f"article.attachment.title:* AND organization.id:{customer.zammad_id}"
            )
            self.stdout.write(f"{customer}")
            self.stdout.write(
                f"article.attachment.title:* AND organization.id:{customer.zammad_id}"
            )
            self.stdout.write(f"{list(results)}")
            while len(results):
                # Process tickets and articles
                for ticket in results:
                    for article_id in ticket["article_ids"]:
                        if article_id in processed_articles:
                            continue
                        processed_articles.add(article_id)
                        # customer.zammadsynclog_set.create(article_id=article_id) # noqa: ERA001

                results = results.next_page()
