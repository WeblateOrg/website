# Create your tests here.
from weblate_web.payments.models import Customer
from weblate_web.tests import UserTestCase

from .models import Agreement, AgreementKind


class InvoiceTestCase(UserTestCase):
    def create_customer(self, *, vat: str = "") -> Customer:
        return Customer.objects.create(
            name="Zkušební zákazník",
            address="Street 42",
            city="City",
            postcode="424242",
            country="cz",
            user_id=-1,
            vat=vat,
        )

    def test_agreement(self):
        agreement = Agreement.objects.create(
            customer=self.create_customer(), kind=AgreementKind.DPA
        )
        self.assertTrue(agreement.path.exists())
        self.assertIn("DPA", str(agreement))
