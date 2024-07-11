# Generated by Django 5.0.6 on 2024-07-11 12:45

import django.db.models.deletion
from django.db import migrations, models


def update_links(apps, schema_editor):
    Package = apps.get_model("weblate_web", "Package")
    Subscription = apps.get_model("weblate_web", "Subscription")

    packages = {package.name: package for package in Package.objects.all()}

    subscriptions = list(Subscription.objects.all())

    for subscription in subscriptions:
        subscription.package_link = packages[subscription.package]

    Subscription.objects.bulk_update(subscriptions, ["package_link"])


class Migration(migrations.Migration):
    dependencies = [
        (
            "weblate_web",
            "0027_alter_donation_payment_alter_pastpayments_payment_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="package_link",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="weblate_web.package",
            ),
        ),
        migrations.RunPython(update_links, migrations.RunPython.noop, elidable=True),
        migrations.AlterField(
            model_name="subscription",
            name="package_link",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="weblate_web.package"
            ),
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="package",
        ),
        migrations.RenameField(
            model_name="subscription",
            old_name="package_link",
            new_name="package",
        ),
    ]
