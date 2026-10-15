#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from weblate_web.categories import ServiceCategory, category_from_metadata
from weblate_web.crm.views import IncomeView
from weblate_web.invoices.models import Currency, InvoiceKind
from weblate_web.invoices.models import Invoice as CurrentInvoice
from weblate_web.models import Package as CurrentPackage
from weblate_web.models import Subscription as CurrentSubscription
from weblate_web.models import get_packages
from weblate_web.payments.backends import get_backend
from weblate_web.payments.models import Customer as CurrentCustomer
from weblate_web.payments.models import Payment as CurrentPayment


class CategoryMetadataTest(SimpleTestCase):
    def test_transaction_metadata(self) -> None:
        packages = {"hosted:10k": 20, "dedicated:160k": 10, "install:linux": 2}
        for extra, expected in (
            ({"subscription": "hosted:10k"}, ServiceCategory.SHARED),
            ({"subscription": 42}, ServiceCategory.DEDICATED),
            (
                {"subscription_upgrade": 42, "package": "hosted:10k"},
                ServiceCategory.SHARED,
            ),
            ({"package": "install:linux"}, ServiceCategory.SUPPORT),
            ({"plan": 1}, ServiceCategory.SHARED),
            ({"plan": "dedicated:160k"}, ServiceCategory.DEDICATED),
            ({"reward": 1}, ServiceCategory.DONATE),
            ({"category": "donate"}, ServiceCategory.DONATE),
            ({"subscription_upgrade": 42}, None),
            ({"package": "missing", "subscription": 42}, None),
            ({"subscription": 999}, None),
            ({"subscription": True}, None),
            ({"plan": "unknown"}, None),
            ({"plan": True}, None),
            ({}, None),
            ([], None),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(
                    category_from_metadata(extra, packages, {42: 10}), expected
                )


class CategoryMigrationTest(TransactionTestCase):
    def test_credit_notes_follow_backfilled_invoice_categories(self) -> None:
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        previous = [
            ("weblate_web", "0054_unique_hosted_billing_service"),
            ("invoices", "0004_alter_invoice_options_invoice_correction_net_and_more"),
            ("payments", "0009_alter_payment_state"),
        ]
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            Package = apps.get_model("weblate_web", "Package")
            Invoice = apps.get_model("invoices", "Invoice")
            InvoiceItem = apps.get_model("invoices", "InvoiceItem")
            customer = apps.get_model("payments", "Customer").objects.create(
                origin="test", user_id=-1
            )
            expected = {}
            for index, (old_category, category, refund) in enumerate(
                ((20, 20, 100), (10, 10, 40), (30, 2, 100), (40, 4, 100), (0, 1, 100))
            ):
                package = Package.objects.create(
                    name=f"package-{index}", price=100, category=old_category
                )
                invoice = Invoice.objects.create(
                    customer=customer,
                    sequence=index * 2 + 1,
                    kind=10,
                    category=1,
                    issue_date=date(2025, 1, 1),
                    due_date=date(2025, 1, 1),
                    tax_date=date(2025, 1, 1),
                    extra={"subscription": package.name},
                )
                InvoiceItem.objects.create(invoice=invoice, unit_price=100)
                correction = Invoice.objects.create(
                    customer=customer,
                    sequence=index * 2 + 2,
                    kind=10,
                    category=1,
                    issue_date=date(2025, 1, 1),
                    due_date=date(2025, 1, 1),
                    tax_date=date(2025, 1, 1),
                    correction_of=invoice,
                    correction_net=-refund,
                    extra={"correction": {"exchange_rate_czk": "25"}},
                )
                InvoiceItem.objects.create(invoice=correction, unit_price=-refund)
                expected[invoice.pk] = category
                expected[correction.pk] = category
        finally:
            MigrationExecutor(connection).migrate(leaves)

        self.assertEqual(
            dict(CurrentInvoice.objects.values_list("pk", "category")), expected
        )
        income = IncomeView().get_income_data(2025)
        self.assertEqual(income[ServiceCategory.SHARED], 0)
        self.assertEqual(income[ServiceCategory.DEDICATED], 60)
        self.assertEqual(income[ServiceCategory.SUPPORT], 0)
        self.assertEqual(income[ServiceCategory.DONATE], 0)
        self.assertEqual(income[ServiceCategory.HOSTING], 0)

    def test_categories_and_historical_metadata(self) -> None:  # ruff:ignore[too-many-locals]
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        previous = [
            ("weblate_web", "0054_unique_hosted_billing_service"),
            ("invoices", "0004_alter_invoice_options_invoice_correction_net_and_more"),
            ("payments", "0009_alter_payment_state"),
        ]
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            Package = apps.get_model("weblate_web", "Package")
            Invoice = apps.get_model("invoices", "Invoice")
            InvoiceItem = apps.get_model("invoices", "InvoiceItem")
            Payment = apps.get_model("payments", "Payment")
            Customer = apps.get_model("payments", "Customer")
            Service = apps.get_model("weblate_web", "Service")
            Subscription = apps.get_model("weblate_web", "Subscription")
            packages = {
                name: Package.objects.create(
                    name=name, verbose=name, price=100, category=category
                )
                for name, category in (
                    ("shared", 20),
                    ("dedicated", 10),
                    ("support", 30),
                    ("donation", 40),
                    ("install:linux", 0),
                    ("unknown", 0),
                )
            }
            customer = Customer.objects.create(origin="test", user_id=-1)
            service = Service.objects.create(customer=customer)
            subscription = Subscription.objects.create(
                service=service, package=packages["dedicated"], expires=timezone.now()
            )
            cases: tuple[
                tuple[Any, list[str | None], list[dict[str, Any]], int], ...
            ] = (
                ({}, ["shared"], [], 20),
                ({}, ["dedicated"], [], 10),
                ({}, ["install:linux"], [], 2),
                ({"subscription": "shared"}, [None], [], 20),
                ({"subscription": "support"}, [], [], 2),
                ({"subscription": "donation"}, [], [], 4),
                ({"subscription": subscription.pk}, [], [], 10),
                (
                    {"subscription_upgrade": subscription.pk, "package": "shared"},
                    [],
                    [],
                    20,
                ),
                ({"subscription_upgrade": subscription.pk}, [], [], 1),
                ({}, [None], [{"subscription": "dedicated"}], 10),
                ({}, [], [{"package": "shared"}], 20),
                ({}, [], [{"plan": 1}], 20),
                ({}, [], [{"reward": 1}], 4),
                ({"subscription": subscription.pk}, [], [{"package": "shared"}], 20),
                ({"package": "dedicated"}, [], [{"package": "shared"}], 10),
                ({}, [], [{"package": "shared"}, {"package": "dedicated"}], 1),
                ({"package": "shared"}, ["shared", "dedicated"], [], 1),
                ({"package": "dedicated"}, ["shared", None], [], 1),
                ({"subscription": "missing"}, [], [], 1),
                ({"subscription": 999999}, [], [], 1),
                ({"plan": "unknown"}, [], [], 1),
                ([], [], [], 1),
                ({}, [], [], 1),
            )
            expected = {}
            for sequence, (extra, items, payments, category) in enumerate(cases, 1):
                invoice = Invoice.objects.create(
                    customer=customer,
                    sequence=sequence,
                    kind=10,
                    category=1,
                    due_date=date(2025, 1, 1),
                    tax_date=date(2025, 1, 1),
                    extra=extra,
                )
                expected[invoice.pk] = category
                for name in items:
                    InvoiceItem.objects.create(
                        invoice=invoice,
                        package=packages[name] if name is not None else None,
                        unit_price=100,
                    )
                for index, metadata in enumerate(payments):
                    Payment.objects.create(
                        customer=customer,
                        amount=100,
                        description="Historical payment",
                        extra=metadata,
                        **{"paid_invoice" if index == 0 else "draft_invoice": invoice},
                    )
            # Explicit pre-existing non-hosting classifications must be preserved.
            preserved = Invoice.objects.create(
                customer=customer,
                sequence=len(cases) + 1,
                kind=10,
                category=3,
                due_date=date(2025, 1, 1),
                tax_date=date(2025, 1, 1),
                extra={"package": "shared"},
            )
            expected[preserved.pk] = 3
        finally:
            MigrationExecutor(connection).migrate(leaves)

        self.assertEqual(
            dict(CurrentInvoice.objects.values_list("pk", "category")), expected
        )
        self.assertEqual(
            dict(CurrentPackage.objects.values_list("name", "category")),
            {
                "shared": 20,
                "dedicated": 10,
                "support": 2,
                "donation": 4,
                "install:linux": 2,
                "unknown": 0,
            },
        )


class CategoryInvoiceTest(TestCase):
    def test_package_invoice_categories_and_installation(self) -> None:
        customer = CurrentCustomer.objects.create(origin="test", user_id=-1)
        self.assertEqual(
            next(
                category
                for category, _, name, _, _ in get_packages()
                if name == "install:linux"
            ),
            ServiceCategory.SUPPORT,
        )
        for category in ServiceCategory:
            with self.subTest(category=category):
                package = CurrentPackage.objects.create(
                    name=f"package-{category}", price=100, category=category
                )
                invoice = CurrentSubscription._create_invoice(  # pylint: disable=protected-access
                    kind=InvoiceKind.DRAFT,
                    customer=customer,
                    package=package,
                    customer_reference="",
                    currency=Currency.EUR,
                    extra={},
                )
                self.assertEqual(invoice.category, category)

    def test_payment_invoice_metadata(self) -> None:
        customer = CurrentCustomer.objects.create(origin="test", user_id=-1)
        CurrentPackage.objects.create(name="dedicated:160k", price=100, category=10)
        for extra, category in (
            ({"plan": 1}, ServiceCategory.SHARED),
            ({"package": "dedicated:160k"}, ServiceCategory.DEDICATED),
            ({"category": "donate"}, ServiceCategory.DONATE),
            ({}, ServiceCategory.HOSTING),
        ):
            with (
                self.subTest(extra=extra),
                patch.object(CurrentInvoice, "generate_files"),
            ):
                payment = CurrentPayment.objects.create(
                    customer=customer, amount=100, description="Payment", extra=extra
                )
                get_backend("pay")(payment).generate_invoice(proforma=True)
                payment.refresh_from_db()
                assert payment.draft_invoice is not None
                self.assertEqual(payment.draft_invoice.category, category)
