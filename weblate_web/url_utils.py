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

from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.translation import gettext_lazy

if TYPE_CHECKING:
    from django.utils.functional import Promise

INVALID_SITE_URL = gettext_lazy("Invalid server URL.")
SITE_URL_MAX_LENGTH = 200
HTTP_URL_VALIDATOR = URLValidator(schemes=["http", "https"])


def normalize_site_url_for_lock(url: str) -> str:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return url.rstrip("/")

    netloc = parts.netloc.lower()
    if hostname:
        host = hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port and (scheme, port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{port}"
        userinfo = ""
        if "@" in parts.netloc:
            userinfo = f"{parts.netloc.rsplit('@', 1)[0]}@"
        netloc = f"{userinfo}{host}"

    return urlunsplit(
        (
            scheme,
            netloc,
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
        )
    )


def has_dot_segment_path(path: str) -> bool:
    return any(segment in {".", ".."} for segment in unquote(path).split("/"))


def normalize_site_url(
    url: str,
    message: str | Promise = INVALID_SITE_URL,
    *,
    allow_empty: bool = False,
) -> str:
    if not url and allow_empty:
        return ""

    try:
        parts = urlsplit(url)
        port = parts.port
        HTTP_URL_VALIDATOR(url)
    except (TypeError, ValueError, ValidationError) as error:
        raise ValidationError(str(message)) from error

    invalid_url = (
        len(url) > SITE_URL_MAX_LENGTH
        or parts.scheme.lower() not in {"http", "https"}
        or not parts.netloc
        or not parts.hostname
    )
    has_delimiter = "?" in url or "#" in url
    has_extra_parts = any((parts.username, parts.password, parts.query, parts.fragment))
    if (
        invalid_url
        or has_delimiter
        or has_extra_parts
        or has_dot_segment_path(parts.path)
        or port == 0
    ):
        raise ValidationError(str(message))

    return normalize_site_url_for_lock(url)
