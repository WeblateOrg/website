# Create your tests here.
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from django.core.management import call_command
from django.test.utils import override_settings

from weblate_web.payments.models import Customer
from weblate_web.tests import SIGNATURE_MOCK_SETTINGS, UserTestCase

from .models import Agreement, AgreementKind


class LegalTestCase(UserTestCase):
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

    @override_settings(**SIGNATURE_MOCK_SETTINGS)
    def test_agreement(self) -> None:
        agreement = Agreement.objects.create(
            customer=self.create_customer(), kind=AgreementKind.DPA
        )
        self.assertTrue(agreement.path.exists())
        self.assertIn("DPA", str(agreement))

    def test_generate_terms(self) -> None:
        tempdir = mkdtemp()
        try:
            call_command("generate_terms", output=Path(tempdir))
        finally:
            rmtree(tempdir)
