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
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from djangosaml2.backends import Saml2Backend  # type: ignore[import-untyped]

from weblate_web.models import SamlIdentity

LOGGER = logging.getLogger(__name__)

PROFILE_FIELDS = {"username", "email", "last_name"}
ACTIVE_FIELDS = ("is_active", "active")
USERNAME_ALLOWED_RE = re.compile(r"[^\w.@+-]+")


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


def username_exists(username: str, user: User | None = None) -> bool:
    users = User.objects.filter(username=username)
    if user is not None and user.pk:
        users = users.exclude(pk=user.pk)
    return users.exists()


def make_unique_username(username: str, user: User | None = None) -> str:
    username = normalize_username(username)
    if not username_exists(username, user):
        return username
    max_length = get_username_max_length()
    counter = 1
    while True:
        suffix = f"-{counter}"
        candidate = f"{username[: max_length - len(suffix)]}{suffix}"
        if not username_exists(candidate, user):
            return candidate
        counter += 1


def get_legacy_candidates(profile: dict[str, Any]) -> list[User]:
    query = Q()
    if username := profile.get("username"):
        query |= Q(username=username)
    if email := profile.get("email"):
        query |= Q(email__iexact=email)
    if not query:
        return []
    return list(User.objects.filter(query).distinct())


def apply_profile(
    user: User, profile: dict[str, Any], *, cycle_unusable_password: bool = False
) -> None:
    changed_fields: set[str] = set()
    for field in PROFILE_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if field == "username":
            value = make_unique_username(str(value), user)
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


def create_user(profile: dict[str, Any], external_id: str) -> User:
    username = profile.get("username") or f"hosted-{external_id}"
    user = User(
        username=make_unique_username(str(username)),
        email=profile.get("email", ""),
        last_name=profile.get("last_name", ""),
    )
    for field in ACTIVE_FIELDS:
        if field in profile:
            user.is_active = parse_active(profile[field])
            break
    user.set_unusable_password()
    user.save()
    return user


def ensure_user_can_link_identity(user: User, provider: str, external_id: str) -> None:
    if (
        user.saml_identities.filter(provider=provider)
        .exclude(external_id=external_id)
        .exists()
    ):
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
) -> tuple[User | None, bool]:
    external_id = normalize_external_id(external_id)
    if not external_id:
        return None, False

    identity = (
        SamlIdentity.objects.select_related("user")
        .filter(provider=provider, external_id=external_id)
        .first()
    )
    created_user = False
    created_unlinked_user: User | None = None

    if identity:
        user = identity.user
    else:
        candidates = get_legacy_candidates(profile)
        if len(candidates) > 1:
            raise AmbiguousSamlIdentityError(
                f"Multiple local users match hosted user {external_id}"
            )
        if candidates:
            user = candidates[0]
            ensure_user_can_link_identity(user, provider, external_id)
        elif create_unknown_user:
            user = create_user(profile, external_id)
            created_user = True
            created_unlinked_user = user
        else:
            return None, False
        identity, identity_created = SamlIdentity.objects.get_or_create(
            provider=provider,
            external_id=external_id,
            defaults={"user": user},
        )
        if not identity_created:
            if (
                created_unlinked_user is not None
                and created_unlinked_user.pk != identity.user_id
            ):
                created_unlinked_user.delete()
            user = identity.user
            created_user = False

    apply_profile(user, profile, cycle_unusable_password=cycle_unusable_password)
    identity.last_seen = timezone.now()
    if raw_attrs is not None:
        identity.raw_attrs = raw_attrs
    identity.save(update_fields=("last_seen", "raw_attrs"))
    return user, created_user


def sync_saml_payload(payload: dict[str, Any]) -> tuple[User | None, bool]:
    external_id = payload.get("external_id")
    if external_id is None:
        return sync_legacy_payload(payload)
    return sync_saml_identity(
        provider=payload.get("provider", get_default_saml_provider()),
        external_id=normalize_external_id(external_id),
        profile=extract_profile(payload),
        raw_attrs=payload,
        cycle_unusable_password=bool(payload.get("changes")),
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
