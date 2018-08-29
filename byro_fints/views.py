from datetime import date
from contextlib import contextmanager

from django import forms
from django.db import transaction
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import CreateView, FormView, ListView, TemplateView
from django.views.generic.detail import SingleObjectMixin
from django.contrib import messages
from fints.client import FinTS3PinTanClient, FinTSOperations
from fints.exceptions import *
from fints.models import SEPAAccount
from mt940 import models as mt940_models
import fints.parser
fints.parser.robust_mode = True

from byro.bookkeeping.models import Account, Transaction, Booking

from .data import get_bank_information_by_blz
from .models import FinTSAccount, FinTSLogin

PIN_CACHED_SENTINEL = '******'
def _cache_label(fints_login):
    return 'byro_fints__pin__{}__cache'.format(fints_login.pk)


class FinTSClientFormMixin:
    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_login = self.get_object()
        if isinstance(fints_login, FinTSAccount):
            fints_login = fints_login.login
        fints_user_login, _ignore = fints_login.user_login.get_or_create(user=self.request.user)
        form.fields['login_name'].initial = fints_user_login.login_name
        form.fields['pin'].label = _('PIN for \'{display_name}\'').format(
            display_name=fints_login.name
        )
        if _cache_label(fints_login) in self.request.securebox:
            form.fields['pin'].initial = PIN_CACHED_SENTINEL
        return form

    @contextmanager
    def fints_client(self, fints_login, form=None):
        fints_user_login, _ignore = fints_login.user_login.get_or_create(user=self.request.user)
        if form:
            fints_user_login.login_name = form.cleaned_data['login_name']
            if form.cleaned_data['pin'] == PIN_CACHED_SENTINEL:
                pin = self.request.securebox[_cache_label(fints_login)]
            else:
                pin = form.cleaned_data['pin']
        else:
            pin = None

        client = FinTS3PinTanClient(
            fints_login.blz,
            fints_user_login.login_name,
            pin,
            fints_login.fints_url,
            set_data=bytes(fints_user_login.fints_client_data) if fints_user_login.fints_client_data else None,
        )
        client.add_response_callback(self.fints_callback)

        try:
            yield client
            pin_correct = True

        except FinTSClientPINError:
            # PIN wrong, clear cached PIN, indicate error
            self.request.securebox.delete_value(_cache_label(fints_login))
            if form:
                form.add_error(None, "Can't establish FinTS dialog: Username/PIN wrong?")
            pin_correct = False

        if pin_correct and form:
            fints_user_login.fints_client_data = client.get_data(including_private=True)
            fints_user_login.save()

            if form.cleaned_data['pin'] != PIN_CACHED_SENTINEL:
                self.request.securebox.store_value(_cache_label(fints_login), form.cleaned_data['pin'])

    def fints_callback(self, segment, response):
        if response.code.startswith('0'):
            messages.info(self.request, "{} \u2014 {}".format(response.code, response.text))
        elif response.code.startswith('9'):
            messages.error(self.request, "{} \u2014 {}".format(response.code, response.text))
        elif response.code.startswith('0'):
            messages.warning(self.request, "{} \u2014 {}".format(response.code, response.text))


class Dashboard(ListView):
    template_name = 'byro_fints/dashboard.html'
    queryset = FinTSLogin.objects.order_by('blz').all()
    context_object_name = "fints_logins"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['fints_accounts'] = FinTSAccount.objects.order_by('iban').all()
        return context


class FinTSLoginCreateView(CreateView):
    template_name = 'byro_fints/login_add.html'
    model = FinTSLogin
    form_class = forms.modelform_factory(FinTSLogin, fields=['blz', 'name', 'fints_url'])
    success_url = reverse_lazy('plugins:byro_fints:finance.fints.dashboard')

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        for name, field in form.fields.items():
            if name in ['name', 'fints_url']:
                field.required = False
        return form

    def form_valid(self, form):
        bank_information = get_bank_information_by_blz(form.instance.blz)

        form.instance.fints_url = form.instance.fints_url or bank_information.get('PIN/TAN URL', '')
        form.instance.name = form.instance.name or bank_information.get('Institut', '')

        if not form.instance.fints_url:
            raise forms.ValidationError(_("FinTS URL could not be looked up automatically, please fill it in manually."))
        return super().form_valid(form)


class PinRequestForm(forms.Form):
    form_name = _("PIN request")
    login_name = forms.CharField(label=_("Login name"))
    pin = forms.CharField(label=_("PIN"), widget=forms.PasswordInput(render_value=True))


class FinTSLoginRefreshView(SingleObjectMixin, FinTSClientFormMixin, FormView):
    template_name = 'byro_fints/login_refresh.html'
    form_class = PinRequestForm
    success_url = reverse_lazy('plugins:byro_fints:finance.fints.dashboard')
    model = FinTSLogin
    context_object_name = 'fints_login'

    @property
    def object(self):
        return self.get_object()  # FIXME: WTF?  Apparently I'm supposed to implement a get()/post() that sets self.object?

    def form_valid(self, form):
        fints_login = self.get_object()
        with self.fints_client(fints_login, form) as client:
            with client:
                accounts = client.get_sepa_accounts()

        if form.errors:
            return super().form_invalid(form)

        for account in accounts:
            FinTSAccount.objects.get_or_create(
                login=fints_login,
                **account._asdict()
            )
            # FIXME: Create accounts in bookeeping?

        return super().form_valid(form)


# FIXME: Allow inline create
# FIXME: Name of default accounts?
class FinTSAccountLinkView(SingleObjectMixin, FormView):
    template_name = 'byro_fints/account_link.html'
    success_url = reverse_lazy('plugins:byro_fints:finance.fints.dashboard')

    model = FinTSAccount
    context_object_name = 'fints_account'

    @property
    def object(self):
        return self.get_object()

    def get_form_class(self):
        class LinkForm(forms.Form):
            existing_account = forms.ChoiceField(
                choices=[(a.pk, a.name) for a in Account.objects.all() if not hasattr(a, 'fints_account')],
                initial=self.object.account.pk if self.object.account else None,
            )
        return LinkForm

    def form_valid(self, form):
        account = self.get_object()
        account.account = Account.objects.get(pk=form.cleaned_data['existing_account'])
        account.save()
        return super().form_valid(form)


class FinTSAccountInformationView(SingleObjectMixin, FinTSClientFormMixin, TemplateView):
    template_name = 'byro_fints/account_information.html'
    model = FinTSAccount
    context_object_name = 'fints_account'

    @property
    def object(self):
        return self.get_object()

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        fints_account = self.get_object()
        with self.fints_client(fints_account.login) as client:
            context['information'] = client.get_information()
        for account in context['information']['accounts']:
            if (account['iban'] == fints_account.iban) or (account['account_number'] == fints_account.accountnumber and account['subaccount_number'] == fints_account.subaccount):
                context['account_information'] = account
                break
            else:
                context['account_information'] = None
        context['OPERATIONS'] = list(FinTSOperations)
        return context


class PinRequestAndDateForm(PinRequestForm):
    fetch_from_date = forms.DateField(label=_("Fetch start date"), required=True)


class FinTSAccountFetchView(SingleObjectMixin, FinTSClientFormMixin, FormView):
    template_name = 'byro_fints/account_fetch.html'
    form_class = PinRequestAndDateForm
    success_url = reverse_lazy('plugins:byro_fints:finance.fints.dashboard')
    model = FinTSAccount
    context_object_name = 'fints_account'

    @property
    def object(self):
        return self.get_object()

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_account = self.get_object()
        form.fields['fetch_from_date'].initial = fints_account.last_fetch_date or date.today().replace(day=1, month=1)
        # FIXME Check for plus/minus 1 day
        return form

    @transaction.atomic
    def form_valid(self, form):
        fints_account = self.get_object()

        sepa_account = SEPAAccount(
            **{
                name: getattr(fints_account, name)
                for name in SEPAAccount._fields
            }
        )

        with self.fints_client(fints_account.login, form) as client:
            with client:
                transactions = client.get_statement(sepa_account, form.cleaned_data['fetch_from_date'], date.today())

        if form.errors:
            return super().form_invalid(form)

        for t in transactions:
            originator = "{} {} {}".format(
                t.data.get('applicant_name') or '',
                t.data.get('applicant_bin') or '',
                t.data.get('applicant_iban') or '',
            )
            purpose = "{} {} | {}".format(
                t.data.get('purpose') or '',
                t.data.get('additional_purpose') or '',
                t.data.get('posting_text') or '',
            )

            # Handle JSON "But I cant't serialize that?!" nonsense
            mt940_data = dict()
            for k, v in t.data.items():
                if isinstance(v, mt940_models.Amount):
                    v = {
                        'amount': str(v.amount),
                        'currency': v.currency,
                    }
                elif isinstance(v, mt940_models.Date):
                    v = v.isoformat()
                
                mt940_data[k] = v

            amount = t.data.get('amount').amount
            if amount < 0:
                amount = -amount
                status = 'D'
            else:
                status = 'C'

            data = dict(
                mt940_data=mt940_data,
                other_party=originator,
            )

            args = dict(
                booking_datetime=t.data.get('entry_date'),
                amount=amount,
                importer='byro_fints',
                memo=purpose,
            )

            # About the status:
            #  From the bank's perspective our bank account (an asset to us)
            #  is a liability. Money we have on the account is money they owe
            #  us. From the banks's perspective, money we get into our account
            #  is credited, it increases their liabilities.
            #  So from our perspective we have to invert that.

            if status == 'C':
                args['debit_account'] = fints_account.account
            else:
                args['credit_account'] = fints_account.account

            for booking in Booking.objects.filter(
                transaction__value_datetime=t.data.get('date'),
                **args,
            ).all():
                if booking.data == data:
                    break
            else:
                tr = Transaction.objects.create(value_datetime=t.data.get('date'))
                Booking.objects.create(transaction=tr, data=data, **args)


        fints_account.last_fetch_date = date.today()
        fints_account.save()

        return super().form_valid(form)
