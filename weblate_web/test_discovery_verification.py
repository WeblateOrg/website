# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from functools import partial
from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.utils.crypto import salted_hmac
from httpx2 import AsyncByteStream, AsyncClient, MockTransport, Request, Response

from weblate_web.discovery import verify_discovery_proof
from weblate_web.models import DiscoveryVerification, Package, Report, Service
from weblate_web.payments.models import Customer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class ProofStream(AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


class DiscoveryVerificationTest(TestCase):
    site_url = "https://instance.example/translations"

    def setUp(self) -> None:
        Package.objects.create(name="community", verbose="Community support", price=0)
        self.service = Service.objects.create(
            customer=Customer.objects.create(user_id=-1, origin="test"),
            site_url=self.site_url,
        )

    def report_discovery(self, **data: str) -> None:
        response = self.client.post(
            "/api/support/",
            {
                "secret": self.service.secret,
                "site_url": self.site_url,
                "discoverable": "1",
                "discovery_verification": "1",
                **data,
            },
            headers={"User-Agent": "Weblate/2026.11"},
        )
        self.assertEqual(response.status_code, 200)

    def matches_proof(self, site_url: str, nonce: str, expected: bytes) -> bool:
        self.assertEqual(site_url, self.site_url)
        return (
            salted_hmac(
                "weblate.discovery",
                nonce,
                secret=self.service.secret,
                algorithm="sha256",
            )
            .hexdigest()
            .encode("ascii")
            == expected
        )

    def test_publish_with_matching_instance_proof(self) -> None:
        self.report_discovery(
            site_title="Example instance",
            public_projects=json.dumps(
                [
                    {
                        "name": "Working project",
                        "url": "/translations/projects/example/",
                        "web": "",
                    },
                    *[
                        {"name": "Invalid project", "url": path, "web": ""}
                        for path in (
                            "invalid",
                            "//other.example/projects/example/",
                            r"/projects\example/",
                            "/projects/\texample/",
                        )
                    ],
                ]
            ),
        )
        self.assertNotContains(self.client.get("/en/discover/"), "Example instance")
        with patch("weblate_web.discovery.verify_discovery_proof", self.matches_proof):
            call_command("verify_discovery")
        self.service.refresh_from_db()
        self.assertEqual(self.service.discovery_verified_url, self.site_url)
        response = self.client.get("/en/discover/")
        self.assertContains(response, "Example instance")
        self.assertContains(response, f'href="{self.site_url}/projects/example/"')
        self.assertFalse(
            self.service.project_set.filter(name="Invalid project").exists()
        )
        self.assertFalse(DiscoveryVerification.objects.exists())

    def test_failed_proof_retries_after_delay(self) -> None:
        self.report_discovery()
        output = StringIO()
        with patch(
            "weblate_web.discovery.verify_discovery_proof", return_value=False
        ) as check:
            call_command("verify_discovery", stdout=output)
            call_command("verify_discovery", stdout=output)
        check.assert_called_once()
        self.assertIn("1 retry", output.getvalue())
        self.service.refresh_from_db()
        self.assertFalse(self.service.discoverable)
        pending = DiscoveryVerification.objects.get(service=self.service)
        self.assertGreater(pending.next_attempt, pending.report.timestamp)
        with (
            patch("django.utils.timezone.now", return_value=pending.next_attempt),
            patch("weblate_web.discovery.verify_discovery_proof", self.matches_proof),
        ):
            call_command("verify_discovery")
        self.service.refresh_from_db()
        self.assertTrue(self.service.discoverable)
        self.assertFalse(DiscoveryVerification.objects.exists())

    def test_expired_proof_is_discarded_without_fetching(self) -> None:
        self.report_discovery()
        self.service.report_set.update(timestamp=timezone.now() - timedelta(hours=1))
        with patch("weblate_web.discovery.verify_discovery_proof") as check:
            call_command("verify_discovery")
        check.assert_not_called()
        self.assertFalse(DiscoveryVerification.objects.exists())
        self.service.refresh_from_db()
        self.assertFalse(self.service.discoverable)

    def test_report_deduplicates_existing_and_new_projects(self) -> None:
        first = {"name": "First", "url": "/projects/first/", "web": ""}
        second = {"name": "Second", "url": "/projects/second/", "web": ""}
        kept = self.service.project_set.create(**first)
        self.service.project_set.create(**first)
        self.report_discovery(
            public_projects=json.dumps([first, first, second, second])
        )
        self.assertEqual(
            list(
                self.service.project_set.order_by("pk").values_list("name", flat=True)
            ),
            ["First", "Second"],
        )
        self.assertTrue(self.service.project_set.filter(pk=kept.pk).exists())

    def test_opt_out_during_verification_keeps_listing_private(self) -> None:
        self.report_discovery()

        def opt_out(site_url: str, nonce: str, expected: bytes) -> bool:
            self.report_discovery(discoverable="")
            return self.matches_proof(site_url, nonce, expected)

        with patch("weblate_web.discovery.verify_discovery_proof", opt_out):
            call_command("verify_discovery")
        self.service.refresh_from_db()
        self.assertFalse(self.service.discoverable)
        self.assertEqual(self.service.discovery_verified_url, "")

    @patch("weblate_web.discovery.httpx2.AsyncClient")
    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_verify_rejects_mixed_public_and_private_dns_results(
        self, resolve, client
    ) -> None:
        resolve.return_value = Mock(
            addresses=Mock(return_value=("1.1.1.1", "127.0.0.1"))
        )
        self.assertFalse(verify_discovery_proof(self.site_url, "a" * 64, b"b" * 64))
        client.assert_not_called()

    @patch("weblate_web.discovery.PROOF_TIMEOUT", 0)
    @patch("weblate_web.discovery.httpx2.AsyncClient")
    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_dns_timeout_cancels_lookup_and_prevents_http_request(
        self, resolve, client
    ) -> None:
        cancelled = Mock()

        async def stall(*args: object, **kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled()

        resolve.side_effect = stall
        self.assertFalse(verify_discovery_proof(self.site_url, "a" * 64, b"b" * 64))
        cancelled.assert_called_once()
        client.assert_not_called()

    @patch("weblate_web.discovery.PROOF_TIMEOUT", 0)
    @patch("weblate_web.discovery._check_discovery_addresses")
    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_dns_and_http_share_verification_deadline(self, resolve, check) -> None:
        resolve.return_value = Mock(addresses=Mock(return_value=("1.1.1.1",)))
        cancelled = Mock()

        async def stall(*args: object) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled()

        check.side_effect = stall
        self.assertFalse(verify_discovery_proof(self.site_url, "a" * 64, b"b" * 64))
        resolve.assert_awaited_once()
        check.assert_awaited_once()
        cancelled.assert_called_once()

    @patch("weblate_web.discovery.httpx2.AsyncClient")
    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_private_ip_literal_is_rejected_without_dns(self, resolve, client) -> None:
        self.assertFalse(
            verify_discovery_proof("https://127.0.0.1", "a" * 64, b"b" * 64)
        )
        resolve.assert_not_awaited()
        client.assert_not_called()

    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_verify_tries_pinned_addresses_until_proof_matches(self, resolve) -> None:
        addresses = ("1.1.1.1", "2606:4700:4700::1111")
        resolve.return_value = Mock(
            addresses=Mock(return_value=(*addresses, addresses[0]))
        )
        nonce = "a" * 64
        expected = b"b" * 64
        attempts: list[str] = []

        def respond(request: Request) -> Response:
            attempts.append(request.url.host)
            self.assertEqual(request.url.port, 8443)
            self.assertEqual(request.headers["Host"], "instance.example:8443")
            self.assertEqual(request.extensions["sni_hostname"], "instance.example")
            self.assertEqual(
                request.url.path,
                f"/translations/.well-known/weblate-discovery/{nonce}/",
            )
            body = expected if request.url.host == addresses[1] else b"c" * 64
            return Response(200, stream=ProofStream(body))

        with patch(
            "weblate_web.discovery.httpx2.AsyncClient",
            side_effect=partial(AsyncClient, transport=MockTransport(respond)),
        ):
            self.assertTrue(
                verify_discovery_proof(
                    "https://instance.example:8443/translations", nonce, expected
                )
            )
        self.assertEqual(tuple(attempts), addresses)
        resolve.assert_awaited_once_with("instance.example", search=False)

    @patch("weblate_web.discovery.dns.asyncresolver.resolve_name")
    def test_verify_rejects_redirects_and_oversized_streams(self, resolve) -> None:
        resolve.return_value = Mock(addresses=Mock(return_value=("1.1.1.1",)))
        expected = b"b" * 64
        for status, body in ((302, expected), (200, expected + b"x")):
            with self.subTest(status=status):
                respond = Mock(
                    return_value=Response(
                        status,
                        headers={"Location": "https://other.example/"},
                        stream=ProofStream(body),
                    )
                )

                with patch(
                    "weblate_web.discovery.httpx2.AsyncClient",
                    side_effect=partial(AsyncClient, transport=MockTransport(respond)),
                ):
                    self.assertFalse(
                        verify_discovery_proof(self.site_url, "a" * 64, expected)
                    )
                respond.assert_called_once()


class DiscoveryVerificationMigrationTest(TransactionTestCase):
    def test_only_existing_public_listings_are_grandfathered(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin="test")
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()

        def restore_migrations() -> None:
            MigrationExecutor(connection).migrate(latest)
            ContentType.objects.clear_cache()

        self.addCleanup(restore_migrations)
        previous = [("weblate_web", "0060_hosted_administrator_owners")]
        executor.migrate(previous)
        services = executor.loader.project_state(previous).apps.get_model(
            "weblate_web", "Service"
        )
        listed = services.objects.create(
            customer_id=customer.pk,
            site_url="HTTPS://INSTANCE.EXAMPLE:443/translations/",
            discoverable=True,
        )
        private = services.objects.create(
            customer_id=customer.pk, site_url="https://private.example"
        )
        restore_migrations()
        self.assertEqual(
            Service.objects.get(pk=listed.pk).discovery_verified_url, listed.site_url
        )
        self.assertEqual(Service.objects.get(pk=private.pk).discovery_verified_url, "")
        service = Service.objects.get(pk=listed.pk)
        site_url = "https://instance.example/translations"
        Report.objects.create(service=service, site_url=site_url, discoverable=True)
        service.refresh_from_db()
        self.assertTrue(service.discoverable)
        self.assertEqual(service.discovery_verified_url, site_url)
