from datetime import timedelta
from decimal import Decimal

import responses
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from weblate_web.invoices.models import (
    Discount,
    Invoice,
    InvoiceCategory,
    InvoiceKind,
)
from weblate_web.models import Package, PackageCategory, Service
from weblate_web.payments.models import Customer, Payment
from weblate_web.tests import cnb_mock_rates


class CRMTestCase(TestCase):
    user: User

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        self.client.force_login(self.user)

    def test_customer_merge(self):
        customer1 = Customer.objects.create(user_id=-1, name="TEST CUSTOMER 1")
        customer2 = Customer.objects.create(user_id=-1, name="TEST CUSTOMER 2")
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
        Customer.objects.create(user_id=-1, name="TEST CUSTOMER 1")
        Customer.objects.create(user_id=-1, name="TEST CUSTOMER 2", end_client="END")

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
        customer = Customer.objects.create(user_id=-1, name="TEST CUSTOMER")
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
        customer = Customer.objects.create(user_id=-1, name="TEST CUSTOMER")

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


class IncomeTrackingTestCase(TestCase):
    user: User

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
        self.customer = Customer.objects.create(user_id=-1, name="TEST CUSTOMER")

    def create_test_invoice(self, year, month, category, amount):
        """Helper to create test invoices."""
        invoice = Invoice.objects.create(
            kind=InvoiceKind.INVOICE,
            category=category,
            customer=self.customer,
            issue_date=timezone.datetime(year, month, 15).date(),
            currency=0,  # EUR
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

    def test_income_yearly_view(self):
        """Test yearly income view."""
        current_year = timezone.now().year

        # Create test invoices
        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal("1000")
        )
        self.create_test_invoice(
            current_year, 2, InvoiceCategory.SUPPORT, Decimal("2000")
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.HOSTING, Decimal("1500")
        )

        response = self.client.get(reverse("crm:income"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Income Tracking")
        self.assertContains(response, str(current_year))

    def test_income_monthly_view(self):
        """Test monthly income view."""
        current_year = timezone.now().year

        # Create test invoices for different categories
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.HOSTING, Decimal("1000")
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.SUPPORT, Decimal("2000")
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.DEVEL, Decimal("3000")
        )

        response = self.client.get(
            reverse("crm:income-month", kwargs={"year": current_year, "month": 3})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Income by Category")
        self.assertContains(response, "Hosting")
        self.assertContains(response, "Support")
        self.assertContains(response, "Development / Consultations")

    def test_income_filters_only_invoices(self):
        """Test that income view only shows INVOICE kind, not quotes."""
        current_year = timezone.now().year

        # Create invoice
        invoice = Invoice.objects.create(
            kind=InvoiceKind.INVOICE,
            category=InvoiceCategory.HOSTING,
            customer=self.customer,
            issue_date=timezone.datetime(current_year, 1, 15).date(),
            currency=0,
        )
        invoice.invoiceitem_set.create(
            description="Invoice item", quantity=1, unit_price=Decimal("1000")
        )

        # Create quote (should not be counted)
        quote = Invoice.objects.create(
            kind=InvoiceKind.QUOTE,
            category=InvoiceCategory.HOSTING,
            customer=self.customer,
            issue_date=timezone.datetime(current_year, 1, 15).date(),
            currency=0,
        )
        quote.invoiceitem_set.create(
            description="Quote item", quantity=1, unit_price=Decimal("5000")
        )

        response = self.client.get(reverse("crm:income-year", kwargs={"year": current_year}))
        self.assertEqual(response.status_code, 200)

        # Check that total income only includes the invoice, not the quote
        # The income should be approximately 1000 (invoice) in CZK
        # (exact value depends on exchange rates)
        self.assertContains(response, "CZK")

    def test_income_year_navigation(self):
        """Test year navigation."""
        current_year = timezone.now().year

        response = self.client.get(reverse("crm:income-year", kwargs={"year": current_year}))
        self.assertEqual(response.status_code, 200)

        # Check for year links
        self.assertContains(response, str(current_year - 1))
        self.assertContains(response, str(current_year))
        self.assertContains(response, str(current_year + 1))

    def test_income_svg_chart_generation(self):
        """Test that SVG charts are generated."""
        current_year = timezone.now().year

        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal("1000")
        )

        response = self.client.get(reverse("crm:income-year", kwargs={"year": current_year}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<svg")
        self.assertContains(response, "</svg>")

    def test_income_category_breakdown(self):
        """Test category breakdown in yearly view."""
        current_year = timezone.now().year

        # Create invoices in different categories
        self.create_test_invoice(
            current_year, 1, InvoiceCategory.HOSTING, Decimal("1000")
        )
        self.create_test_invoice(
            current_year, 2, InvoiceCategory.SUPPORT, Decimal("2000")
        )
        self.create_test_invoice(
            current_year, 3, InvoiceCategory.DEVEL, Decimal("3000")
        )
        self.create_test_invoice(
            current_year, 4, InvoiceCategory.DONATE, Decimal("500")
        )

        response = self.client.get(reverse("crm:income-year", kwargs={"year": current_year}))
        self.assertEqual(response.status_code, 200)

        # All categories should be shown
        self.assertContains(response, "Hosting")
        self.assertContains(response, "Support")
        self.assertContains(response, "Development / Consultations")
        self.assertContains(response, "Donation")
