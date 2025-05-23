# Generated by Django 5.1.3 on 2024-12-18 10:36

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0041_customer_created"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="name",
            field=models.CharField(
                db_index=True,
                default="",
                max_length=200,
                verbose_name="Company or individual name",
            ),
        ),
    ]
