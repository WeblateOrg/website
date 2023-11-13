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

from imaplib import IMAP4_SSL

from django.conf import settings
from django.core.management.base import BaseCommand
from zammad_py import ZammadAPI
from zammad_py.api import Resource


class Tag(Resource):
    path_attribute = "tags"

    def add(self, obj, id, item):  # noqa: A002
        response = self._connection.session.post(
            self.url + "/add",
            data={
                "object": obj,
                "o_id": id,
                "item": item,
            },
        )
        return self._raise_or_return_json(response)


class Command(BaseCommand):
    help = "fetches spam tickets from Zammad"  # noqa: A003
    client = None

    def handle(self, *args, **options):
        zammad = ZammadAPI(
            url="https://care.weblate.org/api/v1/",
            http_token=settings.ZAMMAD_TOKEN,
        )
        tag_obj = Tag(zammad)
        imap = IMAP4_SSL(settings.IMAP_SERVER)
        imap.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        imap.select(settings.IMAP_SPAM_FOLDER)

        search = zammad.ticket.search("tags:spam AND -tags:reported-spam")
        for ticket in search:
            # Oldest article
            ticket_id = ticket["id"]
            article_id = sorted(ticket["article_ids"])[0]
            self.stdout.write(f"Processing {ticket_id}: {article_id}")

            # Get raw e-mail
            response = zammad.session.get(
                f"https://care.weblate.org/api/v1/ticket_article_plain/{article_id}"
            )
            data = zammad.ticket._raise_or_return_json(response)

            # Upload to IMAP
            imap.append(settings.IMAP_SPAM_FOLDER, None, None, data)

            # Add tag
            tag_obj.add("Ticket", ticket_id, "reported-spam")
