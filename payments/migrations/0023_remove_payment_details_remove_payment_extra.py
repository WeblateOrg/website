# Generated by Django 4.2.2 on 2023-08-08 11:18

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0022_jsonfield"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="payment",
            name="details",
        ),
        migrations.RemoveField(
            model_name="payment",
            name="extra",
        ),
    ]