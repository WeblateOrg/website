# Generated by Django 3.2.16 on 2023-01-18 09:04

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0022_alter_project_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="enabled",
            field=models.BooleanField(blank=True, default=True),
        ),
    ]