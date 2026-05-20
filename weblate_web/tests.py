from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import PropertyMock, patch
from uuid import uuid4
from xml.etree import ElementTree  # noqa: S405

import responses
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.signing import dumps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import RestrictedError
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override
from requests.exceptions import HTTPError
from wlc import WeblateException

from weblate_web.invoices.models import Discount, Invoice, InvoiceCategory, InvoiceKind
from weblate_web.payments.models import Customer, Payment

from .exchange_rates import UncachedExchangeRates
from .hetzner import generate_random_password
from .management.commands.backups_sync import Command as BackupsSyncCommand
from .management.commands.recurring_payments import Command as RecurringPaymentsCommand
from .middleware import SecurityMiddleware
from .models import (
    REWARD_LEVELS,
    Package,
    PackageCategory,
    Post,
    Report,
    Service,
    ServiceKind,
    Subscription,
    add_subscription_past_payments,
    get_donation_package,
    get_donation_reward_package_name,
    sync_packages,
)
from .payments.validators import VAT_VALIDITY_DAYS
from .remote import (
    ACTIVITY_URL,
    PYPI_URL,
    WEBLATE_CONTRIBUTORS_URL,
    fetch_vat_info,
    get_activity,
    get_changes,
    get_contributors,
    get_release,
)
from .templatetags.downloads import downloadlink, filesizeformat
from .utils import FOSDEM_ORIGIN, PAYMENTS_ORIGIN
from .views import PostView, server_error

if TYPE_CHECKING:
    from uuid import UUID

    from django.core.mail.message import EmailMultiAlternatives

TEST_DATA = Path(__file__).parent / "test-data"
TEST_ROBOTS = Path(__file__).parent / "static" / "robots.txt"
TEST_SIGNATURE = Path(__file__).parent / "static" / "weblate-black.svg"
TEST_CONTRIBUTORS = TEST_DATA / "contributors.json"
TEST_ACTIVITY = TEST_DATA / "activity.json"
TEST_VIES_WSDL = TEST_DATA / "checkVatService.wsdl"
JSON_LD_RE = re.compile(
    rb'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
JSON_LD_NONCE_RE = re.compile(
    rb'<script[^>]*type="application/ld\+json"[^>]* nonce="([^"]+)"[^>]*>',
    re.DOTALL,
)
CSP_BASE64_VALUE_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
SENTRY_SCRIPT_NONCE_RE = re.compile(
    rb'<script nonce="([^"]+)">\s*Sentry\.init', re.DOTALL
)


def migrate_to_current_weblate_web_head(executor: MigrationExecutor) -> None:
    executor.migrate(
        [
            node
            for node in executor.loader.graph.leaf_nodes()
            if node[0] == "weblate_web"
        ]
    )


def migrate_to_current_payments_head(executor: MigrationExecutor) -> None:
    executor.migrate(
        [node for node in executor.loader.graph.leaf_nodes() if node[0] == "payments"]
    )


@dataclass
class UpcomingInvoiceCases:
    proforma: Invoice
    proforma_urls: list[str]
    invoice: Invoice
    invoice_urls: list[str]
    old_invoice: Invoice
    old_proforma: Invoice
    rejected_proforma: Invoice
    rejected_proforma_urls: list[str]
    rejected_invoice: Invoice
    rejected_invoice_urls: list[str]
    recovered_proforma: Invoice
    recovered_proforma_urls: list[str]
    recovered_invoice: Invoice
    recovered_invoice_urls: list[str]
    paid_invoice: Invoice
    paid_invoice_urls: list[str]


def get_json_ld(response) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(match))
        for match in JSON_LD_RE.findall(response.content)
    ]


def get_json_ld_by_type(response, schema_type: str) -> dict[str, Any]:
    for schema in get_json_ld(response):
        if schema.get("@type") == schema_type:
            return schema
    raise AssertionError(f"Missing JSON-LD schema of type {schema_type}")


RATES_JSON = {
    "rates": [
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Australia",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "AUD",
            "rate": 15.858,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Brazil",
            "currency": "real",
            "amount": 1,
            "currencyCode": "BRL",
            "rate": 5.686,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Bulgaria",
            "currency": "lev",
            "amount": 1,
            "currencyCode": "BGN",
            "rate": 13.162,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Canada",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "CAD",
            "rate": 17.081,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "China",
            "currency": "renminbi",
            "amount": 1,
            "currencyCode": "CNY",
            "rate": 3.334,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Croatia",
            "currency": "kuna",
            "amount": 1,
            "currencyCode": "HRK",
            "rate": 3.466,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Denmark",
            "currency": "krone",
            "amount": 1,
            "currencyCode": "DKK",
            "rate": 3.448,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "EMU",
            "currency": "euro",
            "amount": 1,
            "currencyCode": "EUR",
            "rate": 22.222,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Hongkong",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "HKD",
            "rate": 2.936,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Hungary",
            "currency": "forint",
            "amount": 100,
            "currencyCode": "HUF",
            "rate": 7.883,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Iceland",
            "currency": "krona",
            "amount": 100,
            "currencyCode": "ISK",
            "rate": 18.768,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "IMF",
            "currency": "SDR",
            "amount": 1,
            "currencyCode": "XDR",
            "rate": 31.851,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "India",
            "currency": "rupee",
            "amount": 100,
            "currencyCode": "INR",
            "rate": 32.828,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Indonesia",
            "currency": "rupiah",
            "amount": 1000,
            "currencyCode": "IDR",
            "rate": 1.595,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Israel",
            "currency": "new shekel",
            "amount": 1,
            "currencyCode": "ILS",
            "rate": 6.448,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Japan",
            "currency": "yen",
            "amount": 100,
            "currencyCode": "JPY",
            "rate": 21.045,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Malaysia",
            "currency": "ringgit",
            "amount": 1,
            "currencyCode": "MYR",
            "rate": 5.518,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Mexico",
            "currency": "peso",
            "amount": 1,
            "currencyCode": "MXN",
            "rate": 1.201,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "New Zealand",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "NZD",
            "rate": 15.051,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Norway",
            "currency": "krone",
            "amount": 1,
            "currencyCode": "NOK",
            "rate": 2.629,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Philippines",
            "currency": "peso",
            "amount": 100,
            "currencyCode": "PHP",
            "rate": 43.71,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Poland",
            "currency": "zloty",
            "amount": 1,
            "currencyCode": "PLN",
            "rate": 5.984,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Romania",
            "currency": "leu",
            "amount": 1,
            "currencyCode": "RON",
            "rate": 5.407,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Russia",
            "currency": "rouble",
            "amount": 100,
            "currencyCode": "RUB",
            "rate": 35.668,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Singapore",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "SGD",
            "rate": 16.758,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "South Korea",
            "currency": "won",
            "amount": 100,
            "currencyCode": "KRW",
            "rate": 1.929,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Sweden",
            "currency": "krona",
            "amount": 1,
            "currencyCode": "SEK",
            "rate": 2.392,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Switzerland",
            "currency": "franc",
            "amount": 1,
            "currencyCode": "CHF",
            "rate": 22.819,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Thailand",
            "currency": "baht",
            "amount": 100,
            "currencyCode": "THB",
            "rate": 72.534,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "Turkey",
            "currency": "lira",
            "amount": 1,
            "currencyCode": "TRY",
            "rate": 3.806,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "United Kingdom",
            "currency": "pound",
            "amount": 1,
            "currencyCode": "GBP",
            "rate": 29.395,
        },
        {
            "validFor": "2019-05-17",
            "order": 94,
            "country": "USA",
            "currency": "dollar",
            "amount": 1,
            "currencyCode": "USD",
            "rate": 23.048,
        },
    ]
}

TEST_CUSTOMER = {
    "name": "Michal Čihař",
    "address": "Zdiměřická 1439",
    "city": "Praha 4",
    "postcode": "149 00",
    "country": "CZ",
    "vat_0": "CZ",
    "vat_1": "8003280318",
}

THEPAY2_MOCK_SETTINGS: dict[str, str | bool] = {
    "PAYMENT_DEBUG": True,
    "THEPAY_MERCHANT_ID": "00000000-0000-0000-0000-000000000000",
    "THEPAY_PASSWORD": "test-password",
    "THEPAY_PROJECT_ID": "42",
    "THEPAY_SERVER": "demo.api.thepay.cz",
}
SIGNATURE_MOCK_SETTINGS: dict[str, str] = {
    "AGREEMENTS_SIGNATURE_PATH": TEST_SIGNATURE.as_posix(),
}


def thepay_mock_create_payment() -> None:
    responses.post(
        "https://demo.api.thepay.cz/v1/projects/42/payments",
        json={
            "pay_url": "https://gate.thepay.cz/12345/pay",
            "detail_url": "https://gate.thepay.cz/12345/state",
        },
    )


def thepay_mock_repeated_payment() -> None:
    responses.post(
        re.compile(
            r"https://demo.api.thepay.cz/v2/projects/42/payments/[a-f0-9-]{36}/savedauthorization"
        ),
        json={
            "state": "paid",
            "message": "Ok",
            "parent": {"recurring_payments_available": True},
        },
    )


def cnb_mock_rates() -> None:
    responses.get(
        f"https://api.cnb.cz/cnbapi/exrates/daily?date={timezone.now().date().isoformat()}",
        json=RATES_JSON,
    )
    # Matches Fio payments mock
    responses.get(
        "https://api.cnb.cz/cnbapi/exrates/daily?date=2016-07-29",
        json=RATES_JSON,
    )


def thepay_mock_payment(payment: str | UUID) -> None:
    responses.get(
        f"https://demo.api.thepay.cz/v1/projects/42/payments/{payment}?merchant_id=00000000-0000-0000-0000-000000000000",
        json={
            "uid": "efd7d8e6-2fa3-3c46-b475-51762331bf56",
            "project_id": 1,
            "order_id": "CZ12131415",
            "state": "paid",
            "currency": "CZK",
            "amount": 87654,
            "paid_amount": 87654,
            "created_at": "2019-01-01T12:00:00+00:00",
            "finished_at": "2019-01-01T12:00:00+00:00",
            "valid_to": "2019-01-01T12:00:00+00:00",
            "fee": 121,
            "description": "Some description of the payment purpose.",
            "description_for_merchant": "Some description for merchant.",
            "payment_method": "card",
            "pay_url": "https://gate.thepay.cz/12345/pay",
            "detail_url": "https://gate.thepay.cz/12345/state",
            "customer": {
                "name": "Joe Doe",
                "ip": "192.168.0.1",
                "email": "joe.doe@gmail.com",
            },
            "offset_account": {
                "iban": "CZ6508000000192000145399",
                "owner_name": "Joe Doe",
            },
            "offset_account_status": "not_available",
            "offset_account_determined_at": "2019-01-01T12:00:00+00:00",
            "card": {
                "number": "515735******2654",
                "expiration_date": "2022-05",
                "brand": "MASTERCARD",
                "type": "debit",
            },
            "events": [
                {
                    "occured_at": "2021-04-20T11:05:49.000000Z",
                    "type": "state_change",
                    "data": "expired",
                }
            ],
            "parent": {"recurring_payments_available": True},
        },
    )
    cnb_mock_rates()


def mock_vies(valid: bool = True) -> None:
    responses.add(
        responses.GET,
        "https://ec.europa.eu/taxation_customs/vies/checkVatService.wsdl",
        body=TEST_VIES_WSDL.read_text(),
    )
    if valid:
        payload = """
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
    <env:Header/>
    <env:Body>
        <ns2:checkVatResponse xmlns:ns2="urn:ec.europa.eu:taxud:vies:services:checkVat:types">
            <ns2:countryCode>CZ</ns2:countryCode>
            <ns2:vatNumber>8003280318</ns2:vatNumber>
            <ns2:requestDate>2024-07-09+02:00</ns2:requestDate>
            <ns2:valid>true</ns2:valid>
            <ns2:name>Ing. Michal Čihař</ns2:name>
            <ns2:address>Nábřežní 694
CVIKOV II
471 54  CVIKOV</ns2:address>
        </ns2:checkVatResponse>
    </env:Body>
</env:Envelope>
"""
    else:
        payload = """
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
    <env:Header/>
    <env:Body>
        <ns2:checkVatResponse xmlns:ns2="urn:ec.europa.eu:taxud:vies:services:checkVat:types">
            <ns2:countryCode>CZ</ns2:countryCode>
            <ns2:vatNumber>8003280317</ns2:vatNumber>
            <ns2:requestDate>2024-07-09+02:00</ns2:requestDate>
            <ns2:valid>false</ns2:valid>
            <ns2:name>---</ns2:name>
            <ns2:address>---</ns2:address>
        </ns2:checkVatResponse>
    </env:Body>
</env:Envelope>
"""

    responses.add(
        responses.POST,
        "https://ec.europa.eu/taxation_customs/vies/services/checkVatService",
        body=payload,
    )


def fake_remote() -> None:
    cache.set(
        "VAT-CZ8003280318",
        {
            "countryCode": "CZ",
            "vatNumber": "8003280318",
            "requestDate": date(2020, 3, 20),
            "valid": True,
            "name": "Ing. Michal Čihař",
            "address": "Zdiměřická 1439/8\nPRAHA 11 - CHODOV\n149 00  PRAHA 415",
        },
    )
    cache.set("wlweb-contributors", [])
    cache.set("wlweb-activity-stats", [])
    cache.set(
        "wlweb-changes-list",
        [
            {
                "failing_percent": 0.4,
                "translated_percent": 20.3,
                "total_words": 12385202,
                "failing": 3708,
                "translated_words": 1773069,
                "url_translate": "https://hosted.weblate.org/projects/godot-engine/",
                "fuzzy_percent": 3.9,
                "recent_changes": 2401,
                "translated": 160302,
                "fuzzy": 31342,
                "total": 787070,
                "last_change": timezone.now(),
                "name": "Godot Engine",
                "url": "https://hosted.weblate.org/engage/godot-engine/",
            },
            {
                "failing_percent": 1.4,
                "translated_percent": 46.0,
                "total_words": 7482588,
                "failing": 14319,
                "translated_words": 2917305,
                "url_translate": "https://hosted.weblate.org/projects/phpmyadmin/",
                "fuzzy_percent": 12.3,
                "recent_changes": 3652,
                "translated": 465082,
                "fuzzy": 124794,
                "total": 1009956,
                "last_change": timezone.now() - timedelta(seconds=3600),
                "name": "phpMyAdmin",
                "url": "https://hosted.weblate.org/engage/phpmyadmin/",
            },
            {
                "failing_percent": 0.4,
                "translated_percent": 48.8,
                "total_words": 1386375,
                "failing": 1121,
                "translated_words": 586192,
                "url_translate": "https://hosted.weblate.org/projects/weblate/",
                "fuzzy_percent": 9.5,
                "recent_changes": 2864,
                "translated": 125298,
                "fuzzy": 24461,
                "total": 256275,
                "last_change": timezone.now() - timedelta(seconds=14400),
                "name": "Weblate",
                "url": "https://hosted.weblate.org/engage/weblate/",
            },
            {
                "failing_percent": 0.8,
                "translated_percent": 20.1,
                "total_words": 7707495,
                "failing": 4459,
                "translated_words": 1066440,
                "url_translate": "https://hosted.weblate.org/projects/f-droid/",
                "fuzzy_percent": 0.7,
                "recent_changes": 7080,
                "translated": 104941,
                "fuzzy": 4011,
                "total": 520867,
                "last_change": timezone.now() - timedelta(days=1),
                "name": "F-Droid",
                "url": "https://hosted.weblate.org/engage/f-droid/",
            },
            {
                "failing_percent": 1.0,
                "translated_percent": 72.9,
                "total_words": 324480,
                "failing": 883,
                "translated_words": 231003,
                "url_translate": "https://hosted.weblate.org/projects/freeplane/",
                "fuzzy_percent": 2.1,
                "recent_changes": 535,
                "translated": 61980,
                "fuzzy": 1787,
                "total": 84920,
                "last_change": timezone.now() - timedelta(days=4),
                "name": "Freeplane",
                "url": "https://hosted.weblate.org/engage/freeplane/",
            },
            {
                "failing_percent": 4.4,
                "translated_percent": 57.8,
                "total_words": 2016830,
                "failing": 25211,
                "translated_words": 1036249,
                "url_translate": "https://hosted.weblate.org/projects/osmand/",
                "fuzzy_percent": 3.0,
                "recent_changes": 3633,
                "translated": 325725,
                "fuzzy": 16981,
                "total": 562798,
                "last_change": timezone.now() - timedelta(days=30),
                "name": "OsmAnd",
                "url": "https://hosted.weblate.org/engage/osmand/",
            },
        ],
    )


class UserTestCase(TestCase):
    credentials = {
        "username": "testuser",
        "password": "testpassword",
        "email": "noreply@weblate.org",
    }
    _user = None

    def login(self):
        user = self.create_user()
        self.client.login(**self.credentials)
        return user

    def create_user(self):
        if self._user is None:
            self._user = User.objects.create_user(**self.credentials)
        return self._user


class PostTestCase(TestCase):
    @staticmethod
    def create_post(title="testpost", body="testbody", timestamp=None):
        if timestamp is None:
            timestamp = timezone.now() - timedelta(days=1)
        return Post.objects.create(
            title=title, slug=title, body=body, timestamp=timestamp
        )


class ViewTestCase(PostTestCase):
    """Views testing."""

    def setUp(self) -> None:
        super().setUp()
        fake_remote()
        sync_packages()

    def test_index_redirect(self) -> None:
        response = self.client.get("/")
        links = response["Link"]

        self.assertRedirects(response, "/en/", 302)
        self.assertIn('</en/support/>; rel="help"', links)
        self.assertIn('</en/privacy/>; rel="privacy-policy"', links)
        self.assertIn('</en/terms/>; rel="terms-of-service"', links)
        self.assertIn('</site.webmanifest>; rel="manifest"', links)

    def test_index_en(self) -> None:
        response = self.client.get("/en/")
        self.assertContains(response, "yearly")

    def test_index_link_headers(self) -> None:
        response = self.client.get("/en/")
        links = response["Link"]

        self.assertIn('</en/support/>; rel="help"', links)
        self.assertIn('</en/privacy/>; rel="privacy-policy"', links)
        self.assertIn('</en/terms/>; rel="terms-of-service"', links)
        self.assertIn('</site.webmanifest>; rel="manifest"', links)

    def test_index_cs(self) -> None:
        response = self.client.get("/cs/")
        self.assertContains(response, "ročně")

    def test_index_link_headers_localized(self) -> None:
        response = self.client.get("/cs/")
        links = response["Link"]

        self.assertIn('</cs/support/>; rel="help"', links)
        self.assertIn('</cs/privacy/>; rel="privacy-policy"', links)
        self.assertIn('</cs/terms/>; rel="terms-of-service"', links)

    def test_index_he(self) -> None:
        response = self.client.get("/he/")
        self.assertContains(response, "שנתי")

    def test_index_ar(self) -> None:
        response = self.client.get("/ar/")
        self.assertContains(response, 'lang="ar"')
        self.assertContains(response, 'dir="rtl"')
        self.assertContains(
            response,
            '<h2 class="section-title hp-style">المستخدمون والداعمون</h2>',
            html=True,
        )

    def test_index_be(self) -> None:
        response = self.client.get("/be/")
        self.assertContains(response, "штогод")

    def test_index_be_latin(self) -> None:
        response = self.client.get("/be-latn/")
        self.assertContains(response, "Nieabmiežavany")

    def test_terms(self) -> None:
        response = self.client.get("/en/terms/")
        self.assertContains(response, "21668027")

    def test_privacy(self) -> None:
        response = self.client.get("/en/privacy/")
        self.assertContains(response, "21668027")

    def test_security_txt(self) -> None:
        response = self.client.get("/security.txt", follow=True)
        self.assertRedirects(response, "/.well-known/security.txt", status_code=301)
        self.assertContains(response, "https://hackerone.com/weblate")

    def test_robots_txt_content_signals(self) -> None:
        response = self.client.get("/robots.txt")

        self.assertRedirects(
            response,
            "/static/robots.txt",
            status_code=301,
            fetch_redirect_response=False,
        )
        self.assertIn(
            "Content-Signal: ai-train=no, search=yes, ai-input=no",
            TEST_ROBOTS.read_text(),
        )

    def test_localized_docs(self) -> None:
        response = self.client.get("/uk/contribute/")
        self.assertContains(response, "https://docs.weblate.org/uk/latest/contributing")

    @responses.activate
    def test_about(self) -> None:
        responses.add(
            responses.GET, WEBLATE_CONTRIBUTORS_URL, body=TEST_CONTRIBUTORS.read_text()
        )
        get_contributors(force=True)
        response = self.client.get("/en/about/")
        self.assertContains(response, "comradekingu")
        # Test error handling, cached content should stay there
        responses.replace(responses.GET, WEBLATE_CONTRIBUTORS_URL, status=500)
        get_contributors(force=True)
        response = self.client.get("/en/about/")
        self.assertContains(response, "comradekingu")

    def test_site_json_ld(self) -> None:
        response = self.client.get("/en/about/")

        schemas = get_json_ld(response)

        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["@context"], "https://schema.org")
        graph = schemas[0]["@graph"]
        types = {item["@type"] for item in graph}
        self.assertEqual(types, {"Organization", "WebSite"})
        organization = next(item for item in graph if item["@type"] == "Organization")
        website = next(item for item in graph if item["@type"] == "WebSite")
        self.assertEqual(organization["@id"], "https://weblate.org/#organization")
        self.assertEqual(organization["alternateName"], "Weblate")
        self.assertEqual(organization["url"], "https://weblate.org/")
        self.assertEqual(
            organization["logo"], "https://weblate.org/static/weblate-512.png"
        )
        self.assertEqual(website["@id"], "https://weblate.org/#website")
        self.assertEqual(website["publisher"], {"@id": organization["@id"]})

    @override_settings(DEBUG=False, COMPRESS_ENABLED=False)
    def test_site_json_ld_csp_nonce(self) -> None:
        response = self.client.get("/en/about/")
        nonces = JSON_LD_NONCE_RE.findall(response.content)

        self.assertEqual(len(nonces), 1)
        nonce = nonces[0].decode()
        self.assertRegex(nonce, CSP_BASE64_VALUE_RE)
        self.assertIn(f"'nonce-{nonce}'", response["Content-Security-Policy"])

    @override_settings(DEBUG=False, COMPRESS_ENABLED=False)
    @patch("sentry_sdk.last_event_id", return_value="test-event-id")
    def test_server_error_sentry_script_csp_nonce(self, last_event_id) -> None:
        request = RequestFactory().get("/en/test-error/")
        response = SecurityMiddleware(server_error)(request)
        nonces = SENTRY_SCRIPT_NONCE_RE.findall(response.content)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(nonces), 1)
        nonce = nonces[0].decode()
        self.assertRegex(nonce, CSP_BASE64_VALUE_RE)
        self.assertIn(f"'nonce-{nonce}'", response["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", response["Content-Security-Policy"])
        self.assertContains(response, "test-event-id", status_code=500)
        last_event_id.assert_called_once_with()

    @responses.activate
    def test_activity(self) -> None:
        responses.add(responses.GET, ACTIVITY_URL, body=TEST_ACTIVITY.read_text())
        get_activity(force=True)
        response = self.client.get("/img/activity.svg")
        self.assertContains(response, "<svg")
        # Test error handling, cached content should stay there
        responses.replace(responses.GET, ACTIVITY_URL, status=500)
        get_activity(force=True)
        response = self.client.get("/img/activity.svg")
        self.assertContains(response, "<svg")

    def test_download_en(self) -> None:
        response = self.client.get("/en/download/")
        self.assertContains(response, "Download Weblate")

    def test_sitemap_lang(self) -> None:
        response = self.client.get("/sitemap-es.xml")
        self.assertContains(response, "http://testserver/es/features/")

    def test_sitemap_news(self) -> None:
        self.create_post()
        response = self.client.get("/sitemap-news.xml")
        self.assertContains(response, "testpost")

    def test_sitemaps(self) -> None:
        # Get root sitemap
        response = self.client.get("/sitemap.xml")
        self.assertContains(response, "<sitemapindex")

        # Parse it
        tree = ElementTree.fromstring(response.content)  # noqa: S314
        sitemaps = tree.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap")
        for sitemap in sitemaps:
            location = sitemap.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            self.assertIsNotNone(location)
            response = self.client.get(
                cast("str", cast("ElementTree.Element", location).text)
            )
            self.assertContains(response, "<urlset")
            # Try if it's a valid XML
            ElementTree.fromstring(response.content)  # noqa: S314


class UtilTestCase(TestCase):
    """Helper code testing."""

    def test_format(self) -> None:
        self.assertEqual(filesizeformat(0), "0 bytes")
        self.assertEqual(filesizeformat(1000), "1000 bytes")
        self.assertEqual(filesizeformat(1000000), "976.6 KiB")
        self.assertEqual(filesizeformat(1000000000), "953.7 MiB")
        self.assertEqual(filesizeformat(10000000000000), "9313.2 GiB")

    def test_downloadlink(self) -> None:
        self.assertEqual(
            downloadlink(
                {
                    "comment_text": "",
                    "digests": {
                        "blake2b_256": "67b8258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a",
                        "md5": "e4acec80cbda61a4dffbc591062c1e0e",
                        "sha256": "59224c80144b7784b6efb6dae6bc17745cbbb7938c417c436237d695d75a7db2",
                    },
                    "downloads": -1,
                    "filename": "Weblate-5.3.1-py3-none-any.whl",
                    "has_sig": False,
                    "md5_digest": "e4acec80cbda61a4dffbc591062c1e0e",
                    "packagetype": "bdist_wheel",
                    "python_version": "py3",
                    "requires_python": ">=3.9",
                    "size": 68485094,
                    "upload_time": "2023-12-19T14:02:25",
                    "upload_time_iso_8601": "2023-12-19T14:02:25.035680Z",
                    "url": "https://files.pythonhosted.org/packages/67/b8/258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a/Weblate-5.3.1-py3-none-any.whl",
                    "yanked": False,
                    "yanked_reason": None,
                }
            ),
            {
                "name": "Weblate-5.3.1-py3-none-any.whl",
                "size": "65.3 MiB",
                "text": "Python Wheel package",
                "url": "https://files.pythonhosted.org/packages/67/b8/258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a/Weblate-5.3.1-py3-none-any.whl",
            },
        )
        self.assertEqual(
            downloadlink(
                {
                    "comment_text": "",
                    "digests": {
                        "blake2b_256": "149cb3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857",
                        "md5": "b0cb1a21719712693ea85e4c14ba8559",
                        "sha256": "0b3a3862b3703efee302b62d28914e5b610405eec9db1c232729b31f41a694e5",
                    },
                    "downloads": -1,
                    "filename": "Weblate-5.3.1.tar.gz",
                    "has_sig": False,
                    "md5_digest": "b0cb1a21719712693ea85e4c14ba8559",
                    "packagetype": "sdist",
                    "python_version": "source",
                    "requires_python": ">=3.9",
                    "size": 69811080,
                    "upload_time": "2023-12-19T14:02:53",
                    "upload_time_iso_8601": "2023-12-19T14:02:53.564201Z",
                    "url": "https://files.pythonhosted.org/packages/14/9c/b3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857/Weblate-5.3.1.tar.gz",
                    "yanked": False,
                    "yanked_reason": None,
                }
            ),
            {
                "name": "Weblate-5.3.1.tar.gz",
                "size": "66.6 MiB",
                "text": "Sources tarball, gzip compressed",
                "url": "https://files.pythonhosted.org/packages/14/9c/b3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857/Weblate-5.3.1.tar.gz",
            },
        )


def create_payment(
    *,
    recurring: Literal["y", ""] = "y",
    user: User,
    customer: Customer | None = None,
    **kwargs,
) -> tuple[Payment, str, str]:
    if customer is None:
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
        )
    customer.users.add(user)
    payment = Payment.objects.create(
        customer=customer,
        amount=100,
        description="Test payment",
        backend="pay",
        recurring=recurring,
        **kwargs,
    )
    return (
        payment,
        reverse("payment", kwargs={"pk": payment.pk}),
        reverse("payment-customer", kwargs={"pk": payment.pk}),
    )


class FakturaceTestCase(UserTestCase):
    def assert_notifications(self, *subjects: str) -> list[EmailMultiAlternatives]:
        self.assertEqual(sorted(m.subject for m in mail.outbox), sorted(subjects))
        result = cast("list[EmailMultiAlternatives]", mail.outbox)
        mail.outbox = []
        return result

    def create_donation(
        self,
        years: int = 1,
        days: int = 0,
        recurring: Literal["y", ""] = "y",
        reward: int = 3,
    ) -> Service:
        user = self.create_user()
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        customer.users.add(user)
        service = Service.objects.create(
            customer=customer,
            kind=ServiceKind.DONATION,
            donation_link_url="https://example.com/weblate",
            donation_link_text="Weblate donation test",
        )
        subscription = service.subscription_set.create(
            package=get_donation_package(reward),
            expires=timezone.now() + timedelta(days=days) + relativedelta(years=years),
        )
        subscription.payment = create_payment(
            recurring=recurring,
            user=user,
            state=Payment.PROCESSED,
            customer=customer,
        )[0]
        subscription.save(update_fields=["payment"])
        return service

    def create_packages(self) -> None:
        Package.objects.bulk_create(
            [
                Package(name="community", verbose="Community support", price=0),
                Package(
                    name="extended",
                    verbose="Extended support",
                    price=42,
                    category=PackageCategory.PACKAGE_SUPPORT,
                ),
                Package(
                    name="test:test-1-m",
                    verbose="Weblate hosting (basic)",
                    price=42,
                    category=PackageCategory.PACKAGE_DEDICATED,
                ),
                Package(
                    name="test:test-1",
                    verbose="Weblate hosting (basic)",
                    price=420,
                    category=PackageCategory.PACKAGE_DEDICATED,
                ),
                Package(
                    name="test:test-2",
                    verbose="Weblate hosting (upgraded)",
                    price=840,
                    category=PackageCategory.PACKAGE_DEDICATED,
                ),
            ]
        )

    def create_service(
        self,
        years: int = 1,
        days: int = 0,
        recurring: Literal["y", ""] = "y",
        package: str = "extended",
    ) -> Service:
        user = self.create_user()
        self.create_packages()
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        customer.users.add(user)
        service = Service.objects.create(customer=customer)
        subscription = service.subscription_set.create(
            package=Package.objects.get_or_create(name=package)[0],
            expires=timezone.now() + timedelta(days=days) + relativedelta(years=years),
        )
        subscription.payment = create_payment(
            recurring=recurring,
            user=user,
            customer=customer,
            extra={"subscription": subscription.pk},
            state=Payment.PROCESSED,
        )[0]
        subscription.save(update_fields=["payment"])
        return service


class DonationMigration0049Test(TransactionTestCase):
    migrate_from = [("weblate_web", "0048_service_maintenance_window")]
    migrate_to = [("weblate_web", "0049_consolidate_donations")]

    def setUp(self) -> None:
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps

    def tearDown(self) -> None:
        self.executor = MigrationExecutor(connection)
        migrate_to_current_weblate_web_head(self.executor)
        super().tearDown()

    def test_donation_rewards_are_migrated_to_subscription_packages(  # noqa: PLR0914
        self,
    ) -> None:
        donation_model = self.old_apps.get_model("weblate_web", "Donation")
        past_payments_model = self.old_apps.get_model("weblate_web", "PastPayments")

        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="US",
        )
        created = timezone.now() - timedelta(days=30)
        expires = timezone.now() + timedelta(days=30)
        donations = []
        for reward in range(4):
            payment = Payment.objects.create(
                customer=customer,
                amount=100 + reward,
                description=f"Donation {reward}",
                state=Payment.PROCESSED,
            )
            donation = donation_model.objects.create(
                customer_id=customer.pk,
                payment=payment.pk,
                reward=reward,
                link_text=f"Donor {reward}",
                link_url=f"https://example.com/{reward}",
                link_image=f"donations/{reward}.png",
                expires=expires + timedelta(days=reward),
                active=reward != 1,
            )
            donation_model.objects.filter(pk=donation.pk).update(created=created)
            donation.created = created
            donations.append(donation)

        past_payment = Payment.objects.create(
            customer=customer,
            amount=42,
            description="Past donation",
            state=Payment.PROCESSED,
        )
        past_payments_model.objects.create(
            donation=donations[3],
            payment=past_payment.pk,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        package_model = new_apps.get_model("weblate_web", "Package")
        past_payments_model = new_apps.get_model("weblate_web", "PastPayments")
        service_model = new_apps.get_model("weblate_web", "Service")
        subscription_model = new_apps.get_model("weblate_web", "Subscription")

        self.assertNotIn(
            "donation_reward",
            {
                field.name
                for field in service_model._meta.fields  # pylint: disable=protected-access
            },
        )
        self.assertNotIn(
            "donation",
            {
                field.name
                for field in past_payments_model._meta.fields  # pylint: disable=protected-access
            },
        )
        for reward, donation in enumerate(donations):
            service = service_model.objects.get(donation_legacy_id=donation.pk)
            subscription = subscription_model.objects.get(service=service)
            package = package_model.objects.get(pk=subscription.package_id)
            self.assertEqual(package.name, get_donation_reward_package_name(reward))
            self.assertEqual(package.price, REWARD_LEVELS[reward])
            self.assertEqual(service.kind, ServiceKind.DONATION)
            self.assertEqual(service.donation_link_text, f"Donor {reward}")
            self.assertEqual(service.donation_link_url, f"https://example.com/{reward}")
            self.assertEqual(service.donation_link_image, f"donations/{reward}.png")
            self.assertEqual(subscription.payment, donation.payment)
            self.assertEqual(subscription.enabled, reward != 1)
            self.assertEqual(subscription.expires, expires + timedelta(days=reward))

        migrated_past_payment = past_payments_model.objects.get(payment=past_payment.pk)
        self.assertEqual(
            migrated_past_payment.subscription_id,
            subscription_model.objects.get(
                service__donation_legacy_id=donations[3].pk
            ).pk,
        )


class SubscriptionPaymentMigration0050Test(TransactionTestCase):
    migrate_from = [("weblate_web", "0049_consolidate_donations")]
    migrate_to = [("weblate_web", "0050_subscription_payment_fk")]

    def setUp(self) -> None:
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps

    def tearDown(self) -> None:
        self.executor = MigrationExecutor(connection)
        migrate_to_current_weblate_web_head(self.executor)
        super().tearDown()

    def test_subscription_payments_are_migrated_to_relations(self) -> None:
        package_model = self.old_apps.get_model("weblate_web", "Package")
        past_payments_model = self.old_apps.get_model("weblate_web", "PastPayments")
        service_model = self.old_apps.get_model("weblate_web", "Service")
        subscription_model = self.old_apps.get_model("weblate_web", "Subscription")

        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="US",
        )
        package = package_model.objects.create(
            name="migration-test", verbose="Migration test", price=42
        )
        service = service_model.objects.create(customer_id=customer.pk)
        current_payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Current payment",
            state=Payment.PROCESSED,
        )
        past_payment = Payment.objects.create(
            customer=customer,
            amount=50,
            description="Past payment",
            state=Payment.PROCESSED,
        )
        subscription = subscription_model.objects.create(
            service_id=service.pk,
            package_id=package.pk,
            payment=current_payment.pk,
            expires=timezone.now(),
        )
        past_payments_model.objects.create(
            subscription_id=subscription.pk,
            payment=past_payment.pk,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        subscription_model = new_apps.get_model("weblate_web", "Subscription")

        migrated_subscription = subscription_model.objects.get(pk=subscription.pk)
        self.assertEqual(migrated_subscription.payment_id, current_payment.pk)
        self.assertEqual(
            set(migrated_subscription.past_payments.values_list("pk", flat=True)),
            {past_payment.pk},
        )
        with self.assertRaises(LookupError):
            new_apps.get_model("weblate_web", "PastPayments")

    def test_orphan_payment_references_stop_migration(self) -> None:
        package_model = self.old_apps.get_model("weblate_web", "Package")
        past_payments_model = self.old_apps.get_model("weblate_web", "PastPayments")
        service_model = self.old_apps.get_model("weblate_web", "Service")
        subscription_model = self.old_apps.get_model("weblate_web", "Subscription")
        migration = import_module("weblate_web.migrations.0050_subscription_payment_fk")
        validate_payment_references_migration = (
            migration.validate_payment_references_migration
        )
        self.assertEqual(
            migration.Migration.operations[0].code,
            validate_payment_references_migration,
        )

        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="US",
        )
        package = package_model.objects.create(
            name="migration-orphan-test", verbose="Migration orphan test", price=42
        )
        service = service_model.objects.create(customer_id=customer.pk)
        subscription = subscription_model.objects.create(
            service_id=service.pk,
            package_id=package.pk,
            payment=uuid4(),
            expires=timezone.now(),
        )
        past_payments_model.objects.create(
            subscription_id=subscription.pk,
            payment=uuid4(),
        )

        with self.assertRaisesMessage(
            ValueError, "Can not migrate subscription payment references"
        ):
            validate_payment_references_migration(self.old_apps, None)
        subscription_model.objects.filter(pk=subscription.pk).update(payment=None)
        past_payments_model.objects.filter(subscription_id=subscription.pk).delete()

    def test_format_missing_payments_suffix_only_for_more_than_ten(self) -> None:
        migration = import_module("weblate_web.migrations.0050_subscription_payment_fk")
        payment_ids = [uuid4() for _unused in range(11)]

        self.assertEqual(
            migration.format_missing_payments(payment_ids[:10]),
            ", ".join(str(payment_id) for payment_id in payment_ids[:10]),
        )
        self.assertEqual(
            migration.format_missing_payments(payment_ids),
            f"{', '.join(str(payment_id) for payment_id in payment_ids[:10])}, ...",
        )


class CustomerVatValidationStateMigration0058Test(TransactionTestCase):
    migrate_from = [("payments", "0057_customer_upcoming_payment_notification_days")]
    migrate_to = [("payments", "0058_customer_vat_validation_state_and_error")]

    def setUp(self) -> None:
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps

    def tearDown(self) -> None:
        self.executor = MigrationExecutor(connection)
        migrate_to_current_payments_head(self.executor)
        super().tearDown()

    def test_existing_validated_customers_are_backfilled_as_valid(self) -> None:
        customer_model = self.old_apps.get_model("payments", "Customer")
        validated = customer_model.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="CZ",
            vat="CZ8003280318",
            vat_validated=timezone.now(),
        )
        unknown = customer_model.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="CZ",
            vat="CZ8003280317",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        customer_model = new_apps.get_model("payments", "Customer")

        validated.refresh_from_db()
        migrated_validated = customer_model.objects.get(pk=validated.pk)
        migrated_unknown = customer_model.objects.get(pk=unknown.pk)
        self.assertEqual(
            migrated_validated.vat_validation_state,
            Customer.VatValidationState.VALID,
        )
        self.assertEqual(migrated_validated.vat_validation_error, {})
        self.assertEqual(
            migrated_unknown.vat_validation_state,
            Customer.VatValidationState.UNKNOWN,
        )


class PaymentsTest(FakturaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        fake_remote()

    def create_vat_customer(self) -> Customer:
        return Customer.objects.create(
            email="weblate@example.com",
            user_id=self.create_user().pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
            vat=f"{TEST_CUSTOMER['vat_0']}{TEST_CUSTOMER['vat_1']}",
        )

    def prepare_payment(self):
        with override("en"):
            payment, url, customer_url = create_payment(user=self.create_user())
            response = self.client.get(url, follow=True)
            self.assertRedirects(response, customer_url)
            self.assertContains(response, "Please provide your billing")
            response = self.client.post(
                customer_url,
                TEST_CUSTOMER,
                follow=True,
            )
            self.assertRedirects(response, url)
            self.assertContains(response, "Test payment")
            self.assertContains(response, "€121.0")
            return payment, url, customer_url

    def test_view(self) -> None:
        self.prepare_payment()

    def check_payment(self, payment, state) -> None:
        fresh = Payment.objects.get(pk=payment.pk)
        self.assertEqual(fresh.state, state)

    @override_settings(PAYMENT_DEBUG=True)
    @responses.activate
    def test_pay(self) -> None:
        cnb_mock_rates()
        payment, url, _dummy = self.prepare_payment()
        response = self.client.post(url, {"method": "pay"})
        self.assertRedirects(
            response,
            f"/en/donate/process/?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.ACCEPTED)

        self.login()

        invoice_url = reverse("user-invoice", kwargs={"pk": payment.pk})
        response = self.client.get(invoice_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")

        response = self.client.get(f"{invoice_url}?receipt=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")

    def test_user_invoice_denies_other_payment_customer(self) -> None:
        user = self.create_user()
        other_user = User.objects.create_user(
            username="otheruser",
            email="other@example.com",
        )
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        customer.users.add(user)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )

        self.client.force_login(other_user)
        response = self.client.get(reverse("user-invoice", kwargs={"pk": payment.pk}))
        self.assertEqual(response.status_code, 404)

    def test_user_invoice_denies_other_pending_payment_customer(self) -> None:
        user = self.create_user()
        other_user = User.objects.create_user(
            username="otheruser",
            email="other@example.com",
        )
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        customer.users.add(user)
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.PROFORMA,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.PENDING,
            draft_invoice=invoice,
        )

        self.client.force_login(other_user)
        response = self.client.get(reverse("user-invoice", kwargs={"pk": payment.pk}))
        self.assertEqual(response.status_code, 404)

    def test_staff_receipt_unpaid_invoice_404(self) -> None:
        user = self.create_user()
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )

        response = self.client.get(
            reverse("invoice-pdf", kwargs={"pk": invoice.pk}) + "?receipt=1"
        )
        self.assertEqual(response.status_code, 404)

    def test_staff_receipt_oserror_404(self) -> None:
        user = self.create_user()
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )

        with patch("pathlib.Path.open", side_effect=OSError("cannot open")):
            response = self.client.get(
                reverse("invoice-pdf", kwargs={"pk": invoice.pk}) + "?receipt=1"
            )
        self.assertEqual(response.status_code, 404)

    def test_staff_receipt_valueerror_404(self) -> None:
        user = self.create_user()
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )

        with patch(
            "weblate_web.invoices.models.Invoice.receipt_filename",
            new_callable=PropertyMock,
            side_effect=ValueError("no receipt"),
        ):
            response = self.client.get(
                reverse("invoice-pdf", kwargs={"pk": invoice.pk}) + "?receipt=1"
            )
        self.assertEqual(response.status_code, 404)

    @responses.activate
    def test_paid_receipt_missing_file_404(self) -> None:
        cnb_mock_rates()
        user = self.login()
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        invoice.invoiceitem_set.create(description="Test item", unit_price=100)
        invoice.generate_files()
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )
        paid_invoice = payment.paid_invoice
        self.assertIsNotNone(paid_invoice)
        paid_invoice = cast("Invoice", paid_invoice)
        self.assertTrue(paid_invoice.is_paid)
        paid_invoice.receipt_path.unlink(missing_ok=True)

        response = self.client.get(
            reverse("user-invoice", kwargs={"pk": payment.pk}) + "?receipt=1"
        )
        self.assertEqual(response.status_code, 404)

    def test_user_receipt_unpaid_paid_invoice_404(self) -> None:
        user = self.login()
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )

        with patch(
            "weblate_web.invoices.models.Invoice.is_paid",
            new_callable=PropertyMock,
            return_value=False,
        ):
            response = self.client.get(
                reverse("user-invoice", kwargs={"pk": payment.pk}) + "?receipt=1"
            )
        self.assertEqual(response.status_code, 404)

    def test_user_receipt_valueerror_404(self) -> None:
        user = self.login()
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=user.pk,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        invoice = Invoice.objects.create(
            customer=customer,
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring="",
            state=Payment.ACCEPTED,
            paid_invoice=invoice,
        )

        with patch(
            "weblate_web.invoices.models.Invoice.receipt_filename",
            new_callable=PropertyMock,
            side_effect=ValueError("no receipt"),
        ):
            response = self.client.get(
                reverse("user-invoice", kwargs={"pk": payment.pk}) + "?receipt=1"
            )
        self.assertEqual(response.status_code, 404)

    @responses.activate
    def test_invalid_vat(self) -> None:
        mock_vies(valid=False)
        cnb_mock_rates()
        payment, url, customer_url = self.prepare_payment()
        # Inject invalid VAT
        customer = Customer.objects.get(pk=payment.customer.pk)
        customer.vat = "CZ8003280317"
        customer.save()

        response = self.client.get(url, follow=True)
        self.assertRedirects(response, customer_url)
        self.assertContains(response, "The VAT ID is no longer valid")

    @responses.activate
    def test_prefetch_vat(self) -> None:
        mock_vies(valid=True)
        cnb_mock_rates()
        self.prepare_payment()
        call_command("background_vat")

    def test_prefetch_vat_filtering(self) -> None:
        now = timezone.now()
        never_validated = self.create_vat_customer()
        due = self.create_vat_customer()
        recent = self.create_vat_customer()
        Customer.objects.filter(pk=never_validated.pk).update(
            vat_validated=None,
            vat_validation_state=Customer.VatValidationState.UNKNOWN,
            vat_validation_error={},
        )
        Customer.objects.filter(pk=due.pk).update(
            vat_validated=now - timedelta(days=VAT_VALIDITY_DAYS - 2, minutes=1),
            vat_validation_state=Customer.VatValidationState.VALID,
        )
        Customer.objects.filter(pk=recent.pk).update(
            vat_validated=now,
            vat_validation_state=Customer.VatValidationState.VALID,
        )

        with patch.object(
            Customer, "prepayment_validation", autospec=True
        ) as prepayment_validation:
            fetch_vat_info(delay=0)

        self.assertEqual(
            {call.args[0].pk for call in prepayment_validation.call_args_list},
            {never_validated.pk, due.pk},
        )
        self.assertTrue(
            all(
                call.kwargs == {"automated": True}
                for call in prepayment_validation.call_args_list
            )
        )

    def test_prefetch_vat_all(self) -> None:
        recent = self.create_vat_customer()

        with patch.object(
            Customer, "prepayment_validation", autospec=True
        ) as prepayment_validation:
            fetch_vat_info(fetch_all=True, delay=0)

        self.assertEqual(
            {call.args[0].pk for call in prepayment_validation.call_args_list},
            {recent.pk},
        )

    def test_prefetch_vat_transient_errors_retry_without_interaction(self) -> None:
        customer = self.create_vat_customer()
        original_validation = timezone.now() - timedelta(days=VAT_VALIDITY_DAYS)
        Customer.objects.filter(pk=customer.pk).update(
            vat_validated=original_validation,
            vat_validation_state=Customer.VatValidationState.VALID,
            vat_validation_error={},
        )
        error = ValidationError("Temporary VIES failure", code="other:Error: TIMEOUT")

        with patch(
            "weblate_web.payments.models.validate_vatin", side_effect=error
        ) as validate:
            for _attempt in range(2):
                fetch_vat_info(delay=0)

        customer.refresh_from_db()
        self.assertEqual(customer.vat_validated, original_validation)
        self.assertEqual(
            customer.vat_validation_state, Customer.VatValidationState.VALID
        )
        self.assertEqual(customer.vat_validation_error, {})
        self.assertEqual(customer.interaction_set.count(), 0)
        self.assertEqual(validate.call_count, 2)

    def test_prefetch_vat_invalid_errors_store_state_without_interaction(self) -> None:
        customer = self.create_vat_customer()
        Customer.objects.filter(pk=customer.pk).update(
            vat_validated=None,
            vat_validation_state=Customer.VatValidationState.UNKNOWN,
            vat_validation_error={},
        )
        error = ValidationError("Invalid VAT", code="Invalid VAT")

        with patch(
            "weblate_web.payments.models.validate_vatin", side_effect=error
        ) as validate:
            fetch_vat_info(delay=0)
            fetch_vat_info(delay=0)
            fetch_vat_info(fetch_all=True, delay=0)

        customer.refresh_from_db()
        self.assertEqual(
            customer.vat_validation_state, Customer.VatValidationState.INVALID
        )
        self.assertIsNotNone(customer.vat_validated)
        self.assertEqual(
            customer.vat_validation_error,
            {
                "vat": str(customer.vat),
                "code": "Invalid VAT",
                "message": "Invalid VAT",
            },
        )
        self.assertEqual(customer.interaction_set.count(), 0)
        self.assertEqual(validate.call_count, 1)

    def test_prefetch_vat_valid_to_invalid_creates_interaction(self) -> None:
        customer = self.create_vat_customer()
        Customer.objects.filter(pk=customer.pk).update(
            vat_validated=timezone.now() - timedelta(days=VAT_VALIDITY_DAYS),
            vat_validation_state=Customer.VatValidationState.VALID,
            vat_validation_error={},
        )

        with patch(
            "weblate_web.payments.models.validate_vatin",
            side_effect=ValidationError("Invalid VAT", code="Invalid VAT"),
        ):
            fetch_vat_info(delay=0)

        self.assertEqual(customer.interaction_set.count(), 1)
        interaction = customer.interaction_set.get()
        self.assertEqual(interaction.details["vat"], str(customer.vat))
        self.assertEqual(interaction.details["code"], "Invalid VAT")
        self.assertEqual(interaction.details["message"], "Invalid VAT")
        self.assertTrue(interaction.details["automated"])

    def test_manual_vat_validation_retries_recent_invalid_state(self) -> None:
        customer = self.create_vat_customer()
        Customer.objects.filter(pk=customer.pk).update(
            vat_validated=timezone.now(),
            vat_validation_state=Customer.VatValidationState.INVALID,
            vat_validation_error={
                "vat": str(customer.vat),
                "code": "Invalid VAT",
                "message": "Invalid VAT",
            },
        )
        customer.refresh_from_db()

        with patch("weblate_web.payments.models.validate_vatin") as validate:
            customer.prepayment_validation()

        self.assertEqual(validate.call_count, 1)
        self.assertEqual(validate.call_args.kwargs, {"force": True})
        customer.refresh_from_db()
        self.assertEqual(
            customer.vat_validation_state, Customer.VatValidationState.VALID
        )
        self.assertIsNotNone(customer.vat_validated)
        self.assertEqual(customer.vat_validation_error, {})

    @override_settings(PAYMENT_DEBUG=True)
    def test_reject(self) -> None:
        payment, url, _dummy = self.prepare_payment()
        response = self.client.post(url, {"method": "reject"})
        self.assertRedirects(
            response,
            f"/en/donate/process/?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.REJECTED)

        self.login()
        response = self.client.get(reverse("user-invoice", kwargs={"pk": payment.pk}))
        self.assertEqual(response.status_code, 404)

    @override_settings(PAYMENT_DEBUG=True)
    def test_pending(self) -> None:
        payment, url, _dummy = self.prepare_payment()
        response = self.client.post(url, {"method": "pending"})
        complete_url = reverse("payment-complete", kwargs={"pk": payment.pk})
        self.assertRedirects(
            response,
            f"https://cihar.com/?url=http://localhost:1234{complete_url}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.PENDING)
        response = self.client.get(complete_url)
        self.assertRedirects(
            response,
            f"/en/donate/process/?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.ACCEPTED)


class PaymentTest(FakturaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        fake_remote()

    def test_donate_page(self) -> None:
        response = self.client.get("/en/donate/")
        self.assertContains(response, "/donate/new/")
        self.login()

        # Check rewards on page
        response = self.client.get("/en/donate/new/")
        self.assertContains(response, "list of supporters")

    def test_manual_backend_not_offered(self) -> None:
        customer = Customer.objects.create(
            name="Test Customer",
            address="Test address",
            city="Test City",
            postcode="12345",
            country="CZ",
            origin=PAYMENTS_ORIGIN,
            user_id=-1,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
        )

        response = self.client.get(f"/en/payment/{payment.pk}/")

        self.assertContains(response, "Please choose payment method")
        self.assertNotContains(response, 'value="manual"')
        self.assertNotContains(response, "pay-manual")
        self.assertNotContains(response, "Manual payment")

    @override_settings(**THEPAY2_MOCK_SETTINGS, **SIGNATURE_MOCK_SETTINGS)
    @responses.activate
    def test_service_workflow_card(self) -> None:  # noqa: PLR0915
        self.login()
        thepay_mock_create_payment()
        Package.objects.create(name="community", verbose="Community support", price=0)
        Package.objects.create(name="extended", verbose="Extended support", price=42)
        response = self.client.get("/en/subscription/new/?plan=extended", follow=True)
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "thepay2-card"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://gate.thepay.cz/"))  # type: ignore[attr-defined]

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

        # Perform the payment
        thepay_mock_payment(payment.pk)

        # Back to our web
        response = self.client.get(payment.get_complete_url(), follow=True)
        self.assertRedirects(response, "/en/user/")
        self.assertContains(response, "Thank you for your subscription")
        self.assert_notifications(
            "Your payment on weblate.org", "Your new subscription on weblate.org"
        )

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PROCESSED)

        # Edit customer info
        customer = Customer.objects.get()
        self.assertEqual(customer.name, TEST_CUSTOMER["name"])
        edit_customer = TEST_CUSTOMER.copy()
        edit_customer["name"] = "Test Customer"
        response = self.client.post(
            reverse("edit-customer", kwargs={"pk": customer.pk}),
            edit_customer,
            follow=True,
        )
        self.assertRedirects(response, reverse("user"))
        customer.refresh_from_db()
        self.assertEqual(customer.name, edit_customer["name"])

        # Data processing agreements
        agreements_url = reverse("customer-agreement", kwargs={"pk": customer.pk})
        response = self.client.get(agreements_url)
        self.assertContains(response, "Weblate_Data_Processing_Agreement_Sample.pdf")
        self.assertContains(response, "Weblate_Privacy_Policy.pdf")

        # No agreement without consent
        response = self.client.post(agreements_url, follow=True)
        self.assertContains(response, "This field is required.")
        self.assertEqual(customer.agreement_set.count(), 0)

        # Create agreement
        response = self.client.post(agreements_url, {"consent": 1}, follow=True)
        self.assertRedirects(response, agreements_url)
        self.assertEqual(customer.agreement_set.count(), 1)

        # No agreement on second attempt
        response = self.client.post(agreements_url, {"consent": 1}, follow=True)
        self.assertRedirects(response, agreements_url)
        self.assertEqual(customer.agreement_set.count(), 1)

        # Download agreement
        agreement = customer.agreement_set.get()
        response = self.client.get(
            reverse("agreement-download", kwargs={"pk": agreement.pk})
        )
        self.assertEqual(response.headers["Content-Type"], "application/pdf")

        # Service should not get notifications on expiry now
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()

        # Move expiry into past and renew
        thepay_mock_repeated_payment()
        subscription = Subscription.objects.all().get()
        subscription.expires -= timedelta(days=365)
        subscription.save(update_fields=["expires"])
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your payment on weblate.org")

        # Disable recurring payments
        response = self.client.post(
            reverse("subscription-disable", kwargs={"pk": subscription.pk})
        )
        self.assertRedirects(response, reverse("user"))

        # Ensure no payment is made
        subscription = Subscription.objects.all().get()
        subscription.expires -= timedelta(days=365)
        subscription.save(update_fields=["expires"])
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_donation_workflow_invalid_reward(self) -> None:
        self.login()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 10, "reward": 2},
            follow=True,
        )
        self.assertContains(response, "Insufficient donation for selected reward!")

    def test_donation_reward_packages(self) -> None:
        for reward in range(4):
            package = get_donation_package(reward)
            self.assertEqual(package.name, get_donation_reward_package_name(reward))
            self.assertEqual(package.price, REWARD_LEVELS[reward])
            self.assertEqual(package.category, PackageCategory.PACKAGE_DONATION)
            self.assertEqual(package.donation_reward, reward)

        self.assertEqual(
            get_donation_package(0).donation_payment_description,
            "Weblate donation",
        )
        self.assertEqual(
            get_donation_package(3).donation_payment_description,
            "Weblate donation: Logo and link on the Weblate website",
        )
        package = Package.objects.create(
            name="test",
            verbose="Test package",
            price=42,
        )
        with self.assertRaises(ValueError):
            _description = package.donation_payment_description

    def test_donation_reward_edit_forms_match_package_reward(self) -> None:
        self.login()
        expected_fields = {
            1: ("donation_link_text",),
            2: ("donation_link_text", "donation_link_url"),
            3: ("donation_link_text", "donation_link_url", "donation_link_image"),
        }
        for reward, fields in expected_fields.items():
            donation = self.create_donation(reward=reward)
            with override("en"):
                response = self.client.get(
                    reverse("donate-edit", kwargs={"pk": donation.pk})
                )
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            for field in fields:
                self.assertIn(f'name="{field}"', content)
            for field in {"donation_link_url", "donation_link_image"} - set(fields):
                self.assertNotIn(f'name="{field}"', content)

        donation = self.create_donation(reward=0)
        with override("en"):
            response = self.client.get(
                reverse("donate-edit", kwargs={"pk": donation.pk})
            )
        self.assertEqual(response.status_code, 404)

    def test_donation_workflow_card_reward(self) -> None:
        self.test_donation_workflow_card(2)

    @override_settings(**THEPAY2_MOCK_SETTINGS)
    @responses.activate
    def test_donation_workflow_card(self, reward=0) -> None:  # noqa: PLR0915
        self.login()
        thepay_mock_create_payment()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 1000, "reward": reward},
            follow=True,
        )
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.amount, 1000)
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "thepay2-card"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://gate.thepay.cz/"))  # type: ignore[attr-defined]

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

        # Perform the payment
        thepay_mock_payment(payment.pk)

        # Back to our web
        response = self.client.get(payment.get_complete_url(), follow=True)
        donation = Service.objects.filter(kind=ServiceKind.DONATION).get()
        subscription = cast("Subscription", donation.donation_subscription)
        self.assertEqual(
            subscription.package.name,
            get_donation_reward_package_name(reward),
        )
        redirect_url = f"/en/donate/edit/{donation.pk}/" if reward else "/en/user/"
        self.assertRedirects(response, redirect_url)
        self.assertContains(response, "Thank you for your donation")

        payment.refresh_from_db()
        self.assertEqual(payment.paid_invoice.total_amount, 1000)  # type: ignore[union-attr]
        self.assertEqual(payment.state, Payment.PROCESSED)

        # Manual renew
        response = self.client.post(
            reverse("donate-pay", kwargs={"pk": donation.pk}), follow=True
        )
        renew = Payment.objects.exclude(pk=payment.pk).get()
        self.assertEqual(renew.state, Payment.NEW)
        self.assertContains(response, "Please choose payment method")

        response = self.client.post(
            reverse("payment", kwargs={"pk": renew.uuid}), {"method": "thepay2-card"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://gate.thepay.cz/"))  # type: ignore[attr-defined]

        renew.refresh_from_db()
        self.assertEqual(renew.state, Payment.PENDING)

        # Perform the payment
        thepay_mock_payment(renew.pk)

        # Back to our web
        response = self.client.get(renew.get_complete_url(), follow=True)
        self.assertRedirects(response, redirect_url)
        self.assertContains(response, "Thank you for your donation")
        self.assert_notifications(
            "Your payment on weblate.org", "Your payment on weblate.org"
        )

        renew.refresh_from_db()
        self.assertEqual(renew.state, Payment.PROCESSED)
        self.assertEqual(payment.paid_invoice.total_amount, 1000)  # type: ignore[union-attr]
        subscription = cast("Subscription", donation.donation_subscription)
        self.assertEqual(
            subscription.package.name,
            get_donation_reward_package_name(reward),
        )

        # Service should not get notifications on expiry now
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()

        # Move expiry into past and renew
        thepay_mock_repeated_payment()
        donation = Service.objects.filter(kind=ServiceKind.DONATION).get()
        renewal_subscription = donation.donation_subscription
        self.assertIsNotNone(renewal_subscription)
        renewal_subscription = cast("Subscription", renewal_subscription)
        renewal_subscription.expires -= timedelta(days=365 * 2)
        renewal_subscription.save(update_fields=["expires"])
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your payment on weblate.org")

        # Disable recurring payments
        response = self.client.post(
            reverse("donate-disable", kwargs={"pk": donation.pk})
        )
        self.assertRedirects(response, reverse("user"))

        # Ensure no payment is made
        donation = Service.objects.filter(kind=ServiceKind.DONATION).get()
        expired_subscription = donation.donation_subscription
        self.assertIsNotNone(expired_subscription)
        expired_subscription = cast("Subscription", expired_subscription)
        expired_subscription.expires -= timedelta(days=365)
        expired_subscription.save(update_fields=["expires"])
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_donation_workflow_bank(self) -> None:
        self.login()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 10, "reward": 0},
            follow=True,
        )
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "fio-bank"}, follow=True)
        self.assertContains(response, "Payment Instructions")
        # Verify that second submission doesn't break anything
        response = self.client.post(payment_url, {"method": "fio-bank"}, follow=True)
        self.assertContains(response, "Payment Instructions")

        # Verify that get will also display payment instructions
        response = self.client.get(payment_url, follow=True)
        self.assertContains(response, "Payment Instructions")

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

    def test_your_donations(self) -> None:
        with override("en"):
            # Check login link
            self.assertContains(self.client.get(reverse("donate")), "user-anonymous")
            user = self.login()

            # No login/donations
            response = self.client.get(reverse("donate"))
            self.assertNotContains(response, "user-anonymous")
            self.assertNotContains(response, "Your donations")

            customer = Customer.objects.create(
                user_id=-1,
                origin=PAYMENTS_ORIGIN,
            )
            customer.users.add(user)
            # Donation show show up
            service = Service.objects.create(
                customer=customer,
                kind=ServiceKind.DONATION,
            )
            service.subscription_set.create(
                package=get_donation_package(2),
                expires=timezone.now() + relativedelta(years=1),
                payment=create_payment(user=user, customer=customer)[0],
            )
            self.assertContains(self.client.get(reverse("user")), "My donations")

    def test_user_view_keeps_services_and_donations_separate(self) -> None:
        self.login()
        service = self.create_service()
        donation = self.create_donation()

        with override("en"):
            response = self.client.get(reverse("user"))

        self.assertEqual(
            {item.pk for item in response.context["user_services"]},
            {service.pk},
        )
        self.assertEqual(
            {item.pk for item in response.context["user_donations"]},
            {donation.pk},
        )

    def test_active_customers_exclude_donation_only_customers(self) -> None:
        service = self.create_service()
        donation = self.create_donation()

        self.assertEqual(
            set(Customer.objects.active().values_list("pk", flat=True)),
            {service.customer_id},
        )
        self.assertNotEqual(service.customer_id, donation.customer_id)

    def test_user_view_renders_active_donation_without_payment(self) -> None:
        self.login()
        donation = self.create_donation()
        subscription = cast("Subscription", donation.donation_subscription)
        subscription.payment = None
        subscription.save(update_fields=["payment"])

        with override("en"):
            response = self.client.get(reverse("user"))

        self.assertContains(response, "My donations")
        self.assertContains(response, "No renewal")

    def test_donor_listing_only_shows_active_logo_rewards(self) -> None:
        logo_donation = self.create_donation(reward=3)
        logo_donation.donation_link_text = "Logo donor"
        logo_donation.save(update_fields=["donation_link_text"])
        link_donation = self.create_donation(reward=2)
        link_donation.donation_link_text = "Link donor"
        link_donation.save(update_fields=["donation_link_text"])
        expired_donation = self.create_donation(years=0, days=-1, reward=3)
        expired_donation.donation_link_text = "Expired logo donor"
        expired_donation.save(update_fields=["donation_link_text"])

        response = self.client.get(reverse("donate"))

        self.assertContains(response, "Logo donor")
        self.assertNotContains(response, "Link donor")
        self.assertNotContains(response, "Expired logo donor")

    def test_link(self) -> None:
        self.create_donation()
        response = self.client.get("/en/thanks/", follow=True)
        self.assertContains(response, "https://example.com/weblate")
        self.assertContains(response, "Weblate donation test")

    @responses.activate
    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring(self) -> None:
        donation = self.create_donation(years=0)
        subscription = cast("Subscription", donation.donation_subscription)
        payment = cast("Payment", subscription.payment_obj)
        self.assertIsNotNone(payment)
        # No recurring payments for now
        self.assertEqual(payment.payment_set.count(), 0)

        # Trigger payment and process it
        call_command("recurring_payments")

        # There should be additional payment
        self.assertEqual(payment.payment_set.count(), 1)
        # Verify it is processed
        self.assertEqual(payment.payment_set.get().state, Payment.PROCESSED)

        # Verify expiry has been moved
        old = subscription.expires
        subscription.refresh_from_db()
        self.assertGreater(subscription.expires, old)

        # Process pending payments (should do nothing)
        call_command("process_payments")

    @override_settings(**THEPAY2_MOCK_SETTINGS)
    @responses.activate
    def test_fosdem_donation(self) -> None:
        thepay_mock_create_payment()

        response = self.client.get("/fosdem/donate/", follow=True)
        self.assertContains(response, "Please provide your name")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.amount, 30)
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(
            customer_url, {"name": "FOSDEM Visitor", "country": "BE"}, follow=True
        )
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "thepay2-card"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://gate.thepay.cz/"))  # type: ignore[attr-defined]

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

        # Perform the payment
        thepay_mock_payment(payment.pk)

        # Back to our web
        response = self.client.get(payment.get_complete_url())
        # This redirects to an article we don't have in tests
        self.assertRedirects(response, FOSDEM_ORIGIN, fetch_redirect_response=False)

        # Fetch any page to verif that message is shown
        response = self.client.get("/about/", follow=False)
        self.assertRedirects(response, "/en/about/", fetch_redirect_response=False)
        response = self.client.get("/en/about/")
        self.assertContains(response, "Thank you for your donation and enjoy FOSDEM.")

        # Verify payment info
        payment.refresh_from_db()
        self.assertEqual(payment.paid_invoice.total_amount, 30)  # type: ignore[union-attr]
        self.assertEqual(payment.state, Payment.PROCESSED)


class PostTest(PostTestCase):
    def setUp(self) -> None:
        super().setUp()
        fake_remote()

    def test_future(self) -> None:
        past = self.create_post()
        future = self.create_post(
            "futurepost", "futurebody", timezone.now() + timedelta(days=1)
        )
        response = self.client.get("/feed/")
        self.assertContains(response, "testpost")
        self.assertNotContains(response, "futurepost")
        response = self.client.get("/news/", follow=True)
        self.assertContains(response, "testpost")
        self.assertNotContains(response, "futurepost")
        response = self.client.get(past.get_absolute_url(), follow=True)
        self.assertContains(response, "testpost")
        self.assertContains(response, "testbody")
        response = self.client.get(future.get_absolute_url(), follow=True)
        self.assertEqual(response.status_code, 404)

    def test_detail_json_ld(self) -> None:
        author = User.objects.create_user(username="schema-author", last_name="Author")
        post = Post.objects.create(
            title="Schema post",
            slug="schema-post",
            body="Post body",
            timestamp=timezone.now() - timedelta(days=1),
            author=author,
            topic="release",
            summary="Post summary",
        )

        response = self.client.get(post.get_absolute_url(), follow=True)
        schema = get_json_ld_by_type(response, "BlogPosting")

        self.assertEqual(response.context["object"], post)
        self.assertIsInstance(response.context["view"], PostView)
        self.assertEqual(schema["headline"], post.title)
        self.assertEqual(schema["description"], post.summary)
        self.assertEqual(schema["url"], "https://weblate.org/news/archive/schema-post/")
        self.assertEqual(
            schema["mainEntityOfPage"],
            {
                "@type": "WebPage",
                "@id": "https://weblate.org/news/archive/schema-post/",
            },
        )
        self.assertEqual(schema["author"], {"@type": "Person", "name": "Author"})
        self.assertEqual(
            schema["publisher"], {"@id": "https://weblate.org/#organization"}
        )
        self.assertEqual(schema["isPartOf"], {"@id": "https://weblate.org/#website"})
        self.assertEqual(schema["articleSection"], "Release")
        self.assertEqual(schema["datePublished"], schema["dateModified"])
        self.assertEqual(schema["inLanguage"], "en")

    def test_detail_json_ld_escapes_script_content(self) -> None:
        payload = "</script><img src=x onerror=prompt(document.domain)>"
        post = Post.objects.create(
            title=payload,
            slug="xss-schema",
            body="safe",
            timestamp=timezone.now() - timedelta(days=1),
            summary=payload,
        )

        response = self.client.get(post.get_absolute_url(), follow=True)
        schema = get_json_ld_by_type(response, "BlogPosting")

        self.assertNotContains(response, payload)
        self.assertNotContains(response, "<img src=x")
        self.assertEqual(schema["headline"], payload)
        self.assertEqual(schema["description"], payload)

    def test_archive_escapes_title_and_summary(self) -> None:
        payload = '"><img src=x onerror=prompt(document.domain)>'
        post = Post.objects.create(
            title=payload, slug="xss-title", body="safe", timestamp=timezone.now()
        )
        post.summary = payload
        post.save(update_fields=["summary"])

        response = self.client.get("/news/", follow=True)

        self.assertNotContains(response, "<img src=x onerror=prompt(document.domain)>")
        self.assertContains(
            response, "&lt;img src=x onerror=prompt(document.domain)&gt;"
        )

    def test_body_strips_raw_html(self) -> None:
        payload = '"><img src=x onerror=prompt(document.domain)>'
        post = self.create_post(title="xss-post", body=payload)

        response = self.client.get(post.get_absolute_url(), follow=True)

        self.assertNotContains(response, "src=x")
        self.assertNotContains(response, "onerror")
        self.assertContains(response, "&quot;&gt;")

    def test_body_escapes_unsafe_link(self) -> None:
        post = self.create_post(title="xss-link", body="[link](javascript:alert(1))")

        response = self.client.get(post.get_absolute_url(), follow=True)

        self.assertNotContains(response, 'href="javascript:alert(1)"')
        self.assertContains(response, "[link](javascript:alert(1))")

    def test_body_plain_autolink_boundaries(self) -> None:
        post = self.create_post(
            title="plain-autolink",
            body="Links: https://example.com). and https://example.com/",
        )

        self.assertIn(
            '<a href="https://example.com">https://example.com</a>).',
            post.body_rendered,
        )
        self.assertIn(
            '<a href="https://example.com/">https://example.com/</a>',
            post.body_rendered,
        )

    def test_body_escapes_image_url(self) -> None:
        post = self.create_post(
            title="xss-image",
            body='![logo](<https://example.com/" onerror="alert(1)>)',
        )

        self.assertNotIn('src="https://example.com/" onerror=', post.body_rendered)
        self.assertNotIn(' onerror="alert(1)"', post.body_rendered)
        self.assertIn('src="https://example.com/%22%20onerror=', post.body_rendered)

    def test_body_escapes_image_alt(self) -> None:
        post = self.create_post(
            title="xss-image-alt",
            body='![" onerror="alert(1)](https://example.com/logo.png)',
        )

        self.assertNotIn('alt="" onerror="alert(1)"', post.body_rendered)
        self.assertNotIn(' onerror="alert(1)"', post.body_rendered)
        self.assertIn('alt="&quot; onerror=&quot;alert(1)"', post.body_rendered)

    def test_feed_uses_sanitized_body(self) -> None:
        self.create_post(
            title="xss-feed",
            body='"><img src=x onerror=prompt(document.domain)>',
        )

        response = self.client.get("/feed/")

        self.assertNotContains(response, "<img")
        self.assertNotContains(response, "onerror")


class APITest(UserTestCase):
    def test_hosted(self) -> None:
        Package.objects.create(name="community", verbose="Community support", price=0)
        Package.objects.create(name="shared:test", verbose="Test package", price=0)
        response = self.client.post(
            "/api/hosted/",
            {
                "payload": dumps(
                    {
                        "billing": 42,
                        "package": "shared:test",
                        "projects": 1,
                        "languages": 1,
                        "source_strings": 1,
                        "words": 10,
                        "components": 1,
                        "users": [666],
                    },
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.hosted",
                )
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)

    def test_hosted_links_payments_idempotently(self) -> None:
        Package.objects.create(name="community", verbose="Community support", price=0)
        package = Package.objects.create(
            name="shared:test", verbose="Test package", price=42
        )
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
            name=TEST_CUSTOMER["name"],
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country=TEST_CUSTOMER["country"],
        )
        initial_payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Initial hosted payment",
            extra={"billing": 42},
            end=date(2025, 1, 1),
        )
        payload = {
            "billing": 42,
            "package": "shared:test",
            "projects": 1,
            "languages": 1,
            "source_strings": 1,
            "words": 10,
            "components": 1,
            "users": ["hosted-user"],
        }

        response = self.client.post(
            "/api/hosted/",
            {
                "payload": dumps(
                    payload,
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.hosted",
                )
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)

        current_payment = Payment.objects.create(
            customer=customer,
            amount=200,
            description="Current hosted payment",
            extra={"billing": 42},
            end=date(2026, 1, 1),
        )

        for _unused in range(2):
            response = self.client.post(
                "/api/hosted/",
                {
                    "payload": dumps(
                        payload,
                        key=settings.PAYMENT_SECRET,
                        salt="weblate.hosted",
                    )
                },
                headers={"user-agent": "Weblate/1.2.3"},
            )
            self.assertEqual(response.status_code, 200)

        subscription = Service.objects.get(hosted_billing=42).subscription_set.get()
        self.assertEqual(subscription.package, package)
        self.assertEqual(subscription.payment, current_payment)
        self.assertEqual(subscription.expires.date(), current_payment.end)
        self.assertEqual(
            set(subscription.past_payments.values_list("pk", flat=True)),
            {initial_payment.pk},
        )

    def test_hosted_invalid(self) -> None:
        response = self.client.post("/api/hosted/", {"payload": dumps({}, key="dummy")})
        self.assertEqual(response.status_code, 400)

    def test_hosted_missing(self) -> None:
        response = self.client.post("/api/hosted/")
        self.assertEqual(response.status_code, 400)

    def test_support_missing(self) -> None:
        response = self.client.post("/api/support/")
        self.assertEqual(response.status_code, 404)

    def perform_support(self, *, delta: int = 1, expected: str = "extended"):
        Package.objects.create(name="community", verbose="Community support", price=0)
        extended = Package.objects.create(
            name="extended", verbose="Extended support", price=42
        )
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
        )
        service = Service.objects.create(customer=customer)
        service.subscription_set.create(
            package=extended, expires=timezone.now() + timedelta(days=delta)
        )
        response = self.client.post(
            "/api/support/",
            {"secret": service.secret},
            headers={"user-agent": "Mozilla/1.2.3"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/support/",
            {"secret": service.secret},
            headers={"user-agent": "Weblate/non-version"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/support/",
            {"secret": service.secret},
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], expected)
        if expected == "community":
            self.assertEqual(payload["package"], "")
        elif expected == "extended":
            self.assertEqual(payload["package"], "Extended support")
        else:
            raise ValueError("Missing package expectation!")
        return service

    def test_support(self) -> None:
        self.perform_support()

    def test_support_expired(self) -> None:
        self.perform_support(delta=-1, expected="community")

    def test_support_discovery(self) -> None:
        service = self.perform_support()
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)
        self.client.post(
            "/api/support/",
            {"secret": service.secret, "discoverable": "1"},
            headers={"user-agent": "Weblate/1.2.3"},
        )
        service = Service.objects.get(pk=service.pk)
        self.assertTrue(service.discoverable)
        self.client.post(
            "/api/support/",
            {"secret": service.secret},
            headers={"user-agent": "Weblate/1.2.3"},
        )
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)

    def test_support_discovery_projects(self) -> None:
        service = self.perform_support()
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)

        # Enable discovery
        self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "discoverable": "1",
                "public_projects": json.dumps(
                    [
                        {
                            "name": "Prj1",
                            "url": "/projects/p/",
                            "web": "https://weblate.org/",
                        }
                    ]
                ),
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(service.project_set.count(), 1)
        project = service.project_set.get()
        self.assertEqual(project.name, "Prj1")

        # Project name change
        self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "discoverable": "1",
                "public_projects": json.dumps(
                    [
                        {
                            "name": "Prj2",
                            "url": "/projects/p/",
                            "web": "https://weblate.org/",
                        }
                    ]
                ),
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(service.project_set.count(), 1)
        project = service.project_set.get()
        self.assertEqual(project.name, "Prj2")

        # Invalid listing of projects
        self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "discoverable": "1",
                "public_projects": json.dumps(
                    [
                        {
                            "name": "Prj3",
                            "url": "/projects/p3/",
                        }
                    ]
                ),
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(service.project_set.count(), 0)

    def test_support_site_url_lock(self) -> None:
        service = self.perform_support()
        service.site_url = "https://allowed.example.com"
        service.site_url_lock = True
        service.save(update_fields=["site_url", "site_url_lock"])

        response = self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "site_url": "https://wrong.example.com",
                "discoverable": "1",
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)
        self.assertEqual(service.site_url, "https://allowed.example.com")

        response = self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "site_url": "https://allowed.example.com",
                "discoverable": "1",
            },
            headers={"user-agent": "Weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)
        service = Service.objects.get(pk=service.pk)
        self.assertTrue(service.discoverable)

    def test_user(self) -> None:
        user = self.create_user()
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {"username": user.username},
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "User updated"})
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {
                        "username": "x",
                        "create": {
                            "username": "x",
                            "last_name": "First Last",
                            "email": "noreply@weblate.org",
                        },
                    },
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "User created"})

    def test_user_invalid(self) -> None:
        response = self.client.post("/api/user/", {"payload": dumps({}, key="dummy")})
        self.assertEqual(response.status_code, 400)

    def test_user_missing(self) -> None:
        response = self.client.post("/api/user/")
        self.assertEqual(response.status_code, 400)

    def test_user_rename(self) -> None:
        user = self.create_user()
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {"username": user.username, "changes": {"username": "other"}},
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="testuser").exists())
        self.assertTrue(User.objects.filter(username="other").exists())

    def test_fetch_vat_denied(self) -> None:
        response = self.client.post(reverse("js-vat"))
        self.assertEqual(response.status_code, 302)

    @responses.activate
    def test_fetch_vat(self) -> None:
        self.login()
        mock_vies()
        cnb_mock_rates()
        response = self.client.post(reverse("js-vat"))
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse("js-vat"), {"vat": "CZ8003283018"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "countryCode": "CZ",
                "vatNumber": "8003280318",
                "requestDate": "2024-07-09",
                "valid": True,
                "name": "Ing. Michal Čihař",
                "address": "Nábřežní 694\nCVIKOV II\n471 54  CVIKOV",
            },
        )


@override_settings(
    NOTIFY_SUBSCRIPTION=["noreply@example.com"],
    PAYMENT_DEBUG=True,
)
class ExpiryTest(FakturaceTestCase):
    def test_customer_extra_upcoming_notification_days_validation(self) -> None:
        customer = Customer(
            user_id=-1,
            name="TEST CUSTOMER",
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            country="US",
            origin="https://example.com/customer",
            upcoming_payment_notification_days=366,
        )
        with self.assertRaises(ValidationError) as exc:
            customer.full_clean()
        self.assertEqual(
            list(exc.exception.message_dict),
            ["upcoming_payment_notification_days"],
        )

    def test_customer_extra_upcoming_notification_days_conflicts_with_default(
        self,
    ) -> None:
        service = self.create_service()
        customer = service.customer
        customer.upcoming_payment_notification_days = 31
        with self.assertRaises(ValidationError) as exc:
            customer.full_clean()
        self.assertEqual(
            list(exc.exception.message_dict),
            ["upcoming_payment_notification_days"],
        )

    def test_customer_extra_upcoming_notification_days_allows_31_for_monthly_only(
        self,
    ) -> None:
        service = self.create_service(
            years=0, days=45, recurring="", package="test:test-1-m"
        )
        customer = service.customer
        customer.upcoming_payment_notification_days = 31
        customer.full_clean()

    def _create_invoice_case(
        self,
        customer: Customer,
        *,
        kind: InvoiceKind,
        description: str,
        issue_days: int = 0,
        payment_states: tuple[int, ...] = (),
    ) -> tuple[Invoice, list[str]]:
        invoice = Invoice.objects.create(
            customer=customer,
            kind=kind,
            category=InvoiceCategory.HOSTING,
            vat_rate=21,
            issue_date=timezone.now().date() + timedelta(days=issue_days),
        )
        invoice.invoiceitem_set.create(description=description, unit_price=100)

        relation = "draft_invoice" if kind == InvoiceKind.PROFORMA else "paid_invoice"
        payment_urls = []
        for index, state in enumerate(payment_states):
            payment = Payment.objects.create(
                customer=customer,
                amount=121,
                amount_fixed=True,
                description=f"{description} payment {index}",
                backend="pay",
                state=state,
                **{relation: invoice},
            )
            payment_urls.append(payment.get_payment_url())

        return invoice, payment_urls

    def _prepare_upcoming_invoice_cases(  # noqa: PLR0914
        self, customer: Customer
    ) -> UpcomingInvoiceCases:
        proforma, proforma_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.PROFORMA,
            description="Pending pro forma",
            payment_states=(Payment.NEW, Payment.PENDING),
        )
        invoice, invoice_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.INVOICE,
            description="Pending invoice",
            payment_states=(Payment.PENDING, Payment.NEW),
        )
        old_invoice, _ = self._create_invoice_case(
            customer,
            kind=InvoiceKind.INVOICE,
            description="Old invoice",
            issue_days=-40,
        )
        old_proforma, _ = self._create_invoice_case(
            customer,
            kind=InvoiceKind.PROFORMA,
            description="Old pro forma",
            issue_days=-40,
        )
        rejected_proforma, rejected_proforma_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.PROFORMA,
            description="Rejected pro forma",
            payment_states=(Payment.REJECTED,),
        )
        rejected_invoice, rejected_invoice_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.INVOICE,
            description="Rejected invoice",
            payment_states=(Payment.REJECTED,),
        )
        recovered_proforma, recovered_proforma_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.PROFORMA,
            description="Recovered pro forma",
            payment_states=(Payment.REJECTED, Payment.PROCESSED),
        )
        recovered_invoice, recovered_invoice_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.INVOICE,
            description="Recovered invoice",
            payment_states=(Payment.REJECTED, Payment.PROCESSED),
        )
        paid_invoice, paid_invoice_urls = self._create_invoice_case(
            customer,
            kind=InvoiceKind.INVOICE,
            description="Paid invoice",
            payment_states=(Payment.PROCESSED,),
        )
        return UpcomingInvoiceCases(
            proforma=proforma,
            proforma_urls=proforma_urls,
            invoice=invoice,
            invoice_urls=invoice_urls,
            old_invoice=old_invoice,
            old_proforma=old_proforma,
            rejected_proforma=rejected_proforma,
            rejected_proforma_urls=rejected_proforma_urls,
            rejected_invoice=rejected_invoice,
            rejected_invoice_urls=rejected_invoice_urls,
            recovered_proforma=recovered_proforma,
            recovered_proforma_urls=recovered_proforma_urls,
            recovered_invoice=recovered_invoice,
            recovered_invoice_urls=recovered_invoice_urls,
            paid_invoice=paid_invoice,
            paid_invoice_urls=paid_invoice_urls,
        )

    def test_expiring_donate(self) -> None:
        donation = self.create_donation(years=0, days=-2, recurring="")
        subscription = cast("Subscription", donation.donation_subscription)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications(
            "Your expired payment on weblate.org",
            "Your expired payment on weblate.org",
        )
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()

    def test_expiring_donate_full_command_keeps_summary(self) -> None:
        donation = self.create_donation(years=0, recurring="")
        subscription = cast("Subscription", donation.donation_subscription)
        timestamp = timezone.now().replace(day=1)
        subscription.expires = timestamp - timedelta(days=2)
        subscription.save(update_fields=["expires"])

        with patch(
            "weblate_web.management.commands.recurring_payments.timezone.now",
            return_value=timestamp,
        ):
            call_command("recurring_payments")

        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your expired payment on weblate.org",
            "Your expired payment on weblate.org",
        )
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)

    def test_expiring_recurring_donate(self) -> None:
        self.create_donation(years=0, days=-2)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your payment on weblate.org")
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications()

    def test_expiring_donate_notify_user(self) -> None:
        self.create_donation(years=0, days=7, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        mails = self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )
        self.assertEqual("Your upcoming payment on weblate.org", mails[0].subject)
        self.assertIn("€100", cast("str", mails[0].alternatives[0][0]))
        self.assertIn("€100", mails[0].body)
        self.assertIn("Your donation on weblate.org should renew soon", mails[0].body)
        self.assertNotIn("Your subscription on weblate.org", mails[0].body)

    def test_expiring_recurring_donate_notify_user(self) -> None:
        self.create_donation(years=0, days=7)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        mails = self.assert_notifications(
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )
        self.assertIn("€100", cast("str", mails[0].alternatives[0][0]))
        self.assertIn("€100", mails[0].body)

    def test_expiring_donate_notify_user_extra_customer_notification(self) -> None:
        donation = self.create_donation(years=0, days=14, recurring="")
        donation.customer.upcoming_payment_notification_days = 14
        donation.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_donate_notify_user_extra_customer_notification_before_default(
        self,
    ) -> None:
        donation = self.create_donation(years=0, days=45, recurring="")
        donation.customer.upcoming_payment_notification_days = 45
        donation.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_donate_notify_user_extra_customer_notification_disabled(
        self,
    ) -> None:
        donation = self.create_donation(years=0, days=14, recurring="")
        donation.customer.upcoming_payment_notification_days = 0
        donation.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")

    def test_expired_donate_notify_user_monthly_after_two_months(self) -> None:
        donation = self.create_donation(years=0, days=-62, recurring="")
        subscription = cast("Subscription", donation.donation_subscription)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        mails = self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )
        self.assertIn("Your donation on weblate.org is expired", mails[0].body)
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()

    def test_expired_donate_notify_user_not_weekly_after_two_months(self) -> None:
        self.create_donation(years=0, days=-63, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")

    def test_expired_donate_cleanup_after_grace_period(self) -> None:
        donation = self.create_donation(years=0, days=-11, recurring="")
        subscription = cast("Subscription", donation.donation_subscription)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()

    def test_expiring_subscription(self) -> None:
        self.create_service(years=0, days=-2, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications(
            "Your expired payment on weblate.org", "Your expired payment on weblate.org"
        )

    def test_expiring_recurring_subscription(self) -> None:
        self.create_service(years=0, days=-2)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications()
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your payment on weblate.org")
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications()

    def test_expiring_subscription_notify_user(self) -> None:
        self.create_service(years=0, days=7, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        mails = self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )
        self.assertIn(
            "Your subscription on weblate.org should renew soon", mails[0].body
        )
        self.assertNotIn("Your donation on weblate.org", mails[0].body)

    def test_subscription_payment_delete_is_restricted(self) -> None:
        service = self.create_service(years=0, days=-7)
        subscription = service.subscription_set.get()
        self.assertIsNotNone(subscription.payment)
        payment = cast("Payment", subscription.payment)
        with self.assertRaises(RestrictedError):
            payment.delete()
        subscription.refresh_from_db()
        self.assertEqual(subscription.payment, payment)

    def test_expired_subscription_notify_user_monthly_after_two_months(self) -> None:
        self.create_service(years=0, days=-62, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your expired payment on weblate.org",
            "Your expired payment on weblate.org",
        )

    def test_expired_subscription_notify_user_not_weekly_after_two_months(self) -> None:
        self.create_service(years=0, days=-63, recurring="")
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")

    def test_expiring_recurring_subscription_notify_user(self) -> None:
        self.create_service(years=0, days=7)
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_subscription_notify_user_extra_customer_notification(
        self,
    ) -> None:
        service = self.create_service(years=0, days=14, recurring="")
        service.customer.upcoming_payment_notification_days = 14
        service.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_subscription_notify_user_extra_customer_notification_before_default(
        self,
    ) -> None:
        service = self.create_service(years=0, days=45, recurring="")
        service.customer.upcoming_payment_notification_days = 45
        service.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_monthly_subscription_notify_user_31_days_before(self) -> None:
        service = self.create_service(
            years=0, days=31, recurring="", package="test:test-1-m"
        )
        service.customer.upcoming_payment_notification_days = 31
        service.customer.full_clean()
        service.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )

    def test_expiring_subscription_notify_user_extra_customer_notification_disabled(
        self,
    ) -> None:
        service = self.create_service(years=0, days=14, recurring="")
        service.customer.upcoming_payment_notification_days = 0
        service.customer.save(update_fields=["upcoming_payment_notification_days"])
        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        self.assert_notifications("Expiring subscriptions on weblate.org")

    def test_expiring_subscription_notify_user_lists_unpaid_invoices_and_recent_proformas(
        self,
    ) -> None:
        service = self.create_service(years=0, days=7)
        cases = self._prepare_upcoming_invoice_cases(service.customer)

        RecurringPaymentsCommand.notify_expiry(force_summary=True)
        mails = self.assert_notifications(
            "Your upcoming payment on weblate.org",
            "Your upcoming payment on weblate.org",
        )
        user_mail = next(
            mail
            for mail in mails
            if mail.subject == "Your upcoming payment on weblate.org"
        )
        html_body = cast("str", user_mail.alternatives[0][0])
        self.assertIn("There might already be an invoice for this renewal", html_body)
        self.assertIn(cases.proforma.number, html_body)
        self.assertIn(cases.invoice.number, html_body)
        self.assertIn(cases.old_invoice.number, html_body)
        self.assertIn(cases.rejected_proforma.number, html_body)
        self.assertIn(cases.rejected_invoice.number, html_body)
        self.assertNotIn(cases.old_proforma.number, html_body)
        self.assertNotIn(cases.paid_invoice.number, html_body)
        self.assertNotIn(cases.recovered_proforma.number, html_body)
        self.assertNotIn(cases.recovered_invoice.number, html_body)
        self.assertEqual(html_body.count(cases.proforma.number), 1)
        self.assertEqual(html_body.count(cases.invoice.number), 1)
        self.assertIn(cases.proforma_urls[1], html_body)
        self.assertIn(cases.invoice_urls[1], html_body)
        self.assertIn(cases.rejected_proforma_urls[0], html_body)
        self.assertIn(cases.rejected_invoice_urls[0], html_body)
        self.assertNotIn(cases.recovered_proforma_urls[1], html_body)
        self.assertNotIn(cases.recovered_invoice_urls[1], html_body)
        self.assertNotIn(cases.proforma_urls[0], html_body)
        self.assertNotIn(cases.invoice_urls[0], html_body)


@override_settings(
    NOTIFY_SUBSCRIPTION=["noreply@example.com"],
    PAYMENT_DEBUG=True,
)
class ServiceTest(FakturaceTestCase):
    @responses.activate
    def test_upcoming_payment(self) -> None:
        service = self.create_service(
            years=0, days=3, recurring="", package="test:test-1-m"
        )
        subscription = service.subscription_set.all().get()
        self.assertEqual(subscription.get_expected_payment_amount(), 42)

        discount = Discount.objects.create(percents=50)
        service.customer.discount = discount
        service.customer.save()
        self.assertEqual(subscription.get_expected_payment_amount(), 21)

        discount.percents = 10
        discount.save()
        self.assertEqual(subscription.get_expected_payment_amount(), 37)

    def test_recurring_donation_payment_uses_previous_amount_and_donation_extra(
        self,
    ) -> None:
        donation = self.create_donation(years=0, days=-2, reward=3)
        subscription = cast("Subscription", donation.donation_subscription)
        self.assertIsNotNone(subscription.payment)
        payment = cast("Payment", subscription.payment)
        Payment.objects.filter(pk=payment.pk).update(amount=123)

        with patch.object(
            RecurringPaymentsCommand, "peform_payment"
        ) as perform_payment:
            RecurringPaymentsCommand.handle_donations()

        perform_payment.assert_called_once()
        self.assertEqual(
            perform_payment.call_args.kwargs,
            {
                "amount": 123,
                "recurring": "y",
                "end_date": subscription.expires,
                "extra": {"donation_service": donation.pk},
            },
        )

    def test_past_subscription_payment_delete_is_restricted(self) -> None:
        service = self.create_service()
        subscription = service.subscription_set.get()
        past_payment = create_payment(
            user=service.customer.users.get(),
            customer=service.customer,
            state=Payment.PROCESSED,
        )[0]
        subscription.past_payments.add(past_payment)

        with self.assertRaises(RestrictedError):
            past_payment.delete()

    def test_add_subscription_past_payments_is_idempotent(self) -> None:
        service = self.create_service()
        subscription = service.subscription_set.get()
        past_payment = create_payment(
            user=service.customer.users.get(),
            customer=service.customer,
            state=Payment.PROCESSED,
        )[0]

        add_subscription_past_payments(subscription, past_payment)
        add_subscription_past_payments(subscription, past_payment)

        self.assertEqual(
            list(subscription.past_payments.values_list("pk", flat=True)),
            [past_payment.pk],
        )

    def test_recurring_service_payment_uses_package_price_and_subscription_extra(
        self,
    ) -> None:
        service = self.create_service(years=0, days=-2)
        subscription = service.subscription_set.get()

        with patch.object(
            RecurringPaymentsCommand, "peform_payment"
        ) as perform_payment:
            RecurringPaymentsCommand.handle_subscriptions()

        perform_payment.assert_called_once()
        self.assertEqual(
            perform_payment.call_args.kwargs,
            {
                "amount": subscription.package.price,
                "recurring": "y",
                "end_date": subscription.expires,
                "extra": {"subscription": subscription.pk},
            },
        )

    @responses.activate
    def test_hosted_pay(self) -> None:
        mock_vies()
        cnb_mock_rates()
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="test:test-1-m"
            )
            hosted = service.hosted_subscriptions
            self.assertEqual(
                hosted[0].expires.date(),
                timezone.now().date() + timedelta(days=3),
            )

            # Trigger customer editing
            service.customer.name = ""
            service.customer.save()

            subscription = service.subscription_set.get()
            response = self.client.post(
                reverse("subscription-pay", kwargs={"pk": subscription.pk}),
                follow=True,
            )
            payment_url = response.redirect_chain[0][0].split("localhost:1234")[-1]
            payment_edit_url = response.redirect_chain[1][0]
            self.assertTrue(payment_url.startswith("/en/payment/"))
            response = self.client.post(payment_edit_url, TEST_CUSTOMER, follow=True)
            self.assertRedirects(response, payment_url)
            response = self.client.post(payment_url, {"method": "pay"}, follow=True)
            self.assertRedirects(response, reverse("user"))
            self.assertContains(response, "Weblate hosting (basic)")
            self.assertContains(response, "Download invoice")
            self.assertContains(response, "Download receipt")

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package.name, "test:test-1-m")
        payment = hosted[0].payment_obj
        self.assertEqual(payment.amount, 50)
        self.assertEqual(
            hosted[0].expires.date(),
            timezone.now().date() + timedelta(days=3) + relativedelta(months=1),
        )

    @responses.activate
    def test_hosted_pay_yearly(self) -> None:
        mock_vies()
        cnb_mock_rates()
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="test:test-1-m"
            )
            subscription = service.subscription_set.get()
            response = self.client.post(
                reverse("subscription-pay", kwargs={"pk": subscription.pk}),
                {"switch_yearly": 1},
                follow=True,
            )
            payment_url = response.redirect_chain[0][0].split("localhost:1234")[-1]
            self.assertTrue(payment_url.startswith("/en/payment/"))
            response = self.client.post(payment_url, {"method": "pay"}, follow=True)
            self.assertRedirects(response, reverse("user"))
            self.assertContains(response, "Weblate hosting (basic)")

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package.name, "test:test-1")
        self.assertEqual(hosted[0].payment_obj.amount, 508)
        self.assertEqual(
            hosted[0].expires.date(),
            timezone.now().date() + timedelta(days=3) + relativedelta(years=1),
        )

    @override_settings(ZAMMAD_TOKEN="test")  # noqa: S106
    @responses.activate
    def test_dedicated_new(self) -> None:
        mock_vies()
        cnb_mock_rates()
        self.create_packages()
        responses.add(
            responses.POST,
            "https://care.weblate.org/api/v1/tickets",
            json={
                "id": 19,
                "group_id": 2,
                "priority_id": 2,
                "state_id": 1,
                "organization_id": None,
                "number": "22019",
                "title": "Help me!",
                "owner_id": 1,
                "customer_id": 10,
                "note": None,
                "first_response_at": None,
                "first_response_escalation_at": None,
                "first_response_in_min": None,
                "first_response_diff_in_min": None,
                "close_at": None,
                "close_escalation_at": None,
                "close_in_min": None,
                "close_diff_in_min": None,
                "update_escalation_at": None,
                "update_in_min": None,
                "update_diff_in_min": None,
                "last_contact_at": None,
                "last_contact_agent_at": None,
                "last_contact_customer_at": None,
                "last_owner_update_at": None,
                "create_article_type_id": 10,
                "create_article_sender_id": 1,
                "article_count": 1,
                "escalation_at": None,
                "pending_time": None,
                "type": None,
                "time_unit": None,
                "preferences": {},
                "updated_by_id": 3,
                "created_by_id": 3,
                "created_at": "2021-11-08T14:17:41.913Z",
                "updated_at": "2021-11-08T14:17:41.994Z",
                "article_ids": [30],
                "ticket_time_accounting_ids": [],
            },
        )

        with override("en"):
            self.login()
            response = self.client.get(
                reverse("subscription-new"),
                {"plan": "test:test-1"},
                follow=True,
            )
            payment_url = response.redirect_chain[0][0].split("localhost:1234")[-1]
            payment_edit_url = response.redirect_chain[1][0]
            self.assertTrue(payment_url.startswith("/en/payment/"))
            response = self.client.post(payment_edit_url, TEST_CUSTOMER, follow=True)
            self.assertRedirects(response, payment_url)
            response = self.client.post(payment_url, {"method": "pay"}, follow=True)
            self.assertRedirects(response, reverse("user"))
            self.assertContains(response, "Weblate hosting (upgraded)")

    @responses.activate
    def test_hosted_upgrade(self) -> None:
        mock_vies()
        cnb_mock_rates()
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="test:test-1"
            )
            suggestions = service.get_suggestions()
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0][0], "test:test-2")
            params: dict[str, str | int] = {
                "plan": "test:test-2",
                "service": service.pk,
            }
            response = self.client.get(reverse("subscription-new"), params, follow=True)
            payment_url = response.redirect_chain[0][0].split("localhost:1234")[-1]
            self.assertTrue(payment_url.startswith("/en/payment/"))
            response = self.client.post(payment_url, {"method": "pay"}, follow=True)
            self.assertRedirects(response, reverse("user"))
            self.assertContains(response, "Weblate hosting (upgraded)")

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package.name, "test:test-2")


class CommandsTestCase(FakturaceTestCase):
    def test_list_contacts(self) -> None:
        with StringIO() as buffer:
            call_command("list_contacts", stdout=buffer)
            self.assertEqual(buffer.getvalue(), "")

        self.create_service()
        with StringIO() as buffer:
            call_command("list_contacts", stdout=buffer)
            self.assertEqual(buffer.getvalue(), "noreply@weblate.org\n")

    def test_sync_packages(self) -> None:
        with StringIO() as buffer:
            call_command("sync_packages", stdout=buffer)
            self.assertNotEqual(buffer.getvalue(), "")
        for reward in range(4):
            package = Package.objects.get(name=get_donation_reward_package_name(reward))
            self.assertEqual(package.category, PackageCategory.PACKAGE_DONATION)
            self.assertEqual(package.price, REWARD_LEVELS[reward])

        with StringIO() as buffer:
            call_command("sync_packages", stdout=buffer)
            self.assertEqual(buffer.getvalue(), "")


class BackgroundFetchTestCase(FakturaceTestCase):
    """Tests for background_fetch management command and underlying code."""

    @responses.activate
    def test_get_release(self) -> None:
        pypi_data = {
            "releases": {
                "5.0": [
                    {
                        "comment_text": "",
                        "digests": {"sha256": "abc123"},
                        "downloads": 0,
                        "filename": "Weblate-5.0.tar.gz",
                        "has_sig": False,
                        "md5_digest": "md5",
                        "packagetype": "sdist",
                        "python_version": "source",
                        "requires_python": ">=3.9",
                        "size": 1000,
                        "upload_time": "2024-01-01T00:00:00",
                        "upload_time_iso_8601": "2024-01-01T00:00:00.000000Z",
                        "url": "https://example.com/Weblate-5.0.tar.gz",
                        "yanked": False,
                        "yanked_reason": None,
                    }
                ],
                "5.1": [
                    {
                        "comment_text": "",
                        "digests": {"sha256": "def456"},
                        "downloads": 0,
                        "filename": "Weblate-5.1.tar.gz",
                        "has_sig": False,
                        "md5_digest": "md5",
                        "packagetype": "sdist",
                        "python_version": "source",
                        "requires_python": ">=3.9",
                        "size": 2000,
                        "upload_time": "2024-06-01T00:00:00",
                        "upload_time_iso_8601": "2024-06-01T00:00:00.000000Z",
                        "url": "https://example.com/Weblate-5.1.tar.gz",
                        "yanked": False,
                        "yanked_reason": None,
                    }
                ],
                "4.0": [],
            }
        }
        responses.add(responses.GET, PYPI_URL, json=pypi_data)
        result = get_release(force=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["filename"], "Weblate-5.1.tar.gz")

    @responses.activate
    def test_get_release_error(self) -> None:
        responses.add(responses.GET, PYPI_URL, status=500)
        result = get_release(force=True)
        self.assertEqual(result, [])

    @responses.activate
    def test_get_release_network_error(self) -> None:
        responses.add(responses.GET, PYPI_URL, body=OSError("Connection refused"))
        result = get_release(force=True)
        self.assertEqual(result, [])

    @responses.activate
    def test_get_release_caching(self) -> None:
        pypi_data = {
            "releases": {
                "5.0": [
                    {
                        "comment_text": "",
                        "digests": {"sha256": "abc123"},
                        "downloads": 0,
                        "filename": "Weblate-5.0.tar.gz",
                        "has_sig": False,
                        "md5_digest": "md5",
                        "packagetype": "sdist",
                        "python_version": "source",
                        "requires_python": ">=3.9",
                        "size": 1000,
                        "upload_time": "2024-01-01T00:00:00",
                        "upload_time_iso_8601": "2024-01-01T00:00:00.000000Z",
                        "url": "https://example.com/Weblate-5.0.tar.gz",
                        "yanked": False,
                        "yanked_reason": None,
                    }
                ],
            }
        }
        responses.add(responses.GET, PYPI_URL, json=pypi_data)
        result = get_release(force=True)
        self.assertEqual(len(result), 1)
        # Cached result returned without force
        responses.replace(responses.GET, PYPI_URL, status=500)
        result = get_release(force=False)
        self.assertEqual(len(result), 1)

    def test_get_changes(self) -> None:
        mock_stat_data = {
            "last_change": "2024-06-01T00:00:00Z",
            "name": "Project A",
            "translated_percent": 50.0,
        }
        mock_stat_old_data = {
            "last_change": "2024-01-01T00:00:00Z",
            "name": "Project B",
            "translated_percent": 30.0,
        }
        mock_stat_none_data = {
            "last_change": None,
            "name": "Project C",
            "translated_percent": 10.0,
        }
        with patch("weblate_web.remote.Weblate") as mock_weblate:
            mock_stat_a = type(
                "MockStat",
                (),
                {
                    "__getitem__": lambda _self, key: mock_stat_data[key],
                    "get_data": lambda _self: mock_stat_data,
                },
            )()
            mock_stat_old = type(
                "MockStat",
                (),
                {
                    "__getitem__": lambda _self, key: mock_stat_old_data[key],
                    "get_data": lambda _self: mock_stat_old_data,
                },
            )()
            mock_stat_none = type(
                "MockStat",
                (),
                {
                    "__getitem__": lambda _self, key: mock_stat_none_data[key],
                    "get_data": lambda _self: mock_stat_none_data,
                },
            )()
            mock_project_a = type(
                "MockProject", (), {"statistics": lambda _self: mock_stat_a}
            )()
            mock_project_b = type(
                "MockProject", (), {"statistics": lambda _self: mock_stat_old}
            )()
            mock_project_c = type(
                "MockProject", (), {"statistics": lambda _self: mock_stat_none}
            )()
            mock_weblate.return_value.list_projects.return_value = [
                mock_project_a,
                mock_project_b,
                mock_project_c,
            ]
            result = get_changes(force=True)
        self.assertEqual(len(result), 2)
        # Should be sorted by last_change descending, Project C excluded (None)
        self.assertEqual(result[0]["name"], "Project A")
        self.assertEqual(result[1]["name"], "Project B")

    def test_get_changes_error(self) -> None:
        with patch("weblate_web.remote.Weblate") as mock_weblate:
            mock_weblate.return_value.list_projects.side_effect = WeblateException(
                "Connection failed"
            )
            result = get_changes(force=True)
        self.assertEqual(result, [])

    def test_get_changes_caching(self) -> None:
        mock_stat_data = {
            "last_change": "2024-06-01T00:00:00Z",
            "name": "Project A",
            "translated_percent": 50.0,
        }
        with patch("weblate_web.remote.Weblate") as mock_weblate:
            mock_stat = type(
                "MockStat",
                (),
                {
                    "__getitem__": lambda _self, key: mock_stat_data[key],
                    "get_data": lambda _self: mock_stat_data,
                },
            )()
            mock_project = type(
                "MockProject", (), {"statistics": lambda _self: mock_stat}
            )()
            mock_weblate.return_value.list_projects.return_value = [mock_project]
            result = get_changes(force=True)
            self.assertEqual(len(result), 1)
            # Cached result returned without force
            mock_weblate.return_value.list_projects.side_effect = Exception(
                "Should not be called"
            )
            result = get_changes(force=False)
            self.assertEqual(len(result), 1)

    @patch("weblate_web.management.commands.background_fetch.get_release")
    @patch("weblate_web.management.commands.background_fetch.get_changes")
    @patch("weblate_web.management.commands.background_fetch.get_activity")
    @patch("weblate_web.management.commands.background_fetch.get_contributors")
    def test_disable_stale_services_no_report(self, *mocks: object) -> None:
        """Service with discoverable=True but no report should not be disabled."""
        service = self.create_service()
        service.discoverable = True
        service.save(update_fields=["discoverable"])
        call_command("background_fetch")
        service.refresh_from_db()
        self.assertTrue(service.discoverable)

    @patch("weblate_web.management.commands.background_fetch.get_release")
    @patch("weblate_web.management.commands.background_fetch.get_changes")
    @patch("weblate_web.management.commands.background_fetch.get_activity")
    @patch("weblate_web.management.commands.background_fetch.get_contributors")
    def test_disable_stale_services_fresh(self, *mocks: object) -> None:
        """Service with a recent report should not be disabled."""
        service = self.create_service()
        service.discoverable = True
        service.save(update_fields=["discoverable"])
        Report.objects.create(
            service=service, site_url="https://example.com", discoverable=True
        )
        call_command("background_fetch")
        service.refresh_from_db()
        self.assertTrue(service.discoverable)

    @patch("weblate_web.management.commands.background_fetch.get_release")
    @patch("weblate_web.management.commands.background_fetch.get_changes")
    @patch("weblate_web.management.commands.background_fetch.get_activity")
    @patch("weblate_web.management.commands.background_fetch.get_contributors")
    def test_disable_stale_services_stale(self, *mocks: object) -> None:
        """Service with a stale report (>3 days old) should be disabled."""
        service = self.create_service()
        service.discoverable = True
        service.save(update_fields=["discoverable"])
        report = Report.objects.create(
            service=service, site_url="https://example.com", discoverable=True
        )
        # Make the report stale by backdating its timestamp
        Report.objects.filter(pk=report.pk).update(
            timestamp=timezone.now() - timedelta(days=4)
        )
        call_command("background_fetch")
        service.refresh_from_db()
        self.assertFalse(service.discoverable)

    @patch("weblate_web.management.commands.background_fetch.get_release")
    @patch("weblate_web.management.commands.background_fetch.get_changes")
    @patch("weblate_web.management.commands.background_fetch.get_activity")
    @patch("weblate_web.management.commands.background_fetch.get_contributors")
    def test_disable_stale_services_not_discoverable(self, *mocks: object) -> None:
        """Service that is not discoverable should not be affected."""
        service = self.create_service()
        report = Report.objects.create(service=service, site_url="https://example.com")
        Report.objects.filter(pk=report.pk).update(
            timestamp=timezone.now() - timedelta(days=4)
        )
        call_command("background_fetch")
        service.refresh_from_db()
        self.assertFalse(service.discoverable)

    @responses.activate
    def test_background_fetch_command(self) -> None:
        """Test that the background_fetch command calls all remote functions."""
        responses.add(
            responses.GET,
            WEBLATE_CONTRIBUTORS_URL,
            body=TEST_CONTRIBUTORS.read_text(),
        )
        responses.add(responses.GET, ACTIVITY_URL, body=TEST_ACTIVITY.read_text())
        responses.add(
            responses.GET,
            PYPI_URL,
            json={
                "releases": {
                    "5.0": [
                        {
                            "comment_text": "",
                            "digests": {"sha256": "abc"},
                            "downloads": 0,
                            "filename": "Weblate-5.0.tar.gz",
                            "has_sig": False,
                            "md5_digest": "md5",
                            "packagetype": "sdist",
                            "python_version": "source",
                            "requires_python": ">=3.9",
                            "size": 1000,
                            "upload_time": "2024-01-01T00:00:00",
                            "upload_time_iso_8601": "2024-01-01T00:00:00.000000Z",
                            "url": "https://example.com/Weblate-5.0.tar.gz",
                            "yanked": False,
                            "yanked_reason": None,
                        }
                    ],
                }
            },
        )
        with patch("weblate_web.remote.Weblate") as mock_weblate:
            mock_weblate.return_value.list_projects.return_value = []
            call_command("background_fetch")
        # Verify all remote functions populated the cache
        self.assertIsNotNone(cache.get("wlweb-contributors"))
        self.assertIsNotNone(cache.get("wlweb-activity-stats"))
        self.assertIsNotNone(cache.get("wlweb-changes-list"))
        self.assertIsNotNone(cache.get("wlweb-release-x"))


class ExchangeRatesTestCase(SimpleTestCase):
    def mock_rate(self):
        # Valid response
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-01",
            json=RATES_JSON,
        )
        # Proper error response
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-02",
            status=500,
        )
        # Server error without a proper status code
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-03",
            "Interní chyba serveru",
        )
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-04",
            status=500,
        )
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-05",
            status=500,
        )
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-06",
            status=500,
        )
        responses.get(
            "https://api.cnb.cz/cnbapi/exrates/daily?date=2000-01-07",
            status=500,
        )

    @responses.activate
    def test_mocked(self):
        self.mock_rate()
        self.assertEqual(
            UncachedExchangeRates.get("EUR", date(2000, 1, 1)), Decimal("22.222")
        )

    @responses.activate
    def test_czk(self):
        self.assertEqual(UncachedExchangeRates.get("CZK", date(2000, 1, 1)), Decimal(1))

    @responses.activate
    def test_fallback(self):
        self.mock_rate()
        self.assertEqual(
            UncachedExchangeRates.get("EUR", date(2000, 1, 2)), Decimal("22.222")
        )

    @responses.activate
    def test_fallback_hidden_error(self):
        self.mock_rate()
        self.assertEqual(
            UncachedExchangeRates.get("EUR", date(2000, 1, 3)), Decimal("22.222")
        )

    @responses.activate
    def test_error(self):
        self.mock_rate()
        with self.assertRaises(HTTPError):
            UncachedExchangeRates.get("EUR", date(2000, 1, 7))


class StorageBoxTestCase(FakturaceTestCase):
    def test_password_is_ascii_and_within_byte_limit(self):
        password = generate_random_password()
        # Ensure all characters are ASCII
        self.assertTrue(all(ord(ch) < 128 for ch in password))
        # Ensure the UTF-8 encoded password fits within the 128-byte limit
        self.assertLessEqual(len(password.encode("utf-8")), 128)
        # For ASCII, character count should match UTF-8 byte length
        self.assertEqual(len(password), len(password.encode("utf-8")))

    @responses.activate
    def test_create_fail(self):
        service = self.create_service(years=0, days=-2, recurring="")
        responses.post(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts",
            status=422,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": "invalid input in field password",
                    "details": {
                        "fields": [
                            {
                                "name": "password",
                                "messages": [
                                    "The password must contain at least one upper case letter, one lower case letter, one number, and a special character"
                                ],
                            }
                        ]
                    },
                }
            },
        )
        with (
            patch("weblate_web.models.create_storage_folder"),
            self.assertRaisesRegex(
                HTTPError, "invalid input in field password, The password must contain"
            ),
        ):
            service.create_backup_repository(Report())

    @responses.activate
    def test_create_fail_without_details(self):
        service = self.create_service(years=0, days=-2, recurring="")
        responses.post(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts",
            status=422,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": "invalid input in field password",
                    "details": None,
                }
            },
        )
        with (
            patch("weblate_web.models.create_storage_folder"),
            self.assertRaisesRegex(HTTPError, "invalid input in field password"),
        ):
            service.create_backup_repository(Report())

    @responses.activate
    def test_create(self):
        service = self.create_service(years=0, days=-2, recurring="")
        responses.post(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts",
            json={
                "action": {
                    "id": 13,
                    "command": "create_subaccount",
                    "status": "running",
                    "progress": 0,
                    "started": "2016-01-30T23:50:00+00:00",
                    "finished": None,
                    "resources": [
                        {"id": 42, "type": "storage_box"},
                        {"id": 42, "type": "storage_box_subaccount"},
                    ],
                    "error": None,
                }
            },
        )
        responses.get(
            "https://api.hetzner.com/v1/storage_boxes/153391/actions/13",
            json={
                "action": {
                    "id": 13,
                    "command": "create_subaccount",
                    "status": "success",
                    "progress": 100,
                    "started": "2016-01-30T23:50:00+00:00",
                    "finished": "2016-01-30T23:50:00+00:00",
                    "resources": [
                        {"id": 42, "type": "storage_box"},
                        {"id": 42, "type": "storage_box_subaccount"},
                    ],
                    "error": None,
                }
            },
        )
        responses.get(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts/42",
            json={
                "subaccount": {
                    "id": 42,
                    "username": "u1337-sub1",
                    "home_directory": "my_backups/host01.my.company",
                    "server": "u1337-sub1.your-storagebox.de",
                    "access_settings": {
                        "reachable_externally": False,
                        "readonly": False,
                        "samba_enabled": False,
                        "ssh_enabled": False,
                        "webdav_enabled": False,
                    },
                    "description": "host01 backup",
                    "labels": {
                        "environment": "prod",
                        "example.com/my": "label",
                        "just-a-key": "",
                    },
                    "created": "2016-01-30T23:55:00+00:00",
                    "storage_box": 42,
                }
            },
        )
        with patch("weblate_web.models.create_storage_folder"):
            service.create_backup_repository(Report())

    @responses.activate
    def test_sync(self):
        test_repo = "ssh://u1337-sub1@u1337-sub1.your-storagebox.de:23/./backups"
        service = self.create_service(years=0, days=-2, recurring="")
        service.backup_repository = test_repo

        responses.get(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts",
            json={
                "subaccounts": [
                    {
                        "id": 42,
                        "username": "u1337-sub1",
                        "home_directory": "weblate/host01.my.company",
                        "server": "u1337-sub1.your-storagebox.de",
                        "access_settings": {
                            "reachable_externally": False,
                            "readonly": False,
                            "samba_enabled": False,
                            "ssh_enabled": False,
                            "webdav_enabled": False,
                        },
                        "description": "host01 backup",
                        "labels": {
                            "environment": "prod",
                            "example.com/my": "label",
                            "just-a-key": "",
                        },
                        "created": "2016-01-30T23:55:00+00:00",
                        "storage_box": 42,
                    }
                ]
            },
        )
        responses.put(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts/42",
            json={
                "subaccount": {
                    "id": 42,
                    "username": "u1337-sub1",
                    "home_directory": "my_backups/host01.my.company",
                    "server": "u1337-sub1.your-storagebox.de",
                    "access_settings": {
                        "reachable_externally": False,
                        "readonly": False,
                        "samba_enabled": False,
                        "ssh_enabled": False,
                        "webdav_enabled": False,
                    },
                    "description": "host01 backup",
                    "labels": {
                        "environment": "prod",
                        "example.com/my": "label",
                        "just-a-key": "",
                    },
                    "created": "2016-01-30T23:55:00+00:00",
                    "storage_box": 42,
                }
            },
        )
        responses.post(
            "https://api.hetzner.com/v1/storage_boxes/153391/subaccounts/42/actions/update_access_settings",
            json={
                "action": {
                    "id": 13,
                    "command": "update_access_settings",
                    "status": "running",
                    "progress": 0,
                    "started": "2016-01-30T23:50:00+00:00",
                    "finished": None,
                    "resources": [
                        {"id": 42, "type": "storage_box"},
                        {"id": 42, "type": "storage_box_subaccount"},
                    ],
                    "error": None,
                }
            },
        )
        responses.get(
            "https://api.hetzner.com/v1/storage_boxes/153391/actions/13",
            json={
                "action": {
                    "id": 13,
                    "command": "update_access_settings",
                    "status": "success",
                    "progress": 100,
                    "started": "2016-01-30T23:50:00+00:00",
                    "finished": "2016-01-30T23:50:00+00:00",
                    "resources": [
                        {"id": 42, "type": "storage_box"},
                        {"id": 42, "type": "storage_box_subaccount"},
                    ],
                    "error": None,
                }
            },
        )

        command = BackupsSyncCommand()
        services_dict = {test_repo: service}
        self.assertEqual(command.sync_data(services_dict), {test_repo})


class DiscoveryTestCase(UserTestCase):
    def test_create(self):
        # This requires login
        response = self.client.get("/subscription/discovery/", follow=True)
        self.assertRedirects(
            response, "/admin/login/?next=%2Fen%2Fsubscription%2Fdiscovery%2F"
        )
        self.login()
        # Test exact URL because that is used from Weblate
        response = self.client.get("/subscription/discovery/", follow=True)
        self.assertRedirects(response, "/en/subscription/discovery/")

        # Missing required fields
        response = self.client.post("/en/subscription/discovery/", {}, follow=True)
        self.assertContains(response, "This field is required.")

        # Valid activation
        response = self.client.post(
            "/en/subscription/discovery/",
            {"site_url": "http://localhost", "discover_text": "Discover localhost"},
        )
        service = Service.objects.get()
        self.assertEqual(service.site_url, "http://localhost")
        self.assertNotEqual(service.secret, "")
        self.assertRedirects(
            response,
            f"http://localhost/manage/?activation={service.secret}",
            fetch_redirect_response=False,
        )
