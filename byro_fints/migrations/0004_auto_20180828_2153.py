# Generated by Django 2.1 on 2018-08-28 21:53

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("byro_fints", "0003_auto_20180607_1548"),
    ]

    operations = [
        migrations.CreateModel(
            name="FinTSUserLogin",
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
                    "login_name",
                    models.CharField(
                        max_length=100, verbose_name="Login name/Legitimation ID"
                    ),
                ),
                (
                    "fints_client_data",
                    models.BinaryField(
                        blank=True, null=True, verbose_name="Stored FinTS client data"
                    ),
                ),
            ],
        ),
        migrations.RemoveField(
            model_name="fintslogin",
            name="login_name",
        ),
        migrations.AlterField(
            model_name="fintsaccount",
            name="subaccount",
            field=models.CharField(blank=True, max_length=35, null=True),
        ),
        migrations.AddField(
            model_name="fintsuserlogin",
            name="login",
            field=models.ForeignKey(
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="user_login",
                to="byro_fints.FinTSLogin",
            ),
        ),
        migrations.AddField(
            model_name="fintsuserlogin",
            name="user",
            field=models.ForeignKey(
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
