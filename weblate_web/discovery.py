# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import ssl
from datetime import timedelta
from typing import Literal

import dns.asyncresolver
import httpx2
from asgiref.sync import async_to_sync
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from dns.exception import DNSException

from .models import DiscoveryVerification, Service

PROOF_MAX_AGE = timedelta(hours=1)
PROOF_RETRY_DELAY = timedelta(minutes=5)
PROOF_PATH = "/.well-known/weblate-discovery/"
PROOF_SIZE = 64
PROOF_TIMEOUT = 5
PROOF_ADDRESS_TIMEOUT = 2
NON_PUBLIC_NETWORKS = tuple(
    map(
        ipaddress.ip_network,
        (
            "192.88.99.0/24",
            "::/96",
            "64:ff9b::/96",
            "64:ff9b:1::/48",
            "5f00::/16",
            "2001:20::/28",
        ),
    )
)


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    # Avoid translated destinations and scoped addresses.
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.scope_id or ip.ipv4_mapped or ip.sixtofour or ip.teredo
    ):
        return False
    return (
        ip.is_global
        and not (ip.is_multicast or ip.is_reserved)
        and not any(ip in network for network in NON_PUBLIC_NETWORKS)
    )


async def _resolve_discovery_addresses(host: str) -> tuple[str, ...]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        answers = await dns.asyncresolver.resolve_name(host, search=False)
        return tuple(dict.fromkeys(answers.addresses()))
    return (str(address),)


async def _check_discovery_addresses(
    url: httpx2.URL, addresses: tuple[str, ...], expected: bytes
) -> bool:
    headers = {"Host": url.netloc.decode("ascii"), "Accept-Encoding": "identity"}
    extensions = {"sni_hostname": url.raw_host.decode("ascii")}
    async with httpx2.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        verify=ssl.create_default_context(),
    ) as client:
        for index, address in enumerate(addresses, start=1):
            try:
                # Use only the validated peer, retaining the original HTTP and TLS host.
                async with (
                    asyncio.timeout(
                        PROOF_ADDRESS_TIMEOUT if index < len(addresses) else None
                    ),
                    client.stream(
                        "GET",
                        url.copy_with(host=address),
                        headers=headers,
                        extensions=extensions,
                    ) as response,
                ):
                    if response.status_code != 200:
                        continue
                    proof = await anext(response.aiter_raw(PROOF_SIZE + 1), b"")
                    if len(proof) == PROOF_SIZE and constant_time_compare(
                        proof, expected
                    ):
                        return True
            except (httpx2.RequestError, TimeoutError):
                continue
    return False


@async_to_sync
async def verify_discovery_proof(site_url: str, nonce: str, expected: bytes) -> bool:
    try:
        url = httpx2.URL(f"{site_url}{PROOF_PATH}{nonce}/")
    except (httpx2.InvalidURL, ValueError):
        return False
    if url.scheme != "https" or not url.host:
        return False
    try:
        async with asyncio.timeout(PROOF_TIMEOUT):
            addresses = await _resolve_discovery_addresses(url.raw_host.decode("ascii"))
            if not addresses or not all(map(is_public_address, addresses)):
                return False
            return await _check_discovery_addresses(url, addresses, expected)
    except (DNSException, OSError):
        # The shared deadline also raises an OSError subclass (TimeoutError).
        return False


def process_discovery_verification(
    pending: DiscoveryVerification,
) -> Literal["verified", "retry", "discarded", "superseded"]:
    report = pending.report
    site_url = report.site_url
    secret = pending.service.secret
    expires = report.timestamp + PROOF_MAX_AGE
    verified = False
    if pending.service.site_url == site_url and timezone.now() < expires:
        nonce = secrets.token_hex(32)
        expected = (
            salted_hmac("weblate.discovery", nonce, secret=secret, algorithm="sha256")
            .hexdigest()
            .encode("ascii")
        )
        verified = verify_discovery_proof(site_url, nonce, expected)

    with transaction.atomic():
        service = Service.objects.select_for_update().filter(pk=pending.pk).first()
        if service is None:
            return "superseded"
        current = DiscoveryVerification.objects.filter(
            pk=pending.pk, report=report, next_attempt=pending.next_attempt
        )
        now = timezone.now()
        if service.secret != secret or service.site_url != site_url or now >= expires:
            return "discarded" if current.delete()[0] else "superseded"
        if not verified:
            next_attempt = min(now + PROOF_RETRY_DELAY, expires)
            return (
                "retry" if current.update(next_attempt=next_attempt) else "superseded"
            )
        if not current.delete()[0]:
            return "superseded"
        service.discovery_verified_url = site_url
        service.discoverable = True
        service.save(update_fields=["discovery_verified_url", "discoverable"])
        return "verified"
