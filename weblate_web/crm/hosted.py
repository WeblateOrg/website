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

from typing import Any

import requests
from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.utils.translation import gettext

USER_ENSURE_SALT = "weblate.user-ensure"
USER_ENSURE_RESPONSE_SALT = "weblate.user-ensure-response"


class HostedUserEnsureError(RuntimeError):
    """Hosted user provisioning failed."""


def raise_invalid_hosted_user_response() -> None:
    raise HostedUserEnsureError(gettext("Invalid hosted user response"))


def ensure_hosted_user(email: str, full_name: str) -> tuple[dict[str, Any], bool]:
    if not settings.PAYMENT_SECRET:
        raise HostedUserEnsureError(gettext("Hosted user synchronization is disabled."))

    try:
        response = requests.post(
            settings.HOSTED_USER_CREATE_API,
            data={
                "payload": dumps(
                    {"email": email, "full_name": full_name},
                    key=settings.PAYMENT_SECRET,
                    salt=USER_ENSURE_SALT,
                )
            },
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise HostedUserEnsureError(str(error)) from error

    try:
        response_payload = response.json()
    except ValueError as error:
        raise HostedUserEnsureError(gettext("Invalid hosted user response")) from error
    if not isinstance(response_payload, dict):
        raise_invalid_hosted_user_response()
    signed_payload = response_payload.get("payload")
    if not isinstance(signed_payload, str):
        raise_invalid_hosted_user_response()

    try:
        payload = loads(
            signed_payload,
            key=settings.PAYMENT_SECRET,
            max_age=300,
            salt=USER_ENSURE_RESPONSE_SALT,
        )
    except (BadSignature, SignatureExpired) as error:
        raise HostedUserEnsureError(gettext("Invalid hosted user response")) from error
    if not isinstance(payload, dict):
        raise_invalid_hosted_user_response()
    user_payload = payload.get("user")
    if not isinstance(user_payload, dict):
        raise_invalid_hosted_user_response()
    return user_payload, bool(payload.get("created"))
