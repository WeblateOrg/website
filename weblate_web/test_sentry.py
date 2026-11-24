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
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from importlib import import_module
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import patch

import django
import pytest
import sentry_sdk
from django.conf import settings
from django.core.wsgi import get_wsgi_application
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import clear_url_caches, path
from django.views.decorators.csrf import csrf_exempt
from requests import HTTPError, Response
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration

from .hetzner import handle_error_response
from .middleware import SENTRY_URL
from .sentry import redact_error, redact_fields, sample_trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest
    from sentry_sdk.envelope import Envelope

INVITATION_URL = "/en/customer/owner-invitation/synthetic-invitation-value/"
INVITEE_EMAIL = "owner@example.invalid"
PAYMENT_URL = "/en/payment/bbcdf684-f8b0-49b2-b387-6e918a3dd475/edit/"
INVOICE_URL = "/en/invoice/6db168fa-c059-4515-ad85-aced344db5a0/pay/"
SAMPLED_HEADER = "0123456789abcdef0123456789abcdef-0123456789abcdef-1"
FORM_VALUE = "synthetic-form-value"
LOCAL_VALUE = "synthetic-local-value"
SIGNED_PAYLOAD = "synthetic-signed-user-payload:1:synthetic-invalid-signature"


@pytest.mark.parametrize(
    ("request_path", "rate"),
    [
        ("/", 0.0),
        ("/sitemap.xml", 0.0),
        ("/sitemap-news.xml", 0.0),
        ("/feed/", 0.0),
        ("/site.webmanifest", 0.0),
        ("/static/example.css", 0.0),
        ("/media/example.png", 0.0),
        ("/en/", 0.001),
        ("/pt-br/", 0.001),
        ("/en/hosting/", 0.005),
        ("/xx/payment/", 0.005),
        ("/en/donate/", 0.005),
        ("/api/other/", 0.005),
        ("/api/hosted/", 0.1),
        ("/api/support/", 0.1),
        ("/api/support/activation/", 0.1),
        ("/api/user/", 0.1),
        ("/en/donate/process/", 0.1),
        ("/en/fosdem/donate/", 0.1),
        ("/js/vat/", 0.1),
        ("/oidc/complete/weblate/", 0.1),
        ("/en/subscription/new/", 0.1),
        ("/en/invoice/example/pay/", 0.1),
        (PAYMENT_URL, 0.1),
    ],
)
def test_trace_sampling(request_path: str, rate: float) -> None:
    assert sample_trace(
        {
            "wsgi_environ": {"PATH_INFO": request_path, "REQUEST_METHOD": "GET"},
            "parent_sampled": True,
        }
    ) == pytest.approx(rate)


def test_background_job_sampling() -> None:
    assert sample_trace({}) == pytest.approx(0.0)


def test_redact_fields_preserves_diagnostics_in_nested_data() -> None:
    data = {
        "results": [
            {
                "status_code": 403,
                "customer-email": "test@example.invalid",
                "recipients": ["owner@example.invalid"],
                "recipient_message": "5000000001",
            },
        ],
    }

    redact_fields(data)

    assert data == {
        "results": [
            {
                "status_code": 403,
                "customer-email": "[Filtered]",
                "recipients": "[Filtered]",
                "recipient_message": "5000000001",
            },
        ],
    }


@pytest.mark.parametrize(
    ("body", "message", "expected"),
    [
        (
            (
                b'{"error":{"code":"invalid_input","message":"Invalid request",'
                b'"details":{"api_token":"synthetic-value"}}}'
            ),
            "Invalid request",
            {
                "response": {
                    "error": {
                        "code": "invalid_input",
                        "message": "Invalid request",
                        "details": {"api_token": "[Filtered]"},
                    },
                },
            },
        ),
        (b"Gateway error", "400 Client Error", {"text": "Gateway error"}),
    ],
)
def test_hetzner_error_breadcrumb(
    body: bytes, message: str, expected: dict[str, object]
) -> None:
    response = Response()
    response.status_code = 400
    response.encoding = "utf-8"
    response.raw = BytesIO(body)

    with (
        patch("sentry_sdk.add_breadcrumb") as capture,
        pytest.raises(HTTPError, match=message),
    ):
        handle_error_response(response)

    capture.assert_called_once()
    breadcrumb = dict(capture.call_args.kwargs)
    redact_error({"breadcrumbs": {"values": [breadcrumb]}}, {})
    assert breadcrumb["category"] == "hetzner.api"
    assert breadcrumb["data"] == {"status_code": 400, **expected}


@pytest.mark.parametrize("environment", ["disabled", "staging", "production"])
def test_sentry_startup(environment: str) -> None:
    # Isolate Django startup and the SDK's process-wide integration patches.
    result = subprocess.run(
        [sys.executable, "-m", "weblate_web.test_sentry", environment],
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "weblate_web.settings_test"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@csrf_exempt
def failing_view(request: HttpRequest) -> HttpResponse:
    diagnostic = LOCAL_VALUE
    assert request.POST["value"] == FORM_VALUE
    msg = f"Sentry test request failed ({len(diagnostic)})"
    raise RuntimeError(msg)


def logging_view(_request: HttpRequest, **_kwargs: object) -> HttpResponse:
    sentry_sdk.add_breadcrumb(data={"url.full": f"https://weblate.org{INVITATION_URL}"})
    try:
        int("not a number")
    except ValueError:
        logging.getLogger(__name__).exception("Reporting the handled error")
    return HttpResponse("ok")


def assert_request_envelopes(envelopes: list[Envelope]) -> None:
    urls = import_module("weblate_web.urls")
    application = get_wsgi_application()
    requests = RequestFactory(headers={"host": "weblate.org"})
    client = sentry_sdk.get_client()
    with (
        patch.object(
            urls,
            "urlpatterns",
            [
                path("", failing_view),
                path("en/customer/owner-invitation/<str:token>/", logging_view),
                path("en/payment/<uuid:pk>/edit/", logging_view),
                path("en/invoice/<uuid:pk>/pay/", logging_view),
                *urls.urlpatterns,
            ],
        ),
        patch("weblate_web.context_processors.get_activity", return_value=[]),
    ):
        clear_url_caches()
        request = requests.post(
            "/?page=synthetic-query-value&code=synthetic-oauth-value",
            {
                "value": FORM_VALUE,
                "password": "synthetic-password-value",
                "email": "synthetic-email-value@example.invalid",
            },
            secure=True,
            HTTP_COOKIE="test=synthetic-cookie-value",
            HTTP_AUTHORIZATION="synthetic-auth-value",
            HTTP_USER_AGENT="synthetic-agent-value",
            HTTP_X_FORWARDED_FOR="192.0.2.25",
            HTTP_SENTRY_TRACE=SAMPLED_HEADER,
        )
        headers: dict[str, str] = {}
        statuses: list[str] = []

        def start_response(
            status: str, response_headers: list[tuple[str, str]], *_args: object
        ) -> Callable[[bytes], None]:
            statuses.append(status)
            headers.update(response_headers)
            return lambda _: None

        response = application(request.environ, start_response)
        body = b"".join(response)
        response.close()
        sentry_sdk.flush()
        assert statuses[0].startswith("500")
        assert not any(
            item.type in {"span", "profile_chunk"}
            for env in envelopes
            for item in env.items
        )
        errors = [event for env in envelopes if (event := env.get_event()) is not None]
        assert len(errors) == 1
        request_data = errors[0]["request"]["data"]
        assert isinstance(request_data, dict)
        assert request_data["value"] == FORM_VALUE
        assert b"Sentry.showReportDialog" in body
        assert errors[0]["event_id"].encode() in body
        assert SENTRY_URL in headers["Content-Security-Policy"]

        with patch.dict(client.options, {"traces_sampler": lambda _: 1.0}):
            for url in (INVITATION_URL, PAYMENT_URL, INVOICE_URL):
                response = application(
                    requests.get(
                        url, secure=True, HTTP_USER_AGENT="synthetic-agent-value"
                    ).environ,
                    start_response,
                )
                assert b"".join(response) == b"ok"
                response.close()
        for url in ("/api/user/", "/api/hosted/"):
            request = requests.post(
                url, {"payload": SIGNED_PAYLOAD, "value": FORM_VALUE}, secure=True
            )
            response = application(request.environ, start_response)
            list(response)
            response.close()
            assert statuses[-1].startswith("400")
        sentry_sdk.flush()

    item_types = [item.type for env in envelopes for item in env.items]
    assert item_types.count("event") == 6
    assert "span" in item_types
    assert "log" not in item_types
    payload = b"".join(env.serialize() for env in envelopes)
    assert b'"sentry.profiler_id"' in payload
    assert b"/customer/owner-invitation/redacted/" in payload
    for item_type in ("event", "span"):
        items = b"".join(
            item.get_bytes()
            for env in envelopes
            for item in env.items
            if item.type == item_type
        )
        assert PAYMENT_URL.encode() in items
        assert INVOICE_URL.encode() in items
    for marker in (
        FORM_VALUE.encode(),
        LOCAL_VALUE.encode(),
        b"synthetic-query-value",
        b"synthetic-agent-value",
    ):
        assert marker in payload, marker
    for marker in (
        b"synthetic-oauth-value",
        b"synthetic-password-value",
        b"synthetic-email-value",
        b"synthetic-cookie-value",
        b"synthetic-auth-value",
        b"192.0.2.25",
        b"synthetic-invitation-value",
        b"synthetic-signed-user-payload",
        b"synthetic-invalid-signature",
    ):
        index = payload.find(marker)
        assert index == -1, payload[max(0, index - 50) : index + 60]


def assert_invitation_notification_error(envelopes: list[Envelope]) -> None:
    notifications = import_module("weblate_web.payments.utils")
    first_envelope = len(envelopes)
    with (
        patch.object(notifications.Path, "read_bytes", return_value=b"test-logo"),
        patch.object(
            notifications,
            "render_to_string",
            side_effect=["Invitation", OSError("Could not render the invitation")],
        ),
    ):
        try:
            notifications.send_notification(
                "customer_owner_invitation",
                [INVITEE_EMAIL],
                invitation_url=f"https://weblate.org{INVITATION_URL}",
            )
        except OSError:
            logging.getLogger("weblate_web.payments.invitations").exception(
                "Could not send customer owner invitation"
            )

    sentry_sdk.flush()
    captured = envelopes[first_envelope:]
    assert any(envelope.get_event() is not None for envelope in captured)
    payload = b"".join(envelope.serialize() for envelope in captured)
    assert b"/customer/owner-invitation/redacted/" in payload
    assert INVITATION_URL.split("/")[-2].encode() not in payload
    assert INVITEE_EMAIL.encode() not in payload


def check_startup(environment: str) -> None:
    settings.DEBUG = False
    settings.COMPRESS_ENABLED = False
    os.environ["SENTRY_ENVIRONMENT"] = environment
    if environment != "disabled":
        settings.SENTRY_DSN = "https://public@example.invalid/1"

    envelopes: list[Envelope] = []
    with patch.object(
        sentry_sdk.HttpTransport,
        "capture_envelope",
        autospec=True,
        side_effect=lambda _transport, envelope: envelopes.append(envelope),
    ):
        django.setup()
        if environment == "disabled":
            assert not sentry_sdk.is_initialized()
            return

        client = sentry_sdk.get_client()
        assert sentry_sdk.is_initialized()
        assert client.get_integration(DjangoIntegration)
        assert client.get_integration(RedisIntegration)
        assert client.options["profile_lifecycle"] == "trace"
        assert client.options["profile_session_sample_rate"] == pytest.approx(
            1.0 if environment == "production" else 0.0
        )
        assert client.options["attach_stacktrace"]
        assert client.options["traces_sampler"] is (
            sample_trace if environment == "production" else None
        )
        if environment == "production":
            assert_request_envelopes(envelopes)
            assert_invitation_notification_error(envelopes)
        django.setup()
        assert sentry_sdk.get_client() is client
        client.close()


if __name__ == "__main__":
    check_startup(sys.argv[1])
