from __future__ import annotations

from typing import cast
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from weblate_web.admin import PostAdmin
from weblate_web.admin_app import CustomAdminConfig
from weblate_web.admin_site import UserAutocompleteJsonView
from weblate_web.invoices.admin import InvoiceAdmin
from weblate_web.invoices.models import (
    Invoice,
    InvoiceCategory,
    InvoiceKind,
)
from weblate_web.models import Post
from weblate_web.payments.models import Customer


class AdminSiteTestCase(TestCase):
    """Tests for CustomAdminSite and UserAutocompleteJsonView."""

    def test_custom_admin_site_default_site(self) -> None:
        self.assertEqual(
            CustomAdminConfig.default_site,
            "weblate_web.admin_site.CustomAdminSite",
        )

    def test_user_autocomplete_serialize_user(self) -> None:
        user = User.objects.create_user(
            username="autocomplete_test",
            email="auto@example.com",
            first_name="John",
            last_name="Doe",
        )
        view = UserAutocompleteJsonView()
        result = view.serialize_result(user, "pk")
        self.assertEqual(result["text"], "John Doe <auto@example.com>")

    def test_user_autocomplete_serialize_non_user(self) -> None:
        customer = Customer.objects.create(
            name="Test Customer",
            address="Street 1",
            city="City",
            postcode="12345",
            country="cz",
            user_id=-1,
        )
        view = UserAutocompleteJsonView()
        result = view.serialize_result(customer, "pk")
        # Non-User objects should use default text (str representation)
        self.assertEqual(result["text"], str(customer))

    def test_custom_admin_site_autocomplete_view(self) -> None:
        User.objects.create_superuser(username="admin", email="admin@example.com")
        self.client.force_login(User.objects.get(username="admin"))
        response = self.client.get(
            "/admin/autocomplete/",
            {
                "app_label": "payments",
                "model_name": "customer",
                "field_name": "users",
                "term": "admin",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data.get("results", [])
        # Check that User results use the custom format
        self.assertGreater(len(results), 0)
        self.assertIn("<", results[0]["text"])


class PostAdminTestCase(TestCase):
    """Tests for PostAdmin.save_model() custom behavior."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        self.client.force_login(self.user)
        self.factory = RequestFactory()

    def test_save_model_auto_assigns_author(self) -> None:
        """When author is None, save_model should assign request.user."""
        post = Post(
            title="Test Post",
            slug="test-post",
            body="Test body",
            timestamp=timezone.now(),
        )
        request = self.factory.get("/")
        request.user = self.user

        post_admin = PostAdmin(Post, admin.site)
        post_admin.save_model(request, post, form=None, change=False)

        self.assertEqual(post.author, self.user)
        self.assertIsNotNone(post.pk)

    def test_save_model_preserves_existing_author(self) -> None:
        """When author is already set, save_model should not change it."""
        other_user = User.objects.create_user(
            username="other", email="other@example.com"
        )
        post = Post(
            title="Test Post 2",
            slug="test-post-2",
            body="Test body",
            timestamp=timezone.now(),
            author=other_user,
        )
        request = self.factory.get("/")
        request.user = self.user

        post_admin = PostAdmin(Post, admin.site)
        post_admin.save_model(request, post, form=None, change=True)

        self.assertEqual(post.author, other_user)


class InvoiceAdminTestCase(TestCase):
    """Tests for InvoiceAdmin custom methods."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        self.client.force_login(self.user)
        self.factory = RequestFactory()
        self.invoice_admin = InvoiceAdmin(Invoice, admin.site)

    def create_customer(self) -> Customer:
        return Customer.objects.create(
            name="Test Customer",
            address="Street 42",
            city="City",
            postcode="424242",
            country="cz",
            user_id=-1,
        )

    def create_invoice(
        self, kind: InvoiceKind = InvoiceKind.INVOICE, **kwargs
    ) -> Invoice:
        return Invoice.objects.create(
            customer=self.create_customer(),
            kind=kind,
            category=InvoiceCategory.HOSTING,
            **kwargs,
        )

    def make_mock_form(self, invoice: Invoice):
        return type(
            "MockForm", (), {"instance": invoice, "save_m2m": lambda _self: None}
        )()

    def test_has_delete_permission_always_false(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        invoice = self.create_invoice()
        self.assertFalse(self.invoice_admin.has_delete_permission(request, invoice))
        self.assertFalse(self.invoice_admin.has_delete_permission(request))

    def test_has_change_permission_no_obj(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        self.assertFalse(self.invoice_admin.has_change_permission(request, obj=None))

    def test_has_change_permission_editable(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        invoice = self.create_invoice()
        # A current-month invoice should be editable
        self.assertEqual(
            self.invoice_admin.has_change_permission(request, invoice),
            invoice.is_editable(),
        )

    def test_has_change_permission_draft(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        invoice = self.create_invoice(kind=InvoiceKind.DRAFT)
        # A draft with no children should be editable
        self.assertTrue(self.invoice_admin.has_change_permission(request, invoice))

    def test_get_readonly_fields_no_obj(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        fields = self.invoice_admin.get_readonly_fields(request, obj=None)
        self.assertIn("number", fields)
        self.assertIn("prepaid", fields)
        self.assertNotIn("kind", fields)
        self.assertNotIn("issue_date", fields)

    def test_get_readonly_fields_with_obj(self) -> None:
        request = self.factory.get("/")
        request.user = self.user
        invoice = self.create_invoice()
        fields = self.invoice_admin.get_readonly_fields(request, obj=invoice)
        self.assertIn("number", fields)
        self.assertIn("prepaid", fields)
        self.assertIn("kind", fields)
        self.assertIn("issue_date", fields)

    def test_view_on_site_invoice(self) -> None:
        invoice = self.create_invoice(kind=InvoiceKind.INVOICE)
        url = self.invoice_admin.view_on_site(invoice)
        self.assertIsNotNone(url)
        self.assertIn(str(invoice.pk), cast("str", url))

    def test_view_on_site_draft(self) -> None:
        invoice = self.create_invoice(kind=InvoiceKind.DRAFT)
        url = self.invoice_admin.view_on_site(invoice)
        # Draft invoices have no PDF, so no download URL
        self.assertIsNone(url)

    @patch.object(Invoice, "generate_files")
    def test_save_related_non_draft(self, mock_generate) -> None:
        """save_related should call generate_files for non-draft invoices."""
        invoice = self.create_invoice(kind=InvoiceKind.INVOICE)
        invoice.invoiceitem_set.create(description="Test item", unit_price=100)

        request = self.factory.get("/")
        request.user = self.user

        form = self.make_mock_form(invoice)
        self.invoice_admin.save_related(request, form, formsets=[], change=True)

        mock_generate.assert_called_once()

    @patch.object(Invoice, "generate_files")
    def test_save_related_draft(self, mock_generate) -> None:
        """save_related should NOT call generate_files for draft invoices."""
        invoice = self.create_invoice(kind=InvoiceKind.DRAFT)
        invoice.invoiceitem_set.create(description="Test item", unit_price=100)

        request = self.factory.get("/")
        request.user = self.user

        form = self.make_mock_form(invoice)
        self.invoice_admin.save_related(request, form, formsets=[], change=True)

        mock_generate.assert_not_called()

    @patch.object(Invoice, "generate_files")
    def test_save_related_negative_amount_sets_prepaid(self, mock_generate) -> None:
        """save_related should set prepaid=True for negative amounts (refunds)."""
        invoice = self.create_invoice(kind=InvoiceKind.INVOICE)
        invoice.invoiceitem_set.create(description="Refund", unit_price=-100)

        request = self.factory.get("/")
        request.user = self.user

        form = self.make_mock_form(invoice)
        self.invoice_admin.save_related(request, form, formsets=[], change=True)

        invoice.refresh_from_db()
        self.assertTrue(invoice.prepaid)
        mock_generate.assert_called_once()

    @patch.object(Invoice, "generate_files")
    def test_save_related_positive_amount_no_prepaid(self, mock_generate) -> None:
        """save_related should not set prepaid for positive amounts."""
        invoice = self.create_invoice(kind=InvoiceKind.INVOICE)
        invoice.invoiceitem_set.create(description="Service", unit_price=100)

        request = self.factory.get("/")
        request.user = self.user

        form = self.make_mock_form(invoice)
        self.invoice_admin.save_related(request, form, formsets=[], change=True)

        invoice.refresh_from_db()
        self.assertFalse(invoice.prepaid)
        mock_generate.assert_called_once()
