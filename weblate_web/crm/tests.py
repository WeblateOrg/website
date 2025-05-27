from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from weblate_web.payments.models import Customer


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
        self.assertRedirects(response, customer1.get_absolute_url())
        self.assertFalse(Customer.objects.filter(pk=customer2.pk).exists())

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
