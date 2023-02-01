# Generated by Django 1.11.16 on 2018-11-06 15:22

from django.db import migrations

import payments.utils


class Migration(migrations.Migration):
    dependencies = [("payments", "0007_customer_tax")]

    operations = [
        migrations.AlterModelOptions(
            name="payment", options={"ordering": ["-created"]}
        ),
        migrations.RenameField(
            model_name="payment", old_name="processor", new_name="backend"
        ),
        migrations.AlterField(
            model_name="payment",
            name="details",
            field=payments.utils.JSONField(default={}),
        ),
        migrations.AlterField(
            model_name="payment",
            name="extra",
            field=payments.utils.JSONField(default={}),
        ),
    ]
