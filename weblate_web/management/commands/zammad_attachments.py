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

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from dateutil.parser import isoparse
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from weblate_web.crm.models import Interaction, ZammadSyncLog
from weblate_web.payments.models import Customer
from weblate_web.zammad import get_zammad_client

if TYPE_CHECKING:
    from zammad_py import ZammadAPI

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
        self,
        ticket_id: int,
        article_id: int,
        customer: Customer,
        known_attachments: set[int],
    ) -> bool:
        """Import attachments, returning whether the article can be marked complete."""
        complete = True
        self.stdout.write(f"Processing article {article_id}")
        article = self.client.ticket_article.find(article_id)
        for attachment in article["attachments"]:
            # Skip already imported attachments
            if attachment["id"] in known_attachments:
                continue
            filename = PurePosixPath(attachment["filename"].replace("\\", "/")).name
            # Check file extension
            if filename.lower().endswith(EXTENSIONS):
                self.stdout.write(
                    f"Downloading {attachment['filename']} {attachment['id']}"
                )
                # Download attachment
                content: bytes = self.client.ticket_article_attachment.download(
                    attachment["id"], article_id, ticket_id
                )
                timestamp = isoparse(article["created_at"])
                try:
                    with transaction.atomic():
                        interaction = customer.interaction_set.create(
                            timestamp=timestamp,
                            origin=Interaction.Origin.ZAMMAD_ATTACHMENT,
                            summary=attachment["filename"],
                            content=attachment["filename"],
                            remote_id=attachment["id"],
                            details={
                                "ticket_id": ticket_id,
                                "article_id": article_id,
                                "attachment_id": attachment["id"],
                                "filename": attachment["filename"],
                                "created_at": timestamp.isoformat(),
                            },
                        )
                        interaction.attachment.save(filename, ContentFile(content))
                except (SuspiciousFileOperation, OSError) as error:
                    self.stderr.write(
                        f"Could not store attachment {attachment['id']} "
                        f"(customer {customer.pk}, ticket {ticket_id}, "
                        f"article {article_id}): {error}"
                    )
                    if isinstance(error, OSError):
                        complete = False
                else:
                    known_attachments.add(attachment["id"])
        return complete

    def handle(self, *args, **options) -> None:
        self.client = get_zammad_client()
        customers = Customer.objects.exclude(zammad_id=0)

        processed_articles: set[int] = set(
            ZammadSyncLog.objects.values_list("article_id", flat=True)
        )

        for customer in customers:
            # Search for tickets with attachments from this customer
            results = self.client.ticket.search(f"organization.id:{customer.zammad_id}")
            # Partial articles can be retried in both normal and force mode.
            known_attachments = set(
                customer.interaction_set.filter(
                    origin=Interaction.Origin.ZAMMAD_ATTACHMENT
                )
                .exclude(remote_id=0)
                .exclude(attachment="")
                .values_list("remote_id", flat=True)
            )

            # Process tickets and articles
            while len(results):
                for ticket in results:
                    for article_id in ticket["article_ids"]:
                        if article_id in processed_articles and not options["force"]:
                            continue
                        if self.process_article(
                            ticket["id"], article_id, customer, known_attachments
                        ):
                            ZammadSyncLog.objects.get_or_create(
                                article_id=article_id, defaults={"customer": customer}
                            )
                            processed_articles.add(article_id)
                        elif article_id in processed_articles:
                            # A forced retry must not retain a stale success marker.
                            ZammadSyncLog.objects.filter(article_id=article_id).delete()
                            processed_articles.discard(article_id)

                results = results.next_page()
