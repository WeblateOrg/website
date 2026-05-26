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

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import DataError, IntegrityError
from django.urls import NoReverseMatch, reverse

from weblate_web.models import ExternalSyncState, SamlIdentity
from weblate_web.saml import (
    AmbiguousSamlIdentityError,
    SamlSyncContext,
    extract_profile,
    get_default_saml_provider,
    get_legacy_candidates,
    normalize_external_id,
    sync_saml_payload,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User

SYNC_KEY = "hosted-users"
USER_SYNC_SALT = "weblate.user-sync"
USER_SYNC_RESPONSE_SALT = "weblate.user-sync-response"
INVALID_SYNC_RESPONSE = "Invalid hosted user sync response"
DEFAULT_PROGRESS_EVERY = 10000


@dataclass(frozen=True)
class SyncResult:
    count: int
    linked: int
    skipped: bool


def raise_invalid_sync_response() -> NoReturn:
    raise RuntimeError(INVALID_SYNC_RESPONSE)


class Command(BaseCommand):
    help = "synchronizes hosted.weblate.org users with local SAML identities"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--since", help="override the stored hosted sync cursor")
        parser.add_argument(
            "--no-preload",
            action="store_true",
            help="disable batch lookup caches",
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help="only process hosted users without an existing local SAML identity",
        )
        parser.add_argument(
            "--progress-every",
            default=DEFAULT_PROGRESS_EVERY,
            type=int,
            help="emit progress after this many payloads; use 0 to disable",
        )

    def handle(self, *args, **options) -> None:
        if options["only_missing"] and options["no_preload"]:
            raise CommandError("--only-missing requires preloaded lookup caches")

        state, _created = ExternalSyncState.objects.get_or_create(key=SYNC_KEY)
        cursor = options["since"] or state.cursor
        payload = self.fetch_sync_payload(cursor)
        user_payloads = self.get_user_payloads(payload)
        context = self.get_sync_context(user_payloads, options["no_preload"])
        result = self.sync_user_payloads(
            user_payloads,
            context,
            only_missing=options["only_missing"],
            progress_every=options["progress_every"],
        )
        self.finish_sync(
            state,
            payload,
            result,
            only_missing=options["only_missing"],
        )

    def fetch_sync_payload(self, cursor: str) -> dict:
        request_payload = {"since": cursor}
        response = requests.post(
            settings.HOSTED_USER_SYNC_API,
            data={
                "payload": dumps(
                    request_payload,
                    key=settings.PAYMENT_SECRET,
                    salt=USER_SYNC_SALT,
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
                salt=USER_SYNC_RESPONSE_SALT,
            )
        except (BadSignature, SignatureExpired) as error:
            raise RuntimeError(INVALID_SYNC_RESPONSE) from error
        if not isinstance(payload, dict):
            raise_invalid_sync_response()
        return payload

    def get_user_payloads(self, payload: dict) -> list:
        user_payloads = payload.get("users", [])
        if not isinstance(user_payloads, list):
            raise_invalid_sync_response()
        return user_payloads

    def get_sync_context(
        self, user_payloads: list, no_preload: bool
    ) -> SamlSyncContext | None:
        total = len(user_payloads)
        self.stdout.write(f"Received {total} hosted users")
        if no_preload:
            return None
        self.stdout.write("Preloading hosted user sync lookups")
        return SamlSyncContext.preload(user_payloads)

    def sync_user_payloads(
        self,
        user_payloads: list,
        context: SamlSyncContext | None,
        *,
        only_missing: bool,
        progress_every: int,
    ) -> SyncResult:
        count = 0
        linked = 0
        processed = 0
        skipped = False
        total = len(user_payloads)
        for user_payload in user_payloads:
            processed += 1
            if not isinstance(user_payload, dict):
                skipped = True
                self.write_skip("payload is not a mapping", user_payload, context)
                self.write_progress(processed, total, progress_every)
                continue
            if only_missing and self.get_existing_identity(user_payload, context):
                linked += 1
                self.write_progress(processed, total, progress_every)
                continue
            try:
                user, _created = sync_saml_payload(user_payload, context)
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
                self.write_skip(error, user_payload, context)
                self.write_progress(processed, total, progress_every)
                continue
            if user is None:
                skipped = True
                self.write_skip("user not synchronized", user_payload, context)
                self.write_progress(processed, total, progress_every)
                continue
            count += 1
            self.write_progress(processed, total, progress_every)
        return SyncResult(count=count, linked=linked, skipped=skipped)

    def finish_sync(
        self,
        state: ExternalSyncState,
        payload: dict,
        result: SyncResult,
        *,
        only_missing: bool,
    ) -> None:
        if only_missing:
            self.stdout.write(f"Skipped {result.linked} already linked hosted users")
            self.stderr.write(
                "Not advancing hosted user sync cursor in --only-missing mode"
            )
        elif result.skipped:
            self.stderr.write("Not advancing hosted user sync cursor")
        elif cursor := payload.get("cursor"):
            state.cursor = cursor
            state.save(update_fields=("cursor", "updated"))

        self.stdout.write(f"Synchronized {result.count} hosted users")

    def write_progress(self, processed: int, total: int, progress_every: int) -> None:
        if progress_every > 0 and processed % progress_every == 0:
            self.stdout.write(f"Processed {processed}/{total} hosted users")

    def write_skip(
        self,
        error: object,
        user_payload: object,
        context: SamlSyncContext | None,
    ) -> None:
        self.stderr.write(f"Skipping hosted user payload: {error}")
        for line in self.describe_user_payload(user_payload, context):
            self.stderr.write(f"  {line}")

    def describe_user_payload(
        self, user_payload: object, context: SamlSyncContext | None
    ) -> list[str]:
        if not isinstance(user_payload, dict):
            return [f"payload={user_payload!r}"]
        provider = str(user_payload.get("provider", get_default_saml_provider()))
        external_id = normalize_external_id(user_payload.get("external_id"))
        profile = extract_profile(user_payload)
        username = profile.get("username", user_payload.get("username", ""))
        email = profile.get("email", "")
        lines = [
            (
                "hosted user: "
                f"external_id={external_id!r} username={username!r} email={email!r} "
                f"admin={self.get_hosted_user_admin_url(external_id)}"
            )
        ]
        candidates = sorted(
            get_legacy_candidates(profile, context), key=lambda user: user.pk
        )
        if candidates:
            lines.append("local candidates:")
            lines.extend(f"  {self.describe_local_user(user)}" for user in candidates)
            if external_id:
                lines.append(
                    "possible action: link the correct local candidate with "
                    f"SamlIdentity(provider={provider!r}, "
                    f"external_id={external_id!r}, user=<chosen user>)"
                )
        elif external_id:
            lines.append(
                "possible action: create a placeholder local user and link "
                f"SamlIdentity(provider={provider!r}, external_id={external_id!r})"
            )
        return lines

    def describe_local_user(self, user: User) -> str:
        identities = [
            (
                f"{identity.provider}:{identity.external_id} "
                f"{self.get_admin_url('admin:weblate_web_samlidentity_change', identity.pk)}"
            )
            for identity in SamlIdentity.objects.filter(user=user).order_by(
                "provider", "external_id"
            )
        ]
        return (
            f"id={user.pk} username={user.username!r} email={user.email!r} "
            f"last_login={user.last_login} "
            f"admin={self.get_admin_url('admin:auth_user_change', user.pk)} "
            f"saml_identities={identities}"
        )

    def get_existing_identity(
        self, user_payload: object, context: SamlSyncContext | None
    ) -> SamlIdentity | None:
        if not isinstance(user_payload, dict):
            return None
        external_id = normalize_external_id(user_payload.get("external_id"))
        if not external_id:
            return None
        provider = str(user_payload.get("provider", get_default_saml_provider()))
        if context is not None:
            return context.get_identity(provider, external_id)
        return (
            SamlIdentity.objects.filter(provider=provider, external_id=external_id)
            .select_related("user")
            .first()
        )

    def get_admin_url(self, viewname: str, object_id: object) -> str:
        try:
            path = reverse(viewname, args=[object_id])
        except NoReverseMatch:
            return ""
        return f"{settings.SITE_URL.rstrip('/')}{path}"

    def get_hosted_user_admin_url(self, external_id: str) -> str:
        if not external_id:
            return ""
        parsed = urlsplit(settings.HOSTED_USER_SYNC_API)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return (
            f"{parsed.scheme}://{parsed.netloc}/admin/auth/user/{external_id}/change/"
        )
