# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import DataError, IntegrityError, transaction
from social_core.exceptions import AuthAssociationError
from social_django.models import UserSocialAuth

from weblate_web.models import ExternalSyncState, SamlIdentity
from weblate_web.saml import (
    EMAIL_ALLOCATION_LOCK_KEY,
    PROFILE_FIELDS,
    get_default_saml_provider,
    normalize_external_id,
    sync_saml_identity,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from social_core.backends.weblate import WeblateOpenIdConnect


@transaction.atomic
def sync_hosted_user(
    backend: WeblateOpenIdConnect,
    uid: str,
    details: dict[str, Any],
    user: User | None = None,
    id_key: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Bind OIDC to the persistent hosted identity used by SAML and user sync."""
    # Both deployments explicitly use hosted user.pk as their SAML NameID.
    # Only the configured Weblate issuer may bridge to this identity namespace.
    if backend.name != "weblate" or id_key != "sub" or not uid:
        raise AuthAssociationError(backend, code="identifier_migration_conflict")
    if normalize_external_id(uid) != uid:
        raise AuthAssociationError(backend, code="identifier_migration_conflict")

    # Share the sync allocator lock, including when OIDC omits the email claim.
    ExternalSyncState.objects.select_for_update().get_or_create(
        key=EMAIL_ALLOCATION_LOCK_KEY
    )
    identity = SamlIdentity.objects.filter(
        provider=get_default_saml_provider(), external_id=uid
    ).first()
    social = UserSocialAuth.get_social_auth(backend.name, uid)
    if social is not None and (
        identity is None
        or social.user_id != identity.user_id
        or social.id_key != id_key
    ):
        raise AuthAssociationError(backend, code="identifier_migration_conflict")
    if user is not None and (identity is None or identity.user_id != user.pk):
        raise AuthAssociationError(backend, code="identity_in_use")

    # Do not let social-auth's generic creation/update steps bypass our account
    # sync rules or associate a logged-in local account by profile information.
    profile = {
        key: value
        for key, value in details.items()
        if key in PROFILE_FIELDS and value is not None
    }
    try:
        synced_user, created = sync_saml_identity(
            provider=get_default_saml_provider(),
            external_id=uid,
            profile=profile,
            raw_attrs={"sub": uid, **profile},
        )
        if synced_user is None:
            raise AuthAssociationError(backend, code="identifier_migration_conflict")
        social, association_created = UserSocialAuth.objects.get_or_create(
            provider=backend.name,
            uid=uid,
            defaults={"user": synced_user, "id_key": id_key},
        )
    except ValueError as error:
        raise AuthAssociationError(backend, code="email_in_use") from error
    except (DataError, IntegrityError) as error:
        raise AuthAssociationError(
            backend, code="identifier_migration_conflict"
        ) from error
    if social.user_id != synced_user.pk or social.id_key != id_key:
        raise AuthAssociationError(backend, code="identifier_migration_conflict")
    return {
        "user": synced_user,
        "social": social,
        "is_new": created,
        "new_association": association_created,
    }
