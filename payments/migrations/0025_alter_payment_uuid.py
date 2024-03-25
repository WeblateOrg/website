# Generated by Django 4.2.7 on 2024-03-25 15:50

import uuid

from django.db import migrations

import payments.models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0001_squashed_0024_rename_details_new_payment_details_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="uuid",
            field=payments.models.Char32UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
    ]
