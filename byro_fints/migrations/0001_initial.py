# -*- coding: utf-8 -*-
# Generated by Django 1.11.13 on 2018-06-06 15:11
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('bookkeeping', '0011_auto_20180303_1745'),
    ]

    operations = [
        migrations.CreateModel(
            name='FinTSAccount',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('iban', models.CharField(max_length=35)),
                ('bic', models.CharField(max_length=35)),
                ('accountnumber', models.CharField(max_length=35)),
                ('subaccount', models.CharField(max_length=35)),
                ('blz', models.CharField(max_length=35)),
                ('account', models.OneToOneField(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='fints_account', to='bookkeeping.Account')),
            ],
        ),
        migrations.CreateModel(
            name='FinTSLogin',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, max_length=100, verbose_name='Display name')),
                ('blz', models.CharField(max_length=8, verbose_name='Routing number (BLZ)')),
                ('login_name', models.CharField(max_length=100, verbose_name='Login name/Legitimation ID')),
                ('fints_url', models.CharField(blank=True, max_length=256, verbose_name='FinTS URL')),
            ],
        ),
        migrations.AddField(
            model_name='fintsaccount',
            name='login',
            field=models.ForeignKey(blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='accounts', to='byro_fints.FinTSLogin'),
        ),
    ]
