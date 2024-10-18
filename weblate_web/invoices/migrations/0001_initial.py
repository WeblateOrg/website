# Generated by Django 5.1.2 on 2024-10-18 09:05

import datetime

import django.core.validators
import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("payments", "0028_alter_customer_email"),
    ]

    operations = [
        migrations.CreateModel(
            name="Discount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("description", models.CharField(max_length=200, unique=True)),
                (
                    "percents",
                    models.IntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(99),
                        ]
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Invoice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("sequence", models.IntegerField(editable=False)),
                ("issue_date", models.DateField(default=datetime.date.today)),
                ("due_date", models.DateField(blank=True)),
                (
                    "kind",
                    models.IntegerField(
                        choices=[
                            (10, "Invoice"),
                            (20, "Proforma"),
                            (30, "Quote"),
                            (40, "Draft"),
                        ],
                        default=10,
                    ),
                ),
                ("customer_reference", models.CharField(blank=True, max_length=100)),
                ("vat_rate", models.IntegerField(default=0)),
                ("currency", models.IntegerField(choices=[(0, "EUR")], default=0)),
                (
                    "prepaid",
                    models.BooleanField(
                        default=False,
                        help_text="Invoices paid in advance (card payment, pro forma)",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="payments.customer",
                    ),
                ),
                (
                    "discount",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="invoices.discount",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="invoices.invoice",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="InvoiceItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("description", models.CharField(max_length=200)),
                (
                    "quantity",
                    models.IntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(50),
                        ],
                    ),
                ),
                (
                    "quantity_unit",
                    models.IntegerField(choices=[(0, ""), (1, "hours")], default=0),
                ),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=7)),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="invoices.invoice",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                django.db.models.functions.datetime.Extract("issue_date", "year"),
                models.F("kind"),
                models.F("sequence"),
                name="unique_number",
            ),
        ),
    ]