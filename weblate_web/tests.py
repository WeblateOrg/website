from __future__ import annotations

import json
import re
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast
from xml.etree import ElementTree  # noqa: S405

import responses
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.signing import dumps
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from weblate_web.payments.data import SUPPORTED_LANGUAGES
from weblate_web.payments.models import Customer, Payment
from weblate_web.utils import FOSDEM_ORIGIN

from .management.commands.recurring_payments import Command as RecurringPaymentsCommand
from .models import Donation, Package, PackageCategory, Post, Service, Subscription
from .remote import (
    ACTIVITY_URL,
    WEBLATE_CONTRIBUTORS_URL,
    get_activity,
    get_contributors,
)
from .templatetags.downloads import downloadlink, filesizeformat
from .utils import PAYMENTS_ORIGIN

if TYPE_CHECKING:
    from uuid import UUID

TEST_DATA = Path(__file__).parent / "test-data"
TEST_CONTRIBUTORS = TEST_DATA / "contributors.json"
TEST_ACTIVITY = TEST_DATA / "activity.json"
TEST_VIES_WSDL = TEST_DATA / "checkVatService.wsdl"

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

    def test_index_redirect(self) -> None:
        response = self.client.get("/")
        self.assertRedirects(response, "/en/", 302)

    def test_index_en(self) -> None:
        response = self.client.get("/en/")
        self.assertContains(response, "yearly")

    def test_index_cs(self) -> None:
        response = self.client.get("/cs/")
        self.assertContains(response, "ročně")

    def test_index_he(self) -> None:
        response = self.client.get("/he/")
        self.assertContains(response, "שנתי")

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


def create_payment(*, recurring="y", user, **kwargs):
    customer = Customer.objects.create(
        email="weblate@example.com",
        user_id=user.pk,
        origin=PAYMENTS_ORIGIN,
    )
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
    def assert_notifications(self, *subjects):
        self.assertEqual({m.subject for m in mail.outbox}, set(subjects))
        result = mail.outbox
        mail.outbox = []
        return result

    def create_donation(self, years=1, days=0, recurring="y"):
        user = self.create_user()
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
        )
        customer.users.add(user)
        return Donation.objects.create(
            reward=3,
            customer=customer,
            active=True,
            expires=timezone.now() + timedelta(days=days) + relativedelta(years=years),
            payment=create_payment(
                recurring=recurring, user=user, state=Payment.PROCESSED
            )[0].pk,
            link_url="https://example.com/weblate",
            link_text="Weblate donation test",
        )

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

    def create_service(self, years=1, days=0, recurring="y", package="extended"):
        user = self.create_user()
        self.create_packages()
        customer = Customer.objects.create(
            user_id=-1,
            origin=PAYMENTS_ORIGIN,
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
            extra={"subscription": subscription.pk},
            state=Payment.PROCESSED,
        )[0].pk
        subscription.save(update_fields=["payment"])
        return service


class PaymentsTest(FakturaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        fake_remote()

    def test_languages(self) -> None:
        self.assertEqual(
            set(SUPPORTED_LANGUAGES),
            {x[0] for x in settings.LANGUAGES},
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
    def test_pay(self) -> None:
        payment, url, _dummy = self.prepare_payment()
        response = self.client.post(url, {"method": "pay"})
        self.assertRedirects(
            response,
            f"/en/donate/process/?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.ACCEPTED)

        self.login()
        response = self.client.get(reverse("user-invoice", kwargs={"pk": payment.pk}))
        self.assertEqual(response.status_code, 200)

    @responses.activate
    def test_invalid_vat(self) -> None:
        mock_vies(valid=False)
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
        self.prepare_payment()
        call_command("background_vat")

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
            "https://cihar.com/?url=http://localhost:1234" + complete_url,
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

    @override_settings(**THEPAY2_MOCK_SETTINGS)
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
        RecurringPaymentsCommand.notify_expiry()
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
        donation = Donation.objects.all().get()
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
        self.assert_notifications("Your payment on weblate.org")

        renew.refresh_from_db()
        self.assertEqual(renew.state, Payment.PROCESSED)
        self.assertEqual(payment.paid_invoice.total_amount, 1000)  # type: ignore[union-attr]

        # Service should not get notifications on expiry now
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications()

        # Move expiry into past and renew
        thepay_mock_repeated_payment()
        donation = Donation.objects.all().get()
        donation.expires -= timedelta(days=365 * 2)
        donation.save(update_fields=["expires"])
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your payment on weblate.org")

        # Disable recurring payments
        response = self.client.post(
            reverse("donate-disable", kwargs={"pk": donation.pk})
        )
        self.assertRedirects(response, reverse("user"))

        # Ensure no payment is made
        donation = Donation.objects.all().get()
        donation.expires -= timedelta(days=365)
        donation.save(update_fields=["expires"])
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
        Donation.objects.create(
            reward=2,
            customer=customer,
            active=True,
            expires=timezone.now() + relativedelta(years=1),
            payment=create_payment(user=user)[0].pk,
        )
        self.assertContains(self.client.get(reverse("user")), "My donations")

    def test_link(self) -> None:
        self.create_donation()
        response = self.client.get("/en/thanks/", follow=True)
        self.assertContains(response, "https://example.com/weblate")
        self.assertContains(response, "Weblate donation test")

    @responses.activate
    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring(self) -> None:
        donation = self.create_donation(years=0)
        # No recurring payments for now
        self.assertEqual(donation.payment_obj.payment_set.count(), 0)

        # Trigger payment and process it
        call_command("recurring_payments")

        # There should be additional payment
        self.assertEqual(donation.payment_obj.payment_set.count(), 1)
        # Verify it is processed
        self.assertEqual(
            donation.payment_obj.payment_set.get().state, Payment.PROCESSED
        )

        # Verify expiry has been moved
        old = donation.expires
        donation.refresh_from_db()
        self.assertGreater(donation.expires, old)

        # Process pending payments (should do nothing)
        call_command("process_payments")

    @override_settings(**THEPAY2_MOCK_SETTINGS)
    @responses.activate
    def test_fosdem_donation(self) -> None:
        thepay_mock_create_payment()

        response = self.client.get("/fosdem/donate/", follow=True)
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.amount, 30)
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
        response = self.client.get(payment.get_complete_url())
        # This redirects to an article we don't have in tets
        self.assertRedirects(response, FOSDEM_ORIGIN, fetch_redirect_response=False)

        # Fetch any page to verif that message is shown
        response = self.client.get("/about/", follow=False)
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
            headers={"user-agent": "weblate/1.2.3"},
        )
        self.assertEqual(response.status_code, 200)

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
            headers={"user-agent": "weblate/1.2.3"},
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
            headers={"user-agent": "weblate/1.2.3"},
        )
        service = Service.objects.get(pk=service.pk)
        self.assertTrue(service.discoverable)
        self.client.post(
            "/api/support/",
            {"secret": service.secret},
            headers={"user-agent": "weblate/1.2.3"},
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
            headers={"user-agent": "weblate/1.2.3"},
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
            headers={"user-agent": "weblate/1.2.3"},
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
            headers={"user-agent": "weblate/1.2.3"},
        )
        self.assertEqual(service.project_set.count(), 0)

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
    def test_expiring_donate(self) -> None:
        self.create_donation(years=0, days=3, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_expiring_recurring_donate(self) -> None:
        self.create_donation(years=0, days=3)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications()
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your payment on weblate.org")
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications()

    def test_expiring_donate_notify_user(self) -> None:
        self.create_donation(years=0, days=8, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        mails = self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming renewal on weblate.org",
        )
        self.assertEqual("Your upcoming renewal on weblate.org", mails[0].subject)
        self.assertIn("€100", mails[0].alternatives[0][0])
        self.assertIn("€100", mails[0].body)

    def test_expiring_recurring_donate_notify_user(self) -> None:
        self.create_donation(years=0, days=8)
        RecurringPaymentsCommand.notify_expiry()
        mails = self.assert_notifications("Your upcoming payment on weblate.org")
        self.assertIn("€100", mails[0].alternatives[0][0])
        self.assertIn("€100", mails[0].body)

    def test_expiring_subscription(self) -> None:
        self.create_service(years=0, days=3, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_expiring_recurring_subscription(self) -> None:
        self.create_service(years=0, days=3)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications()
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your payment on weblate.org")
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications()

    def test_expiring_subscription_notify_user(self) -> None:
        self.create_service(years=0, days=8, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming renewal on weblate.org",
        )

    def test_expiring_recurring_subscription_notify_user(self) -> None:
        self.create_service(years=0, days=8)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Your upcoming payment on weblate.org")


@override_settings(
    NOTIFY_SUBSCRIPTION=["noreply@example.com"],
    PAYMENT_DEBUG=True,
)
class ServiceTest(FakturaceTestCase):
    @responses.activate
    def test_hosted_pay(self) -> None:
        mock_vies()
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

            response = self.client.post(
                reverse("subscription-pay", kwargs={"pk": service.pk}),
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

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package.name, "test:test-1-m")
        self.assertEqual(hosted[0].payment_obj.amount, 42)
        self.assertEqual(
            hosted[0].expires.date(),
            timezone.now().date() + timedelta(days=3) + relativedelta(months=1),
        )

    @responses.activate
    def test_hosted_pay_yearly(self) -> None:
        mock_vies()
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="test:test-1-m"
            )
            response = self.client.post(
                reverse("subscription-pay", kwargs={"pk": service.pk}),
                {"switch_yearly": 1},
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

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package.name, "test:test-1")
        self.assertEqual(hosted[0].payment_obj.amount, 420)
        self.assertEqual(
            hosted[0].expires.date(),
            timezone.now().date() + timedelta(days=3) + relativedelta(years=1),
        )

    @override_settings(ZAMMAD_TOKEN="test")  # noqa: S106
    @responses.activate
    def test_decicated_new(self) -> None:
        mock_vies()
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
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="test:test-1"
            )
            suggestions = service.get_suggestions()
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0][0], "test:test-2")
            response = self.client.get(
                reverse("subscription-new"),
                {"plan": "test:test-2", "service": service.pk},
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

        with StringIO() as buffer:
            call_command("sync_packages", stdout=buffer)
            self.assertEqual(buffer.getvalue(), "")
