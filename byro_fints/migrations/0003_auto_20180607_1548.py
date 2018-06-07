# -*- coding: utf-8 -*-
# Generated by Django 1.11.13 on 2018-06-07 15:48
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('byro_fints', '0002_fintsaccount_last_fetch_date'),
    ]

    operations = [
        migrations.AlterField(
            model_name='fintsaccount',
            name='account',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='fints_account', to='bookkeeping.Account'),
        ),
    ]