# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
import responses
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.signing import dumps
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from jwt.algorithms import RSAAlgorithm
from jwt.utils import base64url_encode
from requests.exceptions import Timeout
from social_django.models import UserSocialAuth

from weblate_web.hosted_users import sync_hosted_identity, sync_hosted_payload
from weblate_web.management.commands.sync_hosted_users import USER_SYNC_RESPONSE_SALT
from weblate_web.models import HostedIdentity
from weblate_web.payments.models import Customer

if TYPE_CHECKING:
    from typing import ClassVar

    from requests import PreparedRequest

ISSUER = "https://hosted.weblate.org/oidc"


@override_settings(
    SOCIAL_AUTH_WEBLATE_KEY="website-test",
    SOCIAL_AUTH_WEBLATE_SECRET="website-test-secret",  # ruff:ignore[hardcoded-password-func-arg]
)
class HostedOIDCTest(TestCase):
    key: ClassVar[rsa.RSAPrivateKey]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Discovery/JWKS are cached across backend instances and test methods.
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self) -> None:
        self.http = responses.RequestsMock(assert_all_requests_are_fired=False)
        self.http.start()
        self.addCleanup(self.http.reset)
        self.addCleanup(self.http.stop)
        public_key = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        public_key.update(kid="website-test", alg="RS256", use="sig")
        self.http.get(
            f"{ISSUER}/.well-known/openid-configuration",
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize/",
                "token_endpoint": f"{ISSUER}/token/",
                "userinfo_endpoint": f"{ISSUER}/userinfo/",
                "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            },
        )
        self.http.get(f"{ISSUER}/.well-known/jwks.json", json={"keys": [public_key]})
        self.http.add_callback(
            responses.POST, f"{ISSUER}/token/", callback=self.token_response
        )
        self.http.add_callback(
            responses.GET,
            f"{ISSUER}/userinfo/",
            callback=lambda _request: (
                200,
                {"Content-Type": "application/json"},
                json.dumps(self.userinfo),
            ),
        )
        self.userinfo: dict[str, Any] = {
            "sub": "42",
            "preferred_username": "translator",
            "email": "translator@example.com",
            "family_name": "Example Translator",
        }
        self.claims: dict[str, Any] = {}
        self.params: dict[str, list[str]] = {}
        self.verifier = ""

    def token_response(self, request: PreparedRequest) -> tuple[int, dict, str]:
        body = request.body
        if isinstance(body, bytes):
            body = body.decode()
        self.verifier = parse_qs(str(body))["code_verifier"][0]
        now = int(timezone.now().timestamp())
        claims = {
            "iss": ISSUER,
            "sub": "42",
            "aud": "website-test",
            "iat": now,
            "exp": now + 3600,
            "nonce": self.params["nonce"][0],
            "at_hash": base64url_encode(
                sha256(b"test-access-token").digest()[:16]
            ).decode(),
            **self.claims,
        }
        token = jwt.encode(
            claims, self.key, algorithm="RS256", headers={"kid": "website-test"}
        )
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(
                {
                    "access_token": "test-access-token",
                    "token_type": "Bearer",
                    "id_token": token,
                }
            ),
        )

    def begin(self, **data: str) -> dict[str, list[str]]:
        response = self.client.post(reverse("social:begin", args=["weblate"]), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(f"{ISSUER}/authorize/?"))
        self.params = parse_qs(urlsplit(response["Location"]).query)
        return self.params

    def complete(self, **data: str):
        return self.client.get(
            reverse("social:complete", args=["weblate"]),
            {"code": "test-code", "state": self.params["state"][0], **data},
        )

    def create_identity(self, **fields: Any) -> User:
        user = User.objects.create_user(
            username="original", email="original@example.com", **fields
        )
        HostedIdentity.objects.create(
            provider=settings.HOSTED_IDENTITY_PROVIDER, external_id="42", user=user
        )
        return user

    def assert_no_identity(self) -> None:
        self.assertFalse(UserSocialAuth.objects.exists())
        self.assertFalse(HostedIdentity.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_and_csrf(self) -> None:
        client = Client(enforce_csrf_checks=True)
        response = client.get(reverse("social:login"))
        self.assertContains(response, reverse("social:begin", args=["weblate"]))
        self.assertEqual(
            client.get(reverse("social:begin", args=["weblate"])).status_code, 405
        )
        self.assertEqual(
            client.post(reverse("social:begin", args=["weblate"])).status_code, 403
        )
        response = client.post(
            reverse("social:begin", args=["weblate"]),
            {"csrfmiddlewaretoken": client.cookies["csrftoken"].value},
        )
        self.assertEqual(response.status_code, 302)

    @override_settings(LOGIN_URL="/oidc/")
    def test_website_login_uses_oidc(self) -> None:
        response = self.client.get(reverse("user"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"].split("?", 1)[0], "/oidc/")
        self.assertEqual(
            parse_qs(urlsplit(response["Location"]).query)["next"], [reverse("user")]
        )

    @override_settings(LOGIN_URL="/oidc/")
    def test_admin_login_uses_oidc(self) -> None:
        self.assertRedirects(
            self.client.get("/admin/login/?next=/admin/"),
            "/oidc/?next=/admin/",
            fetch_redirect_response=False,
        )

    @override_settings(LOGIN_URL="/admin/login/")
    def test_local_login_override(self) -> None:
        self.assertTemplateUsed(self.client.get("/admin/login/"), "admin/login.html")
        user = User.objects.create_user(
            username="developer",
            password="developer-test-password",  # ruff:ignore[hardcoded-password-func-arg]
            is_staff=True,
        )
        response = self.client.post(
            "/admin/login/",
            {
                "username": user.username,
                "password": "developer-test-password",
                "next": "/admin/",
            },
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_saml_routes_are_removed(self) -> None:
        for path in ("/saml2/login/", "/saml2/metadata/", "/saml2/acs/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_existing_identity_oidc_and_sync_preserve_account(self) -> None:
        user = self.create_identity(password="local-password", is_staff=True)  # ruff:ignore[hardcoded-password-func-arg]
        customer = Customer.objects.create(name="Test", user_id=user.pk)
        customer.owners.add(user)
        original_password = user.password
        params = self.begin(next="/user/")
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertCountEqual(
            params["scope"][0].split(), ["openid", "profile", "email"]
        )
        self.assertEqual(
            params["redirect_uri"], ["http://testserver/oidc/complete/weblate/"]
        )
        response = self.complete()
        self.assertRedirects(response, "/user/", fetch_redirect_response=False)
        self.assertEqual(
            params["code_challenge"][0],
            base64url_encode(sha256(self.verifier.encode()).digest()).decode(),
        )
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertEqual(UserSocialAuth.objects.get().user_id, user.pk)
        self.assertEqual(UserSocialAuth.objects.get().id_key, "sub")
        user.refresh_from_db()
        self.assertEqual(user.username, "translator")
        self.assertEqual(user.last_name, "Example Translator")
        self.assertEqual(user.password, original_password)
        self.assertTrue(user.is_staff)
        self.assertTrue(customer.owners.filter(pk=user.pk).exists())
        synced, created = sync_hosted_identity(
            provider=settings.HOSTED_IDENTITY_PROVIDER,
            external_id="42",
            profile={"username": "renamed"},
        )
        self.assertFalse(created)
        self.assertEqual(synced, user)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(HostedIdentity.objects.count(), 1)

    def test_oidc_sync_and_repeated_login(self) -> None:
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        user = UserSocialAuth.objects.get().user
        self.assertFalse(user.has_usable_password())
        password = user.password
        synced, created = sync_hosted_payload(
            {
                "external_id": "42",
                "profile": {"username": "synced", "email": "new@example.com"},
            }
        )
        self.assertEqual(synced, user)
        self.assertFalse(created)
        self.client.logout()
        self.userinfo.update(preferred_username="synced", email="new@example.com")
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.password, password)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserSocialAuth.objects.count(), 1)

    def test_sync_before_oidc(self) -> None:
        user, _created = sync_hosted_payload(
            {"external_id": "42", "profile": {"username": "synced"}}
        )
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        self.assertEqual(UserSocialAuth.objects.get().user, user)
        self.assertEqual(User.objects.count(), 1)

    def test_sync_push_after_oidc(self) -> None:
        self.begin()
        self.complete()
        user = UserSocialAuth.objects.get().user
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {
                        "external_id": "42",
                        "changes": {"username": "pushed", "active": False},
                    },
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.username, "pushed")
        self.assertFalse(user.is_active)
        self.assertEqual(User.objects.count(), 1)

    def test_bulk_sync_after_oidc(self) -> None:
        self.begin()
        self.complete()
        user = UserSocialAuth.objects.get().user
        payload = {
            "users": [{"external_id": "42", "profile": {"username": "bulk-synced"}}],
            "cursor": "oidc-test-cursor",
        }
        self.http.post(
            settings.HOSTED_USER_SYNC_API,
            json={
                "payload": dumps(
                    payload, key=settings.PAYMENT_SECRET, salt=USER_SYNC_RESPONSE_SALT
                )
            },
        )
        call_command("sync_hosted_users", verbosity=0)
        user.refresh_from_db()
        self.assertEqual(user.username, "bulk-synced")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserSocialAuth.objects.get().user, user)

    def test_profile_collision_does_not_link_by_email(self) -> None:
        user = User.objects.create_user(
            username="translator", email="translator@example.com"
        )
        self.begin()
        self.assertEqual(self.complete().status_code, 409)
        self.assert_no_identity()
        self.assertEqual(User.objects.get(), user)

    def test_username_collision_allocates_new_account(self) -> None:
        existing = User.objects.create_user(
            username="translator", email="other@example.com"
        )
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        user = UserSocialAuth.objects.get().user
        self.assertNotEqual(user, existing)
        self.assertEqual(user.username, "translator-1")

    def test_existing_duplicate_email_is_preserved(self) -> None:
        user = self.create_identity()
        User.objects.create_user(username="duplicate", email=user.email)
        self.userinfo["email"] = user.email
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        self.assertEqual(UserSocialAuth.objects.get().user, user)

    def test_missing_optional_claims_preserve_profile(self) -> None:
        user = self.create_identity(last_name="Original name")
        self.userinfo = {"sub": "42"}
        self.begin()
        self.assertEqual(self.complete().status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.username, "original")
        self.assertEqual(user.email, "original@example.com")
        self.assertEqual(user.last_name, "Original name")

    def test_inactive_user_stays_inactive(self) -> None:
        user = self.create_identity(is_active=False)
        self.begin()
        self.complete()
        self.assertNotIn("_auth_user_id", self.client.session)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_authenticated_account_mismatch(self) -> None:
        original = self.create_identity()
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        self.begin()
        self.assertEqual(self.complete().status_code, 409)
        self.assertEqual(self.client.session["_auth_user_id"], str(other.pk))
        original.refresh_from_db()
        self.assertEqual(original.username, "original")
        self.assertFalse(UserSocialAuth.objects.exists())

    def test_authenticated_unlinked_account_is_not_associated(self) -> None:
        other = User.objects.create_user(
            username="translator", email="translator@example.com"
        )
        self.client.force_login(other)
        self.begin()
        self.assertEqual(self.complete().status_code, 409)
        self.assertFalse(HostedIdentity.objects.exists())
        self.assertFalse(UserSocialAuth.objects.exists())

    def test_conflicting_social_association(self) -> None:
        user = self.create_identity()
        other = User.objects.create_user(username="other")
        social = UserSocialAuth.objects.create(
            provider="weblate", uid="42", id_key="sub", user=other
        )
        self.begin()
        self.assertEqual(self.complete().status_code, 409)
        social.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(social.user, other)
        self.assertEqual(user.username, "original")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_failed_association_rolls_back_user(self) -> None:
        self.begin()
        with patch(
            "weblate_web.oidc.UserSocialAuth.objects.get_or_create",
            side_effect=IntegrityError("conflict"),
        ):
            self.assertEqual(self.complete().status_code, 409)
        self.assert_no_identity()
        self.assertFalse(User.objects.exists())

    def test_racing_association_conflict_rolls_back_profile(self) -> None:
        user = self.create_identity()
        other = User.objects.create_user(username="other")
        social = UserSocialAuth.objects.create(
            provider="weblate", uid="42", id_key="sub", user=other
        )
        self.begin()
        # Simulate a competing association appearing after the first lookup.
        with patch(
            "weblate_web.oidc.UserSocialAuth.get_social_auth", return_value=None
        ):
            self.assertEqual(self.complete().status_code, 409)
        user.refresh_from_db()
        social.refresh_from_db()
        self.assertEqual(user.username, "original")
        self.assertEqual(social.user, other)
        self.assertEqual(HostedIdentity.objects.get().user, user)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_failed_pipeline_rolls_back_both_associations(self) -> None:
        self.begin()
        with (
            patch(
                "social_core.pipeline.social_auth.load_extra_data",
                side_effect=RuntimeError("test failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.complete()
        self.assert_no_identity()
        self.assertFalse(User.objects.exists())

    def test_invalid_state(self) -> None:
        self.begin()
        self.assertEqual(self.complete(state="wrong-state").status_code, 403)
        self.assert_no_identity()

    def test_invalid_nonce(self) -> None:
        self.claims["nonce"] = "wrong-nonce"
        self.begin()
        self.assertEqual(self.complete().status_code, 403)
        self.assert_no_identity()

    def test_invalid_signature(self) -> None:
        self.begin()
        different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encode = jwt.encode
        with patch(
            "weblate_web.test_oidc.jwt.encode",
            side_effect=lambda claims, _key, **kwargs: encode(
                claims, different_key, **kwargs
            ),
        ):
            self.assertEqual(self.complete().status_code, 502)
        self.assert_no_identity()

    def test_invalid_token_claims(self) -> None:
        for claim, value in (
            ("iss", "https://other.example.com"),
            ("aud", "another-client"),
            ("exp", 1),
        ):
            with self.subTest(claim=claim):
                self.claims = {claim: value}
                self.begin()
                self.assertEqual(
                    self.complete().status_code, 403 if claim == "exp" else 502
                )
                self.assert_no_identity()

    def test_userinfo_subject_mismatch(self) -> None:
        self.userinfo["sub"] = "43"
        self.begin()
        self.assertEqual(self.complete().status_code, 502)
        self.assert_no_identity()

    def test_cancelled_login(self) -> None:
        self.begin()
        response = self.complete(
            error="access_denied", error_description="private-provider-detail"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "private-provider-detail", status_code=403)
        self.assert_no_identity()

    def test_provider_timeout(self) -> None:
        self.http.replace(
            responses.POST, f"{ISSUER}/token/", body=Timeout("private-provider-detail")
        )
        self.begin()
        response = self.complete()
        self.assertEqual(response.status_code, 504)
        self.assertNotContains(response, "private-provider-detail", status_code=504)
        self.assert_no_identity()

    def test_external_next_is_rejected(self) -> None:
        self.begin(next="https://other.example.com/")
        self.assertRedirects(self.complete(), "/user/", fetch_redirect_response=False)

    def test_social_auth_migrations(self) -> None:
        call_command("makemigrations", check=True, dry_run=True, verbosity=0)
