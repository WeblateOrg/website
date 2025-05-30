# Generated by Django 5.2.1 on 2025-05-27 09:53

import django.core.files.storage
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("CRM", "0002_alter_interaction_options_interaction_user_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="interaction",
            name="attachment",
            field=models.FileField(
                storage=django.core.files.storage.FileSystemStorage(
                    location=settings.CRM_ROOT
                ),
                upload_to="attachments",
                verbose_name="Attachment",
            ),
        ),
    ]
