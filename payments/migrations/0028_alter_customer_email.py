# Generated by Django 5.0.6 on 2024-07-18 11:46

from django.db import migrations, models

import payments.utils


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0027_payment_card_info"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="email",
            field=models.EmailField(
                blank=True,
                help_text="Additional e-mail to receive billing notifications",
                max_length=190,
                validators=[payments.utils.validate_email],
                verbose_name="Billing e-mail",
            ),
        ),
    ]
