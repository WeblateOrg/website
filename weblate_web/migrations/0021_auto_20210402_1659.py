# Generated by Django 3.1.5 on 2021-04-02 14:59

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("weblate_web", "0020_auto_20210322_1423"),
    ]

    operations = [
        migrations.AlterField(
            model_name="post",
            name="milestone",
            field=models.BooleanField(
                blank=True,
                db_index=True,
                default=False,
                help_text="Important milestone, shown in the milestones archive",
            ),
        ),
    ]
