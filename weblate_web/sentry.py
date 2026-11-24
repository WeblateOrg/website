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
import os
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import sentry_sdk
from django.conf import settings
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint, SamplingContext, SpanJSON

CRITICAL_PATHS = {
    "/api/hosted/",
    "/api/support/",
    "/api/support/activation/",
    "/api/user/",
    "/donate/process/",
    "/donate/new/",
    "/fosdem/donate/",
    "/js/vat/",
}
CRITICAL_PREFIXES = (
    "/payment/",
    "/invoice/",
    "/user/invoice/",
    "/subscription/",
    "/donate/edit/",
    "/donate/pay/",
    "/donate/disable/",
    "/oidc/",
)

INVITATION_PATH = re.compile(r"(/customer/owner-invitation/)(?!\{token\}/)[^/?#]+")
SIGNATURE_MISMATCH = re.compile(r'Signature "[^"\r\n]*" does not match')
URL_FIELDS = ("url", "url.full", "url.path", "http.target", "http.url")
SIGNING_MODULE = "django.core.signing"
SENSITIVE_FIELDS = (
    "accounting_reference",
    "address",
    "api_key",
    "apikey",
    "auth",
    "bank_account",
    "card_number",
    "company_name",
    "contact_name",
    "cookie",
    "contact_point",
    "country",
    "credentials",
    "csrf",
    "customer_note",
    "customer_reference",
    "email",
    "first_name",
    "full_name",
    "iban",
    "last_name",
    "password",
    "passwd",
    "phone",
    "postal_code",
    "postcode",
    "private_key",
    "privatekey",
    "recipients",
    "secret",
    "session",
    "token",
    "username",
    "vat",
)
PRIVATE_KEYS = frozenset(
    {
        "city",
        "code",
        "company",
        "cvv",
        "name",
        "pk",
        "pwd",
        "state",
        "tax",
        "user",
        "zip",
    }
)
IP_TERMS = ("forwarded", "-ip", "ip_address", "remote-", "remote_addr", "via")


def sample_trace(context: SamplingContext) -> float:
    environ = context.get("wsgi_environ", {})
    raw_path = environ.get("PATH_INFO")
    if not raw_path or not environ.get("REQUEST_METHOD"):
        return 0.0

    language, _, remainder = raw_path.removeprefix("/").partition("/")
    localized = any(language == code for code, _ in settings.LANGUAGES)
    if localized and not remainder and raw_path.endswith("/"):
        return 0.001

    if raw_path in {"/", "/feed/", "/site.webmanifest", "/sitemap.xml"} or (
        raw_path.startswith("/sitemap-") and raw_path.endswith(".xml")
    ):
        return 0.0

    for url in (settings.STATIC_URL, settings.MEDIA_URL):
        if (
            url
            and (prefix := urlsplit(url).path.rstrip("/"))
            and (raw_path == prefix or raw_path.startswith(f"{prefix}/"))
        ):
            return 0.0

    path = f"/{remainder}" if localized else raw_path
    return (
        0.1 if path in CRITICAL_PATHS or path.startswith(CRITICAL_PREFIXES) else 0.005
    )


def redact_url(value: str) -> str:
    return INVITATION_PATH.sub(r"\g<1>redacted", value)


def redact_fields(
    value: object, *, private_keys: frozenset[str] = PRIVATE_KEYS
) -> object:
    if isinstance(value, str):
        return SIGNATURE_MISMATCH.sub("Signature does not match", redact_url(value))
    if isinstance(value, dict):
        for field, item in value.items():
            key = field.lower().replace("-", "_") if isinstance(field, str) else ""
            if key in private_keys or any(term in key for term in SENSITIVE_FIELDS):
                value[field] = "[Filtered]"
            else:
                value[field] = redact_fields(item, private_keys=private_keys)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = redact_fields(item, private_keys=private_keys)
    return value


def redact_error(event: Event, _hint: Hint) -> Event:
    request = event.get("request", {})
    if isinstance(url := request.get("url"), str):
        request["url"] = redact_url(url)
    if "data" in request:
        request["data"] = redact_fields(
            request["data"], private_keys=PRIVATE_KEYS | {"payload"}
        )
    if isinstance(transaction := event.get("transaction"), str):
        event["transaction"] = redact_url(transaction)
    for collection in (event.get("exception"), event.get("threads")):
        if isinstance(collection, dict):
            for entry in collection.get("values", []):
                if isinstance(message := entry.get("value"), str):
                    entry["value"] = redact_fields(message)
                for frame in (entry.get("stacktrace") or {}).get("frames", []):
                    if frame.get("module") == SIGNING_MODULE:
                        # Django's signer keeps its raw input in stack-frame locals.
                        frame.pop("vars", None)
                    else:
                        redact_fields(frame.get("vars"))
    if isinstance(breadcrumbs := event.get("breadcrumbs"), dict):
        for breadcrumb in breadcrumbs.get("values", []):
            data = breadcrumb.get("data") or {}
            if breadcrumb.get("category") == "hetzner.api" and "response" in data:
                # The provider's error code is useful diagnostic context.
                data["response"] = redact_fields(
                    data["response"], private_keys=PRIVATE_KEYS - {"code"}
                )
            for field in URL_FIELDS:
                if isinstance(url := data.get(field), str):
                    data[field] = redact_url(url)
    return event


def redact_span(span: SpanJSON, _hint: Hint) -> SpanJSON:
    span["name"] = redact_url(span["name"])
    attributes = span.get("attributes", {})
    for field in URL_FIELDS:
        if isinstance(url := attributes.get(field), str):
            attributes[field] = redact_url(url)
    return span


def initialize_sentry() -> None:
    if not settings.SENTRY_DSN or sentry_sdk.is_initialized():
        return

    environment = os.environ.get("SENTRY_ENVIRONMENT", "production")
    tracing_enabled = environment == "production" and not settings.DEBUG
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=environment,
        integrations=[
            DjangoIntegration(cache_spans=False),
            RedisIntegration(),
            LoggingIntegration(
                level=None, event_level=logging.ERROR, breadcrumb_level=logging.INFO
            ),
        ],
        traces_sampler=sample_trace if tracing_enabled else None,
        profile_lifecycle="trace",
        profile_session_sample_rate=1.0 if tracing_enabled else 0.0,
        attach_stacktrace=True,
        data_collection={
            "user_info": False,
            "http_bodies": ["incoming_request"],
            "http_headers": {
                "request": {
                    "mode": "denylist",
                    "terms": [*IP_TERMS, *SENSITIVE_FIELDS, "-user", "referer"],
                },
            },
            "cookies": {"mode": "off"},
            "url_query_params": {
                "mode": "denylist",
                "terms": [*IP_TERMS, *SENSITIVE_FIELDS, *sorted(PRIVATE_KEYS)],
            },
            "stack_frame_variables": {
                "mode": "denylist",
                "terms": [
                    *IP_TERMS,
                    *SENSITIVE_FIELDS,
                    "args",
                    "body",
                    "customer",
                    "environ",
                    "form",
                    "headers",
                    "payload",
                    "request",
                    "response",
                    "user",
                ],
            },
            "frame_context_lines": 5,
            "database_query_data": False,
            "queues": False,
            "graphql": {"document": False, "variables": False},
            "gen_ai": {"inputs": False, "outputs": False},
            "mcp": {"inputs": False, "outputs": False},
        },
        before_send=redact_error,
        before_send_span=redact_span,
    )
