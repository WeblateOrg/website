# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib import admin
from django.db import DataError, IntegrityError, transaction
from django.views.decorators.cache import never_cache
from django.views.generic import RedirectView
from social_core.exceptions import AuthAssociationError
from social_django.models import UserSocialAuth

from weblate_web.hosted_users import (
    EMAIL_ALLOCATION_LOCK_KEY,
    PROFILE_FIELDS,
    get_default_hosted_provider,
    normalize_external_id,
    sync_hosted_identity,
)
from weblate_web.models import ExternalSyncState, HostedIdentity

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest, HttpResponseBase
    from social_core.backends.weblate import WeblateOpenIdConnect


@never_cache
def admin_login(request: HttpRequest) -> HttpResponseBase:
    """Use Hosted login unless local password login is explicitly selected."""
    if settings.LOGIN_URL == "/admin/login/":
        return admin.site.login(request)
    return RedirectView.as_view(url=settings.LOGIN_URL, query_string=True)(request)


@transaction.atomic
def sync_hosted_user(
    backend: WeblateOpenIdConnect,
    uid: str,
    details: dict[str, Any],
    user: User | None = None,
    id_key: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Bind OIDC to the persistent Hosted identity used by account sync."""
    if backend.name != "weblate" or id_key != "sub" or not uid:
        raise AuthAssociationError(backend, code="identifier_migration_conflict")
    if normalize_external_id(uid) != uid:
        raise AuthAssociationError(backend, code="identifier_migration_conflict")

    # Share the sync allocator lock, including when OIDC omits the email claim.
    ExternalSyncState.objects.select_for_update().get_or_create(
        key=EMAIL_ALLOCATION_LOCK_KEY
    )
    identity = HostedIdentity.objects.filter(
        provider=get_default_hosted_provider(), external_id=uid
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
        synced_user, created = sync_hosted_identity(
            provider=get_default_hosted_provider(),
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
