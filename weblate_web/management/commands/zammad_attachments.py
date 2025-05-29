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

from weblate_web.crm.models import Interaction, ZammadSyncLog
from weblate_web.payments.models import Customer

EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".doc", ".odf", ".ods", ".xls", ".xlsx")


class Command(BaseCommand):
    help = "synchronizes attachments from Zammad"
    client: ZammadAPI

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--force",
            default=False,
            action="store_true",
            help="Force processing already processed articles",
        )

    def process_article(
        self, article_id: int, customer: Customer, known_attachments: set[int]
    ) -> None:
        self.stdout.write(f"Processing article {article_id}")
        article = self.client.ticket_article.find(article_id)
        for attachment in article["attachments"]:
            if attachment["filename"].lower().endswith(EXTENSIONS):
                self.stdout.write(
                    f"Downloading {attachment['filename']} {attachment['id']}"
                )
                # ticket_article_attachment
        customer.zammadsynclog_set.create(article_id=article_id)

    def handle(self, *args, **options) -> None:
        self.client = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        customers = Customer.objects.exclude(zammad_id=0)

        processed_articles: set[int] = set(
            ZammadSyncLog.objects.values_list("article_id", flat=True)
        )

        for customer in customers:
            # Search for tickets with attachments from this customer
            results = self.client.ticket.search(
                f"article.attachment.title:* AND organization.id:{customer.zammad_id}"
            )
            # List of known attachments is only needed in force mode, otherwise we do not
            # visit processed articles otherwise
            known_attachments: set[int] = set()
            if options["force"]:
                known_attachments = set(
                    customer.interaction_set.filter(
                        origin=Interaction.Origin.ZAMMAD_ATTACHMENT
                    )
                    .exclude(remote_id=0)
                    .values_list("remote_id", flat=True)
                )

            # Process tickets and articles
            while len(results):
                for ticket in results:
                    for article_id in ticket["article_ids"]:
                        if article_id in processed_articles and not options["force"]:
                            continue
                        self.process_article(article_id, customer, known_attachments)

                results = results.next_page()
