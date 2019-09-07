from django.contrib.postgres.fields import JSONField
from django.db import models
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from enum import IntEnum
from byro.common.models import LogTargetMixin


class FinTSLogin(models.Model, LogTargetMixin):
    form_title = _('FinTS Login Data')
    LOG_TARGET_BASE = 'byro_fints.login'

    name = models.CharField(
        max_length=100,
        verbose_name=_('Display name'),
        null=False, blank=True,
    )

    blz = models.CharField(
        verbose_name=_('Routing number (BLZ)'),
        max_length=8,
        null=False, blank=False,
    )

    fints_url = models.CharField(
        verbose_name=_('FinTS URL'),
        max_length=256,
        null=False, blank=True,
    )

    @property
    def is_usable(self):
        return bool(self.blz and self.fints_url)


class FinTSUserLogin(models.Model):
    login = models.ForeignKey(
        to='byro_fints.FinTSLogin',
        on_delete=models.CASCADE,
        related_name='user_login',
        blank=True,
    )

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='+',
        blank=True,
    )

    login_name = models.CharField(
        verbose_name=_('Login name/Legitimation ID'),
        max_length=100,
        null=False, blank=False,
    )

    fints_client_data = models.BinaryField(verbose_name='Stored FinTS client data', null=True, blank=True)
    available_tan_media = JSONField(default=list)
    selected_tan_medium = models.CharField(default=None, null=True, blank=True, max_length=32)


class FinTSAccountCapabilities(IntEnum):
    FETCH_TRANSACTIONS = 1
    SEND_TRANSFER = 2
    SEND_TRANSFER_MULTIPLE = 4


class FinTSAccount(models.Model, LogTargetMixin):
    form_title = _('FinTS Account')
    LOG_TARGET_BASE = 'byro_fints.account'

    login = models.ForeignKey(
        to='byro_fints.FinTSLogin',
        on_delete=models.CASCADE,
        related_name='accounts',
        blank=True,
    )

    account = models.OneToOneField(
        to='bookkeeping.Account',
        on_delete=models.SET_NULL,
        related_name='fints_account',
        null=True
    )

    iban = models.CharField(max_length=35, null=False, blank=False)
    bic = models.CharField(max_length=35, null=False, blank=False)
    accountnumber = models.CharField(max_length=35, null=False, blank=False)
    subaccount = models.CharField(max_length=35, null=True, blank=True)
    blz = models.CharField(max_length=35, null=False, blank=False)

    name = models.CharField(
        max_length=100,
        verbose_name=_('Display name'),
        null=False, blank=True,
    )

    last_fetch_date = models.DateField(null=True)

    caps = models.BigIntegerField(default=0)

    def can_fetch_transactions(self):
        return bool(FinTSAccountCapabilities.FETCH_TRANSACTIONS & self.caps)

    def can_send_transfer(self):
        return bool(FinTSAccountCapabilities.SEND_TRANSFER & self.caps)
