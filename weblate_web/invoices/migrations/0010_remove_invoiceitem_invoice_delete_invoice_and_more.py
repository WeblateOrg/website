# Generated by Django 5.1.2 on 2024-10-24 06:24

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0009_alter_invoice_kind"),
        ("payments", "0034_remove_payment_draft_invoice_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="invoiceitem",
            name="invoice",
        ),
        migrations.DeleteModel(
            name="Invoice",
        ),
        migrations.DeleteModel(
            name="InvoiceItem",
        ),
    ]
