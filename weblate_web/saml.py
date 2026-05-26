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

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone
from djangosaml2.backends import Saml2Backend  # type: ignore[import-untyped]

from weblate_web.models import SamlIdentity

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

LOGGER = logging.getLogger(__name__)

PROFILE_FIELDS = {"username", "email", "last_name"}
ACTIVE_FIELDS = ("is_active", "active")
USERNAME_ALLOWED_RE = re.compile(r"[^\w.@+-]+")
PRELOAD_CHUNK_SIZE = 1000


class AmbiguousSamlIdentityError(ValueError):
    """Raised when a legacy user can not be selected safely."""


def get_default_saml_provider() -> str:
    return settings.HOSTED_SAML_PROVIDER


def normalize_external_id(external_id: object) -> str:
    if external_id is None:
        return ""
    return str(external_id).strip()


def get_username_max_length() -> int:
    max_length = User._meta.get_field("username").max_length  # pylint: disable=protected-access
    return int(max_length or 150)


def iter_chunks(
    values: Iterable[Any], chunk_size: int = PRELOAD_CHUNK_SIZE
) -> Iterator[list[Any]]:
    chunk = []
    for value in values:
        chunk.append(value)
        if len(chunk) == chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def normalize_username(username: object) -> str:
    result = USERNAME_ALLOWED_RE.sub("-", str(username).strip())
    result = result or "hosted-user"
    return result[: get_username_max_length()]


def parse_active(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in {"0", "false", "no", "off"}
    return bool(value)


def extract_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for container in ("profile", "create", "changes"):
        values = payload.get(container, {})
        if isinstance(values, dict):
            profile.update(values)
    for field in PROFILE_FIELDS | set(ACTIVE_FIELDS):
        if field in payload:
            profile[field] = payload[field]
    return profile


class SamlSyncContext:
    """Batch lookup cache for hosted user synchronization."""

    def __init__(self) -> None:
        self.identities: dict[tuple[str, str], SamlIdentity] = {}
        self.provider_identities_by_user: dict[tuple[str, int], list[SamlIdentity]] = {}
        self.users_by_username: dict[str, list[User]] = defaultdict(list)
        self.users_by_email: dict[str, list[User]] = defaultdict(list)
        self.username_owner: dict[str, int] = {}

    @classmethod
    def preload(cls, payloads: Iterable[object]) -> SamlSyncContext:
        context = cls()
        providers_external_ids: dict[str, set[str]] = defaultdict(set)
        providers = set()
        usernames = set()
        emails = set()

        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            provider = payload.get("provider", get_default_saml_provider())
            provider = str(provider)
            providers.add(provider)
            if external_id := normalize_external_id(payload.get("external_id")):
                providers_external_ids[provider].add(external_id)
            profile = extract_profile(payload)
            if username := profile.get("username"):
                usernames.add(str(username).casefold())
            if email := profile.get("email"):
                emails.add(str(email).casefold())

        context.preload_username_owners()
        context.preload_identities(providers_external_ids)
        users = context.preload_legacy_users(usernames, emails)
        context.preload_user_identities(providers, users)
        return context

    def preload_username_owners(self) -> None:
        for user_id, username in User.objects.values_list("id", "username").iterator():
            self.username_owner[username.casefold()] = user_id

    def preload_identities(self, providers_external_ids: dict[str, set[str]]) -> None:
        for provider, external_ids in providers_external_ids.items():
            for chunk in iter_chunks(external_ids):
                for identity in (
                    SamlIdentity.objects.select_related("user")
                    .filter(provider=provider, external_id__in=chunk)
                    .iterator()
                ):
                    self.add_identity(identity)

    def preload_legacy_users(
        self, usernames: set[str], emails: set[str]
    ) -> dict[int, User]:
        users: dict[int, User] = {}
        for chunk in iter_chunks(usernames):
            for username_user in (
                User.objects.annotate(username_lower=Lower("username"))
                .filter(username_lower__in=chunk)
                .iterator()
            ):
                users[username_user.pk] = username_user
                self.users_by_username[username_user.username].append(username_user)
        for chunk in iter_chunks(emails):
            for email_user in (
                User.objects.annotate(email_lower=Lower("email"))
                .filter(email_lower__in=chunk)
                .iterator()
            ):
                users[email_user.pk] = email_user
                self.users_by_email[email_user.email.casefold()].append(email_user)
        return users

    def preload_user_identities(
        self, providers: set[str], users: dict[int, User]
    ) -> None:
        if not providers or not users:
            return
        for user_ids in iter_chunks(users):
            for identity in (
                SamlIdentity.objects.select_related("user")
                .filter(provider__in=providers, user_id__in=user_ids)
                .iterator()
            ):
                self.add_identity(identity)

    def add_identity(self, identity: SamlIdentity) -> None:
        self.identities[identity.provider, identity.external_id] = identity
        key = (identity.provider, identity.user_id)
        identities = self.provider_identities_by_user.setdefault(key, [])
        if all(item.pk != identity.pk for item in identities):
            identities.append(identity)

    def get_identity(self, provider: str, external_id: str) -> SamlIdentity | None:
        return self.identities.get((provider, external_id))

    def get_user_provider_identities(
        self, user: User, provider: str
    ) -> list[SamlIdentity]:
        return self.provider_identities_by_user.get((provider, user.pk), [])

    def get_legacy_candidates(self, profile: dict[str, Any]) -> list[User]:
        users = {}
        if username := profile.get("username"):
            for user in self.users_by_username.get(str(username), []):
                users[user.pk] = user
        if email := profile.get("email"):
            for user in self.users_by_email.get(str(email).casefold(), []):
                users[user.pk] = user
        return list(users.values())

    def username_exists(self, username: str, user: User | None = None) -> bool:
        user_id = self.username_owner.get(username.casefold())
        return user_id is not None and (user is None or user_id != user.pk)

    def set_username_owner(self, user: User, old_username: str | None = None) -> None:
        if old_username:
            old_key = old_username.casefold()
            if self.username_owner.get(old_key) == user.pk:
                del self.username_owner[old_key]
        self.username_owner[user.username.casefold()] = user.pk

    def set_user_email(self, user: User, old_email: str | None = None) -> None:
        if old_email:
            self.users_by_email[old_email.casefold()] = [
                item
                for item in self.users_by_email[old_email.casefold()]
                if item.pk != user.pk
            ]
        if user.email and all(
            item.pk != user.pk for item in self.users_by_email[user.email.casefold()]
        ):
            self.users_by_email[user.email.casefold()].append(user)

    def add_legacy_user(self, user: User) -> None:
        self.users_by_username[user.username].append(user)
        self.set_user_email(user)
        self.set_username_owner(user)


def profile_from_saml_attributes(
    attributes: dict[str, list[Any]], attribute_mapping: dict[str, tuple[str, ...]]
) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for saml_attr, django_attrs in attribute_mapping.items():
        values = attributes.get(saml_attr)
        if not values:
            continue
        for django_attr in django_attrs:
            if django_attr in PROFILE_FIELDS:
                profile[django_attr] = values[0]
    return profile


def username_exists(
    username: str, user: User | None = None, context: SamlSyncContext | None = None
) -> bool:
    if context is not None:
        return context.username_exists(username, user)
    users = User.objects.filter(username__iexact=username)
    if user is not None and user.pk:
        users = users.exclude(pk=user.pk)
    return users.exists()


def make_unique_username(
    username: str, user: User | None = None, context: SamlSyncContext | None = None
) -> str:
    username = normalize_username(username)
    if not username_exists(username, user, context):
        return username
    max_length = get_username_max_length()
    counter = 1
    while True:
        suffix = f"-{counter}"
        candidate = f"{username[: max_length - len(suffix)]}{suffix}"
        if not username_exists(candidate, user, context):
            return candidate
        counter += 1


def get_legacy_candidates(
    profile: dict[str, Any], context: SamlSyncContext | None = None
) -> list[User]:
    if context is not None:
        return context.get_legacy_candidates(profile)
    users = {}
    if username := profile.get("username"):
        for user in User.objects.filter(username=username):
            if user.username == username:
                users[user.pk] = user
    if email := profile.get("email"):
        for user in User.objects.filter(email__iexact=email):
            users[user.pk] = user
    return list(users.values())


def apply_profile(
    user: User,
    profile: dict[str, Any],
    *,
    cycle_unusable_password: bool = False,
    context: SamlSyncContext | None = None,
) -> None:
    changed_fields: set[str] = set()
    old_username = user.username
    old_email = user.email
    for field in PROFILE_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if field == "username":
            if value is None:
                continue
            value = make_unique_username(str(value), user, context)
        elif value is None:
            value = ""
        if getattr(user, field) != value:
            setattr(user, field, value)
            changed_fields.add(field)
    for field in ACTIVE_FIELDS:
        if field in profile:
            is_active = parse_active(profile[field])
            if user.is_active != is_active:
                user.is_active = is_active
                changed_fields.add("is_active")
            break
    if cycle_unusable_password and not user.has_usable_password():
        user.set_unusable_password()
        changed_fields.add("password")
    if changed_fields:
        user.save(update_fields=sorted(changed_fields))
        if context is not None and "username" in changed_fields:
            context.set_username_owner(user, old_username=old_username)
        if context is not None and "email" in changed_fields:
            context.set_user_email(user, old_email=old_email)


def create_user(
    profile: dict[str, Any], external_id: str, context: SamlSyncContext | None = None
) -> User:
    username = profile.get("username") or f"hosted-{external_id}"
    user = User(
        username=make_unique_username(str(username), context=context),
        email=profile.get("email") or "",
        last_name=profile.get("last_name") or "",
    )
    for field in ACTIVE_FIELDS:
        if field in profile:
            user.is_active = parse_active(profile[field])
            break
    user.set_unusable_password()
    user.save()
    if context is not None:
        context.add_legacy_user(user)
    return user


def ensure_user_can_link_identity(
    user: User,
    provider: str,
    external_id: str,
    context: SamlSyncContext | None = None,
) -> None:
    if context is None:
        already_linked = (
            user.saml_identities.filter(provider=provider)
            .exclude(external_id=external_id)
            .exists()
        )
    else:
        already_linked = any(
            identity.external_id != external_id
            for identity in context.get_user_provider_identities(user, provider)
        )
    if already_linked:
        raise AmbiguousSamlIdentityError(
            f"Local user {user.pk} is already linked to another hosted identity"
        )


@transaction.atomic
def sync_saml_identity(  # noqa: PLR0913
    *,
    provider: str,
    external_id: str,
    profile: dict[str, Any],
    raw_attrs: dict[str, Any] | None = None,
    create_unknown_user: bool = True,
    cycle_unusable_password: bool = False,
    context: SamlSyncContext | None = None,
) -> tuple[User | None, bool]:
    external_id = normalize_external_id(external_id)
    if not external_id:
        return None, False

    if context is None:
        identity = (
            SamlIdentity.objects.select_related("user")
            .filter(provider=provider, external_id=external_id)
            .first()
        )
    else:
        identity = context.get_identity(provider, external_id)
    created_user = False
    created_unlinked_user: User | None = None
    identity_created = False

    if identity:
        user = identity.user
    else:
        candidates = get_legacy_candidates(profile, context)
        if len(candidates) > 1:
            raise AmbiguousSamlIdentityError(
                f"Multiple local users match hosted user {external_id}"
            )
        if candidates:
            user = candidates[0]
            ensure_user_can_link_identity(user, provider, external_id, context)
        elif create_unknown_user:
            user = create_user(profile, external_id, context)
            created_user = True
            created_unlinked_user = user
        else:
            return None, False
        now = timezone.now()
        defaults: dict[str, Any] = {"user": user, "last_seen": now}
        if raw_attrs is not None:
            defaults["raw_attrs"] = raw_attrs
        identity, identity_created = SamlIdentity.objects.get_or_create(
            provider=provider,
            external_id=external_id,
            defaults=defaults,
        )
        if not identity_created:
            if (
                created_unlinked_user is not None
                and created_unlinked_user.pk != identity.user_id
            ):
                created_unlinked_user.delete()
            user = identity.user
            created_user = False
        elif context is not None:
            context.add_identity(identity)

    apply_profile(
        user,
        profile,
        cycle_unusable_password=cycle_unusable_password,
        context=context,
    )
    if not identity_created:
        identity.last_seen = timezone.now()
        if raw_attrs is not None:
            identity.raw_attrs = raw_attrs
        identity.save(update_fields=("last_seen", "raw_attrs"))
    return user, created_user


def sync_saml_payload(
    payload: dict[str, Any], context: SamlSyncContext | None = None
) -> tuple[User | None, bool]:
    external_id = payload.get("external_id")
    if external_id is None:
        return sync_legacy_payload(payload)
    return sync_saml_identity(
        provider=str(payload.get("provider", get_default_saml_provider())),
        external_id=normalize_external_id(external_id),
        profile=extract_profile(payload),
        raw_attrs=payload,
        cycle_unusable_password=bool(payload.get("changes")),
        context=context,
    )


def sync_legacy_payload(payload: dict[str, Any]) -> tuple[User | None, bool]:
    username = payload["username"]
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        user = User.objects.create(**payload["create"])
        return user, True

    changes = payload.get("changes", {})
    apply_profile(user, changes, cycle_unusable_password=bool(changes))
    return user, False


class HostedSaml2Backend(Saml2Backend):
    def _extract_user_identifier_params(
        self, session_info: dict, attributes: dict, attribute_mapping: dict
    ) -> tuple[str, str | None]:
        name_id = session_info.get("name_id")
        if name_id is None:
            LOGGER.error("The nameid is not available.")
            return "external_id", None
        external_id = normalize_external_id(name_id.text)
        if not external_id:
            LOGGER.error("The nameid text is not available.")
            return "external_id", None
        return "external_id", external_id

    def get_or_create_user(  # noqa: PLR0913,PLR0917
        self,
        user_lookup_key: str,
        user_lookup_value: Any,
        create_unknown_user: bool,
        idp_entityid: str,
        attributes: dict,
        attribute_mapping: dict,
        request,
    ):
        try:
            return sync_saml_identity(
                provider=idp_entityid,
                external_id=user_lookup_value,
                profile=profile_from_saml_attributes(attributes, attribute_mapping),
                raw_attrs=attributes,
                create_unknown_user=create_unknown_user,
            )
        except AmbiguousSamlIdentityError:
            LOGGER.exception("Could not link SAML identity %s", user_lookup_value)
            return None, False

    def _update_user(
        self, user, attributes: dict, attribute_mapping: dict, force_save: bool = False
    ):
        return user
