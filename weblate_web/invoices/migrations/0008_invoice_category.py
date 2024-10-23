# Generated by Django 5.1.2 on 2024-10-23 13:36

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0007_invoice_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="category",
            field=models.IntegerField(
                choices=[
                    (1, "Hosting"),
                    (2, "Support"),
                    (3, "Development"),
                    (4, "Donation"),
                ],
                default=1,
            ),
        ),
    ]