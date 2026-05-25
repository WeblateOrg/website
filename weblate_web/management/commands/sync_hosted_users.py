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

from typing import NoReturn

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import DataError, IntegrityError

from weblate_web.models import ExternalSyncState
from weblate_web.saml import AmbiguousSamlIdentityError, sync_saml_payload

SYNC_KEY = "hosted-users"
INVALID_SYNC_RESPONSE = "Invalid hosted user sync response"


def raise_invalid_sync_response() -> NoReturn:
    raise RuntimeError(INVALID_SYNC_RESPONSE)


class Command(BaseCommand):
    help = "synchronizes hosted.weblate.org users with local SAML identities"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--since", help="override the stored hosted sync cursor")

    def handle(self, *args, **options) -> None:
        state, _created = ExternalSyncState.objects.get_or_create(key=SYNC_KEY)
        cursor = options["since"] or state.cursor
        request_payload = {"since": cursor}
        response = requests.post(
            settings.HOSTED_USER_SYNC_API,
            data={
                "payload": dumps(
                    request_payload,
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user-sync",
                )
            },
            timeout=60,
        )
        response.raise_for_status()
        try:
            response_payload = response.json()
        except ValueError as error:
            raise RuntimeError(INVALID_SYNC_RESPONSE) from error
        if not isinstance(response_payload, dict):
            raise_invalid_sync_response()
        signed_payload = response_payload.get("payload")
        if not isinstance(signed_payload, str):
            raise_invalid_sync_response()
        try:
            payload = loads(
                signed_payload,
                key=settings.PAYMENT_SECRET,
                max_age=300,
                salt="weblate.user-sync-response",
            )
        except (BadSignature, SignatureExpired) as error:
            raise RuntimeError(INVALID_SYNC_RESPONSE) from error
        if not isinstance(payload, dict):
            raise_invalid_sync_response()

        count = 0
        skipped = False
        for user_payload in payload.get("users", []):
            try:
                user, _created = sync_saml_payload(user_payload)
            except (
                AmbiguousSamlIdentityError,
                AttributeError,
                DataError,
                IntegrityError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                skipped = True
                self.stderr.write(f"Skipping hosted user payload: {error}")
                continue
            if user is None:
                skipped = True
                self.stderr.write("Skipping hosted user payload: user not synchronized")
                continue
            count += 1

        if skipped:
            self.stderr.write("Not advancing hosted user sync cursor")
        elif cursor := payload.get("cursor"):
            state.cursor = cursor
            state.save(update_fields=("cursor", "updated"))

        self.stdout.write(f"Synchronized {count} hosted users")
