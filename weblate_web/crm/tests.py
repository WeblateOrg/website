from datetime import timedelta

import responses
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from weblate_web.invoices.models import Discount, Invoice, InvoiceKind
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

    def test_service_list_views(self):
        """Test various service list views."""
        # Community package is required by the Service.update_status() method
        Package.objects.create(name="community", price=0)
        customer = Customer.objects.create(user_id=-1, name="TEST CUSTOMER")
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
