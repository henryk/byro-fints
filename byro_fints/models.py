from django.db import models
from django.utils.translation import ugettext_lazy as _


class FinTSLogin(models.Model):
    form_title = _('FinTS Login Data')

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

    login_name = models.CharField(
        verbose_name=_('Login name/Legitimation ID'),
        max_length=100,
        null=False, blank=False,
    )

    fints_url = models.CharField(
        verbose_name=_('FinTS URL'),
        max_length=256,
        null=False, blank=True,
    )

    @property
    def is_usable(self):
        return bool(self.blz and self.login_name and self.fints_url)


class FinTSAccount(models.Model):
    form_title = _('FinTS Account')

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
    subaccount = models.CharField(max_length=35, null=False, blank=False)
    blz = models.CharField(max_length=35, null=False, blank=False)

    last_fetch_date = models.DateField(null=True)
