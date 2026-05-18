#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

from django.db import migrations, models
from django.utils.translation import gettext_lazy, pgettext_lazy

from weblate_web.models import DONATION_REWARD_PACKAGE_NAMES, REWARD_LEVELS

SERVICE_KIND_DONATION = 10
PACKAGE_CATEGORY_DONATION = 40


def get_donation_package_verbose(reward: int, reward_labels: dict[int, str]) -> str:
    if reward:
        return f"Weblate donation: {reward_labels[reward]}"
    return "Weblate donation"


def get_donation_packages(donation_model, package_model) -> dict[int, models.Model]:
    reward_labels = dict(
        donation_model._meta.get_field("reward").choices  # pylint: disable=protected-access
    )
    packages = {}
    for reward, name in DONATION_REWARD_PACKAGE_NAMES.items():
        package, _created = package_model.objects.get_or_create(
            name=name,
            defaults={
                "category": PACKAGE_CATEGORY_DONATION,
                "verbose": get_donation_package_verbose(reward, reward_labels),
                "price": REWARD_LEVELS[reward],
            },
        )
        packages[reward] = package
    return packages


def migrate_donations(apps, schema_editor) -> None:
    donation_model = apps.get_model("weblate_web", "Donation")
    package_model = apps.get_model("weblate_web", "Package")
    past_payments_model = apps.get_model("weblate_web", "PastPayments")
    service_model = apps.get_model("weblate_web", "Service")
    subscription_model = apps.get_model("weblate_web", "Subscription")

    donation_packages = get_donation_packages(donation_model, package_model)

    for donation in donation_model.objects.order_by("pk"):
        service = service_model.objects.create(
            customer_id=donation.customer_id,
            kind=SERVICE_KIND_DONATION,
            donation_link_text=donation.link_text,
            donation_link_url=donation.link_url,
            donation_link_image=donation.link_image,
            donation_legacy_id=donation.pk,
            created=donation.created,
        )
        service_model.objects.filter(pk=service.pk).update(created=donation.created)
        subscription = subscription_model.objects.create(
            service_id=service.pk,
            package_id=donation_packages[donation.reward].pk,
            payment=donation.payment,
            created=donation.created,
            expires=donation.expires,
            enabled=donation.active,
        )
        subscription_model.objects.filter(pk=subscription.pk).update(
            created=donation.created
        )
        past_payments_model.objects.filter(donation_id=donation.pk).update(
            subscription_id=subscription.pk
        )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("weblate_web", "0048_service_maintenance_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="kind",
            field=models.IntegerField(
                choices=[
                    (0, pgettext_lazy("Service kind", "Service")),
                    (10, pgettext_lazy("Service kind", "Donation")),
                ],
                default=0,
            ),
        ),
        migrations.AddField(
            model_name="service",
            name="donation_link_text",
            field=models.CharField(
                blank=True, max_length=200, verbose_name=gettext_lazy("Link text")
            ),
        ),
        migrations.AddField(
            model_name="service",
            name="donation_link_url",
            field=models.URLField(blank=True, verbose_name=gettext_lazy("Link URL")),
        ),
        migrations.AddField(
            model_name="service",
            name="donation_link_image",
            field=models.ImageField(
                blank=True,
                upload_to="donations/",
                verbose_name=gettext_lazy("Link image"),
            ),
        ),
        migrations.AddField(
            model_name="service",
            name="donation_legacy_id",
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="package",
            name="category",
            field=models.IntegerField(
                choices=[
                    (0, pgettext_lazy("Package category", "None")),
                    (10, pgettext_lazy("Package category", "Dedicated hosting")),
                    (20, pgettext_lazy("Package category", "Shared hosting")),
                    (30, pgettext_lazy("Package category", "Self-hosted support")),
                    (40, pgettext_lazy("Package category", "Donation")),
                ],
                default=0,
            ),
        ),
        migrations.RunPython(migrate_donations, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="pastpayments",
            name="donation",
        ),
        migrations.DeleteModel(
            name="Donation",
        ),
    ]
