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


def get_missing_payments(model, field: str, valid_payments):
    return (
        model.objects.exclude(**{f"{field}__isnull": True})
        .exclude(**{f"{field}__in": valid_payments})
        .values_list(field, flat=True)
        .distinct()
    )


def format_missing_payments(missing_payments) -> str:
    payment_ids = [str(payment_id) for payment_id in missing_payments[:11]]
    suffix = ", ..." if len(payment_ids) > 10 else ""
    return f"{', '.join(payment_ids[:10])}{suffix}"


def validate_payment_references(
    subscription_model, past_payments_model, valid_payments
) -> None:
    missing_current = get_missing_payments(
        subscription_model, "payment", valid_payments
    )
    missing_past = get_missing_payments(past_payments_model, "payment", valid_payments)
    messages = []
    if missing_current.exists():
        messages.append(
            "Subscription.payment references missing Payment rows: "
            f"{format_missing_payments(missing_current)}"
        )
    if missing_past.exists():
        messages.append(
            "PastPayments.payment references missing Payment rows: "
            f"{format_missing_payments(missing_past)}"
        )
    if messages:
        message = "; ".join(messages)
        raise ValueError(
            f"Can not migrate subscription payment references. {message}. "
            "Restore the missing Payment rows or remove the orphaned references first."
        )


def validate_payment_references_migration(apps, schema_editor) -> None:
    payment_model = apps.get_model("payments", "Payment")
    past_payments_model = apps.get_model("weblate_web", "PastPayments")
    subscription_model = apps.get_model("weblate_web", "Subscription")
    valid_payments = payment_model.objects.values_list("pk", flat=True)
    validate_payment_references(subscription_model, past_payments_model, valid_payments)


def migrate_payment_relations(apps, schema_editor) -> None:
    payment_model = apps.get_model("payments", "Payment")
    past_payments_model = apps.get_model("weblate_web", "PastPayments")
    subscription_model = apps.get_model("weblate_web", "Subscription")

    valid_payments = payment_model.objects.values_list("pk", flat=True)
    subscription_past_payment_model = apps.get_model(
        "weblate_web", "SubscriptionPastPayment"
    )
    subscription_model.objects.filter(payment__in=valid_payments).update(
        new_payment_id=models.F("payment")
    )

    batch = []
    past_payments = past_payments_model.objects.filter(
        subscription_id__isnull=False,
        payment__in=valid_payments,
    ).values_list("subscription_id", "payment")
    for subscription_id, payment_id in past_payments.iterator(chunk_size=1000):
        batch.append(
            subscription_past_payment_model(
                subscription_id=subscription_id,
                payment_id=payment_id,
            )
        )
        if len(batch) == 1000:
            subscription_past_payment_model.objects.bulk_create(
                batch, batch_size=1000, ignore_conflicts=True
            )
            batch = []
    if batch:
        subscription_past_payment_model.objects.bulk_create(
            batch, batch_size=1000, ignore_conflicts=True
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0057_customer_upcoming_payment_notification_days"),
        ("weblate_web", "0049_consolidate_donations"),
    ]

    operations = [
        migrations.RunPython(
            validate_payment_references_migration,
            migrations.RunPython.noop,
            elidable=True,
        ),
        migrations.AddField(
            model_name="subscription",
            name="new_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.RESTRICT,
                to="payments.payment",
            ),
        ),
        migrations.CreateModel(
            name="SubscriptionPastPayment",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "payment",
                    models.ForeignKey(
                        on_delete=models.deletion.RESTRICT,
                        to="payments.payment",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        to="weblate_web.subscription",
                    ),
                ),
            ],
            options={
                "verbose_name": "Past subscription payment",
                "verbose_name_plural": "Past subscription payments",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("subscription", "payment"),
                        name="unique_subscription_past_payment",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="subscription",
            name="past_payments",
            field=models.ManyToManyField(
                blank=True,
                related_name="past_subscription_set",
                to="payments.payment",
                through="weblate_web.SubscriptionPastPayment",
            ),
        ),
        migrations.RunPython(
            migrate_payment_relations, migrations.RunPython.noop, elidable=True
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="payment",
        ),
        migrations.RenameField(
            model_name="subscription",
            old_name="new_payment",
            new_name="payment",
        ),
        migrations.DeleteModel(
            name="PastPayments",
        ),
    ]
