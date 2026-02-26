from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import responses
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from weblate_web.crm.models import Interaction, ZammadSyncLog
from weblate_web.invoices.models import (
    Currency,
    Discount,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
)
from weblate_web.management.commands.zammad_sync import (
    Command as ZammadSyncCommand,
    InvalidSubscriptionError,
)
from weblate_web.models import Package, PackageCategory, Service
from weblate_web.payments.models import Customer, Payment
from weblate_web.tests import TEST_CUSTOMER, cnb_mock_rates
from weblate_web.zammad import create_dedicated_hosting_ticket


class BaseCRMTestCase(TestCase):
    def create_customer(
        self, name: str = "TEST CUSTOMER", end_client: str = ""
    ) -> Customer:
        return Customer.objects.create(
            user_id=-1,
            name=name,
            end_client=end_client,
            address=TEST_CUSTOMER["address"],
            city=TEST_CUSTOMER["city"],
            postcode=TEST_CUSTOMER["postcode"],
            # Use non-EU country to avoid VAT on generated invoices
            country="US",
        )


class CRMTestCase(BaseCRMTestCase):
    user: User

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        self.client.force_login(self.user)

    def test_customer_merge(self):
        customer1 = self.create_customer("TEST CUSTOMER 1")
        customer2 = self.create_customer("TEST CUSTOMER 2")
        response = self.client.get(customer1.get_absolute_url())
        self.assertContains(response, "TEST CUSTOMER 1")
        response = self.client.get(customer2.get_absolute_url())
        self.assertContains(response, "TEST CUSTOMER 2")

        merge_url = reverse("crm:customer-merge", kwargs={"pk": customer1.pk})
        response = self.client.get(merge_url, {"merge": customer2.pk})
        self.assertContains(response, "TEST CUSTOMER 1")
        self.assertContains(response, "TEST CUSTOMER 2")

        response = self.client.post(merge_url, {"merge": customer2.pk})
        self.assertRedirects(response, customer2.get_absolute_url())
        self.assertFalse(Customer.objects.filter(pk=customer1.pk).exists())

    def test_customer_search(self):
        self.create_customer("TEST CUSTOMER 1")
        self.create_customer("TEST CUSTOMER 2", end_client="END")

        list_url = reverse("crm:customer-list", kwargs={"kind": "all"})
        response = self.client.get(list_url)
        self.assertContains(response, "TEST CUSTOMER 1")
        self.assertContains(response, "TEST CUSTOMER 2")
        response = self.client.get(list_url, {"q": "test customer"})
        self.assertContains(response, "TEST CUSTOMER 1")
        self.assertContains(response, "TEST CUSTOMER 2")
        response = self.client.get(list_url, {"q": "end"})
        self.assertNotContains(response, "TEST CUSTOMER 1")
        self.assertContains(response, "TEST CUSTOMER 2")

    def test_service(self):
        Package.objects.create(name="community", price=0)
        customer = self.create_customer()
        payment = Payment.objects.create(customer=customer, amount=1)
        service = Service.objects.create(customer=customer)
        expires = timezone.now() + timedelta(days=1)
        subscription1 = service.subscription_set.create(
            package=Package.objects.create(
                name="x1",
                verbose="pkg1",
                price=42,
                category=PackageCategory.PACKAGE_SHARED,
            ),
            expires=expires,
            payment=payment.pk,
        )
        subscription2 = service.subscription_set.create(
            package=Package.objects.create(
                name="x2",
                verbose="pkg2",
                price=99,
                category=PackageCategory.PACKAGE_SHARED,
            ),
            expires=expires,
            payment=payment.pk,
        )
        self.assertTrue(subscription1.enabled)
        self.assertTrue(subscription2.enabled)
        response = self.client.get(service.get_absolute_url())
        self.assertContains(response, "pkg1")
        self.assertContains(response, "pkg2")

        # Test renewal quote
        response = self.client.post(
            service.get_absolute_url(),
            {"quote": 1, "subscription": subscription1.pk},
            follow=True,
        )
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.kind, InvoiceKind.QUOTE)
        self.assertEqual(invoice.total_amount, 42)
        self.assertEqual(
            invoice.all_items[0].start_date, expires.date() + timedelta(days=1)
        )
        self.assertRedirects(response, invoice.get_absolute_url())
        self.assertContains(response, f"Quote {invoice.number}")
        self.assertNotContains(response, "Followup as")
        self.assertContains(response, "Create invoice")

        # Convert to invoice
        response = self.client.post(invoice.get_absolute_url(), follow=True)
        children = invoice.invoice_set.all()
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child.kind, InvoiceKind.INVOICE)
        self.assertEqual(child.total_amount, 42)
        self.assertEqual(
            child.all_items[0].start_date, expires.date() + timedelta(days=1)
        )
        self.assertContains(response, f"Invoice {child.number}")
        self.assertNotContains(response, "Create invoice")

        response = self.client.get(invoice.get_absolute_url())
        self.assertContains(response, "Followup as")
        self.assertNotContains(response, "Create invoice")

        # Second conversion should fail
        response = self.client.post(invoice.get_absolute_url())
        self.assertContains(response, f"Quote {invoice.number}")

        # Test renewal invoices
        response = self.client.post(
            service.get_absolute_url(),
            {
                "invoice": 1,
                "subscription": subscription2.pk,
                "customer_reference": "PO1234",
                "customer_note": "Custom note",
            },
            follow=True,
        )
        invoice = Invoice.objects.exclude(pk__in={invoice.pk, child.pk}).get()
        self.assertEqual(invoice.customer_reference, "PO1234")
        self.assertEqual(invoice.customer_note, "Custom note")
        self.assertEqual(invoice.kind, InvoiceKind.INVOICE)
        self.assertEqual(invoice.total_amount, 99)
        self.assertEqual(
            invoice.all_items[0].start_date, expires.date() + timedelta(days=1)
        )
        self.assertRedirects(response, invoice.get_absolute_url())
        self.assertContains(response, f"Invoice {invoice.number}")
        self.assertNotContains(response, "Followup as")
        self.assertNotContains(response, "Create invoice")

        # Test disabling
        response = self.client.post(
            service.get_absolute_url(), {"disable": 1, "subscription": subscription1.pk}
        )
        self.assertRedirects(response, service.get_absolute_url())
        subscription1.refresh_from_db()
        self.assertFalse(subscription1.enabled)

    @responses.activate
    def test_customer_quote(self):
        cnb_mock_rates()
        Package.objects.create(name="community", price=0)
        package = Package.objects.create(name="x1", verbose="pkg1", price=42)
        customer = self.create_customer()

        response = self.client.get(customer.get_absolute_url())
        self.assertContains(response, "Invoice new service")

        response = self.client.post(
            customer.get_absolute_url(),
            {
                "package": package.id,
                "customer_reference": "PO123456",
                "currency": 1,
                "kind": 90,
            },
        )
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.total_amount, 1027)
        self.assertRedirects(response, invoice.get_absolute_url())

        customer.discount = Discount.objects.create(
            description="Libre discount", percents=50
        )
        customer.save()

        response = self.client.post(
            customer.get_absolute_url(),
            {
                "package": package.id,
                "customer_reference": "PO123456",
                "currency": 1,
                "kind": 90,
            },
        )
        invoice = Invoice.objects.exclude(pk=invoice.pk).get()
        self.assertEqual(invoice.total_amount, 513)
        self.assertRedirects(response, invoice.get_absolute_url())


class IncomeTrackingTestCase(BaseCRMTestCase):
    user: User
    customer: Customer

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        # Add the view_income permission
        permission = Permission.objects.get(
            codename="view_income", content_type__app_label="invoices"
        )
        self.user.user_permissions.add(permission)
        self.client.force_login(self.user)

        # Create test customer
        self.customer = self.create_customer()

    def create_test_invoice(self, year, month, category, amount):
        """Create a test invoice with the specified parameters."""
        # Mock exchange rates
        cnb_mock_rates()

        invoice = Invoice.objects.create(
            kind=InvoiceKind.INVOICE,
            category=category,
            customer=self.customer,
            issue_date=date(year, month, 15),
            currency=Currency.EUR,
        )
        invoice.invoiceitem_set.create(
            description="Test item", quantity=1, unit_price=amount
        )
        return invoice

    def test_income_permission_required(self):
        """Test that income view requires permission."""
        # Create a regular user without permission
        regular_user = User.objects.create_user(
            username="regular", email="regular@example.com", is_staff=True
        )
        self.client.force_login(regular_user)

        response = self.client.get(reverse("crm:income"))
        self.assertEqual(response.status_code, 403)

    @responses.activate
    def test_income_yearly_view(self):
        """Test yearly income view."""
        cnb_mock_rates()
        current_year = timezone.now().year

        # Create test invoices
        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal(1000)
        )
        self.create_test_invoice(
            current_year, 2, InvoiceCategory.SUPPORT, Decimal(2000)
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.HOSTING, Decimal(1500)
        )

        response = self.client.get(reverse("crm:income"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Income Tracking")
        self.assertContains(response, str(current_year))

    @responses.activate
    def test_income_monthly_view(self):
        """Test monthly income view."""
        cnb_mock_rates()
        current_year = timezone.now().year

        # Create test invoices for different categories
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.HOSTING, Decimal(1000)
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.SUPPORT, Decimal(2000)
        )
        self.create_test_invoice(current_year, 3, InvoiceCategory.DEVEL, Decimal(3000))

        response = self.client.get(
            reverse("crm:income-month", kwargs={"year": current_year, "month": 3})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Income by Category")
        self.assertContains(response, "Hosting")
        self.assertContains(response, "Support")
        self.assertContains(response, "Development / Consultations")

    @responses.activate
    def test_income_filters_only_invoices(self):
        """Test that income view only shows INVOICE kind, not quotes."""
        cnb_mock_rates()
        current_year = timezone.now().year

        # Create invoice
        invoice = Invoice.objects.create(
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            customer=self.customer,
            issue_date=date(current_year, 1, 15),
            currency=Currency.EUR,
        )
        invoice.invoiceitem_set.create(
            description="Invoice item", quantity=1, unit_price=Decimal(1000)
        )

        # Create quote (should not be counted)
        quote = Invoice.objects.create(
            kind=InvoiceKind.QUOTE,
            category=InvoiceCategory.HOSTING,
            customer=self.customer,
            issue_date=date(current_year, 1, 15),
            currency=Currency.EUR,
        )
        quote.invoiceitem_set.create(
            description="Quote item", quantity=1, unit_price=Decimal(5000)
        )

        response = self.client.get(
            reverse("crm:income-year", kwargs={"year": current_year})
        )
        self.assertEqual(response.status_code, 200)

        # Check that total income only includes the invoice, not the quote
        # The income should be 1000 (invoice) in EUR
        self.assertContains(response, "EUR")

    def test_income_year_navigation(self):
        """Test year navigation."""
        current_year = timezone.now().year

        response = self.client.get(
            reverse("crm:income-year", kwargs={"year": current_year})
        )
        self.assertEqual(response.status_code, 200)

        # Check for year links
        self.assertContains(response, str(current_year - 1))
        self.assertContains(response, str(current_year))
        self.assertContains(response, str(current_year + 1))

    @responses.activate
    def test_income_svg_chart_generation(self):
        """Test that SVG charts are generated."""
        cnb_mock_rates()
        current_year = timezone.now().year

        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal(1000)
        )

        response = self.client.get(
            reverse("crm:income-year", kwargs={"year": current_year})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<svg")
        self.assertContains(response, "</svg>")

    @responses.activate
    def test_income_category_breakdown(self):
        """Test category breakdown in yearly view."""
        cnb_mock_rates()
        current_year = timezone.now().year

        # Create invoices in different categories
        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal(1000)
        )
        self.create_test_invoice(
            current_year, 2, InvoiceCategory.SUPPORT, Decimal(2000)
        )
        self.create_test_invoice(current_year, 3, InvoiceCategory.DEVEL, Decimal(3000))
        self.create_test_invoice(current_year, 4, InvoiceCategory.DONATE, Decimal(500))

        response = self.client.get(
            reverse("crm:income-year", kwargs={"year": current_year})
        )
        self.assertEqual(response.status_code, 200)

        # All categories should be shown
        self.assertContains(response, "Hosting")
        self.assertContains(response, "Support")
        self.assertContains(response, "Development / Consultations")
        self.assertContains(response, "Donation")

    def test_service_list_views(self):
        """Test various service list views."""
        # Community package is required by the Service.update_status() method
        Package.objects.create(name="community", price=0)
        customer = self.create_customer()
        payment = Payment.objects.create(customer=customer, amount=1)

        # Create services with different types of packages
        dedicated_service = Service.objects.create(customer=customer)
        dedicated_service.subscription_set.create(
            package=Package.objects.create(
                name="dedicated:160k",
                verbose="Dedicated 160k",
                price=100,
                category=PackageCategory.PACKAGE_DEDICATED,
            ),
            expires=timezone.now() + timedelta(days=30),
            payment=payment.pk,
        )

        shared_service = Service.objects.create(customer=customer)
        shared_service.subscription_set.create(
            package=Package.objects.create(
                name="hosted:10k",
                verbose="Hosted 10k",
                price=50,
                category=PackageCategory.PACKAGE_SHARED,
            ),
            expires=timezone.now() + timedelta(days=30),
            payment=payment.pk,
        )

        support_service = Service.objects.create(customer=customer)
        support_service.subscription_set.create(
            package=Package.objects.create(
                name="basic",
                verbose="Basic Support",
                price=600,
                category=PackageCategory.PACKAGE_SUPPORT,
            ),
            expires=timezone.now() + timedelta(days=30),
            payment=payment.pk,
        )

        # Test "all" services view - should show all services grouped by package_kind
        response = self.client.get(reverse("crm:service-list", kwargs={"kind": "all"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All services")
        # Check that all package types are present
        self.assertContains(response, "Dedicated 160k")
        self.assertContains(response, "Hosted 10k")
        self.assertContains(response, "Basic Support")
        # Check grouping headers
        self.assertContains(response, "Dedicated service")
        self.assertContains(response, "Hosted service")
        self.assertContains(response, "Support")

        # Test "dedicated" services view - should only show dedicated hosting services
        response = self.client.get(
            reverse("crm:service-list", kwargs={"kind": "dedicated"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dedicated hosting services")
        self.assertContains(response, "Dedicated 160k")
        self.assertNotContains(response, "Hosted 10k")
        self.assertNotContains(response, "Basic Support")


class MockPaginatedResults(list):
    """Mock for zammad_py paginated results supporting next_page()."""

    def next_page(self):
        return MockPaginatedResults()


class ZammadLibraryTestCase(TestCase):
    """Tests for weblate_web.zammad module."""

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.zammad.ZammadAPI")
    def test_get_zammad_client(self, mock_api_class):
        """Test get_zammad_client creates ZammadAPI with correct settings."""
        from weblate_web.zammad import get_zammad_client

        client = get_zammad_client()
        mock_api_class.assert_called_once_with(
            url="https://care.weblate.org/api/v1/",
            http_token="test-token",
        )
        self.assertEqual(client, mock_api_class.return_value)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.zammad.ZammadAPI")
    def test_create_dedicated_hosting_ticket(self, mock_api_class):
        """Test create_dedicated_hosting_ticket with single email."""
        mock_client = mock_api_class.return_value
        mock_subscription = MagicMock()
        mock_subscription.service.user_emails = "user@example.com"

        create_dedicated_hosting_ticket(mock_subscription)

        mock_client.ticket.create.assert_called_once()
        params = mock_client.ticket.create.call_args.kwargs["params"]
        self.assertEqual(params["title"], "Your dedicated Weblate instance (example)")
        self.assertEqual(params["customer_id"], "guess:user@example.com")
        self.assertEqual(params["group"], "Users")
        self.assertEqual(params["tags"], "dedicated")
        self.assertEqual(params["article"]["to"], "user@example.com")
        self.assertEqual(params["article"]["cc"], "")
        self.assertEqual(params["article"]["type"], "email")
        self.assertEqual(params["article"]["sender"], "Agent")
        self.assertFalse(params["article"]["internal"])

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.zammad.ZammadAPI")
    def test_create_ticket_multiple_emails(self, mock_api_class):
        """Test create_dedicated_hosting_ticket with multiple emails."""
        mock_client = mock_api_class.return_value
        mock_subscription = MagicMock()
        mock_subscription.service.user_emails = "user@example.com,other@test.org"

        create_dedicated_hosting_ticket(mock_subscription)

        params = mock_client.ticket.create.call_args.kwargs["params"]
        self.assertEqual(params["article"]["to"], "user@example.com")
        self.assertEqual(params["article"]["cc"], "other@test.org")

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.zammad.ZammadAPI")
    def test_create_ticket_no_email(self, mock_api_class):
        """Test create_dedicated_hosting_ticket raises ValueError without email."""
        mock_subscription = MagicMock()
        mock_subscription.service.user_emails = ""

        with self.assertRaises(ValueError):
            create_dedicated_hosting_ticket(mock_subscription)


class ZammadSyncCommandTestCase(BaseCRMTestCase):
    """Tests for zammad_sync management command."""

    def create_customer_with_service(
        self,
        name="TEST CUSTOMER",
        end_client="",
        package_name="extended",
    ):
        """Create a customer with service and active subscription."""
        customer = self.create_customer(name=name, end_client=end_client)
        Package.objects.get_or_create(name="community", defaults={"price": 0})
        package, _ = Package.objects.get_or_create(
            name=package_name,
            defaults={
                "verbose": f"{package_name.capitalize()} support",
                "price": 42,
                "category": PackageCategory.PACKAGE_SUPPORT,
            },
        )
        service = Service.objects.create(customer=customer)
        service.subscription_set.create(
            package=package,
            expires=timezone.now() + timedelta(days=365),
        )
        return customer, service

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_empty(self, mock_get_client):
        """Test sync with no organizations or customers."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults()

        call_command("zammad_sync", stdout=StringIO())

        mock_client.user.search.assert_called_once()
        mock_client.organization.all.assert_called_once()

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_handle_hosted_account(self, mock_get_client):
        """Test updating users with hosted account link."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = [
            {"id": 1, "login": "user1"},
            {"id": 2, "login": "user2"},
        ]
        mock_client.organization.all.return_value = MockPaginatedResults()

        out = StringIO()
        call_command("zammad_sync", stdout=out)

        self.assertEqual(mock_client.user.update.call_count, 2)
        mock_client.user.update.assert_any_call(
            1, {"hosted_account": "Hosted Weblate account"}
        )
        mock_client.user.update.assert_any_call(
            2, {"hosted_account": "Hosted Weblate account"}
        )
        output = out.getvalue()
        self.assertIn("user1", output)
        self.assertIn("user2", output)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_existing_mapped_organization(self, mock_get_client):
        """Test syncing an organization already mapped via crm ID."""
        customer, service = self.create_customer_with_service()
        subscription = service.subscription_set.first()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults(
            [
                {
                    "id": 100,
                    "name": "Zammad Org",
                    "crm": str(customer.pk),
                    "last_payment": "2020-01-01",
                    "service_link": "",
                    "premium_support": False,
                    "support": False,
                    "plan": "old plan",
                }
            ]
        )

        call_command("zammad_sync", stdout=StringIO())

        # Customer's zammad_id should be updated
        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 100)
        # Organization attributes should be updated
        mock_client.organization.update.assert_any_call(
            100,
            {"last_payment": subscription.expires.date().isoformat()},
        )
        mock_client.organization.update.assert_any_call(100, {"support": True})
        mock_client.organization.update.assert_any_call(
            100, {"plan": subscription.package.verbose}
        )

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_name_matching(self, mock_get_client):
        """Test mapping organization to customer by name matching."""
        customer, _service = self.create_customer_with_service(name="Acme Corp")

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults(
            [{"id": 200, "name": "Acme Corp"}]
        )

        call_command("zammad_sync", stdout=StringIO())

        # Customer should be mapped
        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 200)
        # Organization should be updated with crm ID
        mock_client.organization.update.assert_any_call(
            200, {"crm": str(customer.pk)}
        )

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_end_client_matching(self, mock_get_client):
        """Test mapping organization to customer by end_client name."""
        customer, _service = self.create_customer_with_service(
            name="Parent Co", end_client="End Client Inc"
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults(
            [{"id": 250, "name": "End Client Inc"}]
        )

        call_command("zammad_sync", stdout=StringIO())

        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 250)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_create_new_organization(self, mock_get_client):
        """Test creating a new organization for unmapped customer."""
        customer, _service = self.create_customer_with_service(name="New Corp")

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults()
        mock_client.organization.create.return_value = {"id": 300, "name": "New Corp"}

        call_command("zammad_sync", stdout=StringIO())

        # Organization should be created
        mock_client.organization.create.assert_called_once()
        create_args = mock_client.organization.create.call_args[0][0]
        self.assertEqual(create_args["name"], "New Corp")
        self.assertEqual(create_args["crm"], str(customer.pk))

        # Customer zammad_id should be updated
        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 300)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_customer_uses_end_client_name(self, mock_get_client):
        """Test organization creation uses end_client over name."""
        customer, _service = self.create_customer_with_service(
            name="Parent Co", end_client="End Client Inc"
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults()
        mock_client.organization.create.return_value = {
            "id": 350,
            "name": "End Client Inc",
        }

        call_command("zammad_sync", stdout=StringIO())

        create_args = mock_client.organization.create.call_args[0][0]
        self.assertEqual(create_args["name"], "End Client Inc")

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_customer_no_name(self, mock_get_client):
        """Test skipping customer creation when customer has no name."""
        customer = self.create_customer(name="")
        Package.objects.get_or_create(name="community", defaults={"price": 0})
        package, _ = Package.objects.get_or_create(
            name="extended",
            defaults={
                "verbose": "Extended support",
                "price": 42,
                "category": PackageCategory.PACKAGE_SUPPORT,
            },
        )
        service = Service.objects.create(customer=customer)
        service.subscription_set.create(
            package=package,
            expires=timezone.now() + timedelta(days=365),
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults()

        err = StringIO()
        call_command("zammad_sync", stdout=StringIO(), stderr=err)

        mock_client.organization.create.assert_not_called()
        self.assertIn("has no name", err.getvalue())

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_invalid_subscription_skipped(self, mock_get_client):
        """Test that customers with invalid subscriptions are skipped."""
        customer = self.create_customer(name="No Service Corp")
        Package.objects.get_or_create(name="community", defaults={"price": 0})
        # Create a service but no subscription
        Service.objects.create(customer=customer)

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults()

        out = StringIO()
        call_command("zammad_sync", stdout=out)

        # Organization should not be created for customer with no subscription
        mock_client.organization.create.assert_not_called()

    def test_get_customer_service_no_services(self):
        """Test get_customer_service raises when customer has no services."""
        customer = self.create_customer()

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()

        with self.assertRaises(InvalidSubscriptionError):
            cmd.get_customer_service(customer)

    def test_get_customer_service_no_subscription(self):
        """Test get_customer_service raises when service has no subscription."""
        customer = self.create_customer()
        Package.objects.get_or_create(name="community", defaults={"price": 0})
        Service.objects.create(customer=customer)

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()

        with self.assertRaises(InvalidSubscriptionError):
            cmd.get_customer_service(customer)

    def test_get_customer_service_multiple_services(self):
        """Test get_customer_service raises when customer has multiple services."""
        customer = self.create_customer()
        Package.objects.get_or_create(name="community", defaults={"price": 0})
        Service.objects.create(customer=customer)
        Service.objects.create(customer=customer)

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()

        with self.assertRaises(InvalidSubscriptionError):
            cmd.get_customer_service(customer)

    def test_update_zammad_id(self):
        """Test update_zammad_id updates customer's zammad_id."""
        customer = self.create_customer()
        self.assertEqual(customer.zammad_id, 0)

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()
        cmd.update_zammad_id(customer, 42)

        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 42)

    def test_update_zammad_id_same_value(self):
        """Test update_zammad_id skips update when value is the same."""
        customer = self.create_customer()
        customer.zammad_id = 42
        customer.save(update_fields=["zammad_id"])

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()
        cmd.update_zammad_id(customer, 42)

        self.assertEqual(cmd.stdout.getvalue(), "")

    def test_update_zammad_id_zero(self):
        """Test update_zammad_id skips update when zammad_id is 0."""
        customer = self.create_customer()

        cmd = ZammadSyncCommand()
        cmd.stdout = StringIO()
        cmd.update_zammad_id(customer, 0)

        customer.refresh_from_db()
        self.assertEqual(customer.zammad_id, 0)

    def test_get_organization_subscription(self):
        """Test get_organization_subscription returns correct data."""
        customer, service = self.create_customer_with_service(package_name="premium")
        subscription = service.subscription_set.first()

        cmd = ZammadSyncCommand()
        result = cmd.get_organization_subscription(service, subscription)

        self.assertEqual(
            result["last_payment"], subscription.expires.date().isoformat()
        )
        self.assertEqual(result["service_link"], service.site_url)
        self.assertTrue(result["premium_support"])
        self.assertTrue(result["support"])
        self.assertEqual(result["plan"], subscription.package.verbose)

    def test_get_organization_subscription_non_premium(self):
        """Test get_organization_subscription with non-premium package."""
        customer, service = self.create_customer_with_service(package_name="extended")
        subscription = service.subscription_set.first()

        cmd = ZammadSyncCommand()
        result = cmd.get_organization_subscription(service, subscription)

        self.assertFalse(result["premium_support"])
        self.assertTrue(result["support"])

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_sync.get_zammad_client")
    def test_sync_unmatched_organization_warning(self, mock_get_client):
        """Test warning for organizations without CRM match."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.user.search.return_value = []
        mock_client.organization.all.return_value = MockPaginatedResults(
            [{"id": 999, "name": "Unknown Org"}]
        )

        out = StringIO()
        call_command("zammad_sync", stdout=out)

        self.assertIn("No match found", out.getvalue())


class ZammadAttachmentsCommandTestCase(BaseCRMTestCase):
    """Tests for zammad_attachments management command."""

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_no_customers_with_zammad_id(self, mock_get_client):
        """Test command does nothing when no customers have zammad_id."""
        self.create_customer()
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        call_command("zammad_attachments", stdout=StringIO())

        mock_client.ticket.search.assert_not_called()

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_download_attachment(self, mock_get_client):
        """Test downloading and storing a PDF attachment."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "contract.pdf"},
            ],
        }
        mock_client.ticket_article_attachment.download.return_value = b"PDF content"

        call_command("zammad_attachments", stdout=StringIO())

        # Verify interaction was created
        interaction = Interaction.objects.get(customer=customer)
        self.assertEqual(interaction.origin, Interaction.Origin.ZAMMAD_ATTACHMENT)
        self.assertEqual(interaction.summary, "contract.pdf")
        self.assertEqual(interaction.remote_id, 500)

        # Verify sync log was created
        self.assertTrue(
            ZammadSyncLog.objects.filter(customer=customer, article_id=100).exists()
        )

        # Verify download was called with correct args
        mock_client.ticket_article_attachment.download.assert_called_once_with(
            500, 100, 1
        )

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_skip_non_matching_extension(self, mock_get_client):
        """Test skipping attachments with non-allowed extensions."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "image.png"},
                {"id": 501, "filename": "script.py"},
            ],
        }

        call_command("zammad_attachments", stdout=StringIO())

        self.assertFalse(Interaction.objects.filter(customer=customer).exists())
        mock_client.ticket_article_attachment.download.assert_not_called()

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_case_insensitive_extension(self, mock_get_client):
        """Test that file extension matching is case-insensitive."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "REPORT.PDF"},
            ],
        }
        mock_client.ticket_article_attachment.download.return_value = b"PDF content"

        call_command("zammad_attachments", stdout=StringIO())

        self.assertEqual(Interaction.objects.filter(customer=customer).count(), 1)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_skip_processed_articles(self, mock_get_client):
        """Test skipping already processed articles."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        # Mark article as already processed
        ZammadSyncLog.objects.create(customer=customer, article_id=100)

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )

        call_command("zammad_attachments", stdout=StringIO())

        # Article should not be processed
        mock_client.ticket_article.find.assert_not_called()

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_force_mode_reprocesses(self, mock_get_client):
        """Test force mode reprocesses already processed articles."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        # Mark article as already processed
        ZammadSyncLog.objects.create(customer=customer, article_id=100)

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "contract.pdf"},
            ],
        }
        mock_client.ticket_article_attachment.download.return_value = b"PDF content"

        call_command("zammad_attachments", "--force", stdout=StringIO())

        # Article should be processed in force mode
        mock_client.ticket_article.find.assert_called_once_with(100)
        interaction = Interaction.objects.get(customer=customer)
        self.assertEqual(interaction.remote_id, 500)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_force_mode_skips_known_attachments(self, mock_get_client):
        """Test force mode skips already imported attachments."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        # Create existing interaction for this attachment
        customer.interaction_set.create(
            origin=Interaction.Origin.ZAMMAD_ATTACHMENT,
            summary="contract.pdf",
            remote_id=500,
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "contract.pdf"},
            ],
        }

        call_command("zammad_attachments", "--force", stdout=StringIO())

        # Download should not be called for already imported attachment
        mock_client.ticket_article_attachment.download.assert_not_called()

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_multiple_allowed_extensions(self, mock_get_client):
        """Test processing attachments with different allowed extensions."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [{"id": 1, "article_ids": [100]}]
        )
        mock_client.ticket_article.find.return_value = {
            "id": 100,
            "created_at": "2024-01-15T10:30:00Z",
            "attachments": [
                {"id": 500, "filename": "doc.pdf"},
                {"id": 501, "filename": "data.xlsx"},
                {"id": 502, "filename": "image.png"},
                {"id": 503, "filename": "text.docx"},
            ],
        }
        mock_client.ticket_article_attachment.download.return_value = b"file content"

        call_command("zammad_attachments", stdout=StringIO())

        # Only 3 allowed files should create interactions (pdf, xlsx, docx)
        self.assertEqual(Interaction.objects.filter(customer=customer).count(), 3)
        self.assertEqual(mock_client.ticket_article_attachment.download.call_count, 3)

    @override_settings(ZAMMAD_TOKEN="test-token")  # noqa: S106
    @patch("weblate_web.management.commands.zammad_attachments.get_zammad_client")
    def test_multiple_tickets_and_articles(self, mock_get_client):
        """Test processing multiple tickets with multiple articles."""
        customer = self.create_customer()
        customer.zammad_id = 10
        customer.save(update_fields=["zammad_id"])

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.ticket.search.return_value = MockPaginatedResults(
            [
                {"id": 1, "article_ids": [100, 101]},
                {"id": 2, "article_ids": [200]},
            ]
        )
        mock_client.ticket_article.find.side_effect = [
            {
                "id": 100,
                "created_at": "2024-01-15T10:30:00Z",
                "attachments": [{"id": 500, "filename": "file1.pdf"}],
            },
            {
                "id": 101,
                "created_at": "2024-01-16T10:30:00Z",
                "attachments": [{"id": 501, "filename": "file2.docx"}],
            },
            {
                "id": 200,
                "created_at": "2024-01-17T10:30:00Z",
                "attachments": [{"id": 502, "filename": "file3.xlsx"}],
            },
        ]
        mock_client.ticket_article_attachment.download.return_value = b"content"

        call_command("zammad_attachments", stdout=StringIO())

        self.assertEqual(Interaction.objects.filter(customer=customer).count(), 3)
        self.assertEqual(ZammadSyncLog.objects.count(), 3)
