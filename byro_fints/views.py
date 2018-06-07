from datetime import date

from django import forms
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import CreateView, FormView, ListView
from django.views.generic.detail import SingleObjectMixin
from fints.client import FinTS3PinTanClient
from fints.models import SEPAAccount

from byro.bookkeeping.models import Account, RealTransaction, TransactionChannel

from .data import get_bank_information_by_blz
from .models import FinTSAccount, FinTSLogin


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
    form_class = forms.modelform_factory(FinTSLogin, fields=['blz', 'login_name', 'name', 'fints_url'])
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')

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
    pin = forms.CharField(label=_("PIN"), widget=forms.PasswordInput())


class FinTSLoginRefreshView(SingleObjectMixin, FormView):
    template_name = 'byro_fints/login_refresh.html'
    form_class = PinRequestForm
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')
    model = FinTSLogin
    context_object_name = 'fints_login'

    @property
    def object(self):
        return self.get_object()  # FIXME: WTF?  Apparently I'm supposed to implement a get()/post() that sets self.object?

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_login = self.get_object()
        form.fields['pin'].label = _('PIN for \'{login_name}\' at \'{display_name}\'').format(
            login_name=fints_login.login_name,
            display_name=fints_login.name
        )
        return form

    def form_valid(self, form):
        fints_login = self.get_object()
        client = FinTS3PinTanClient(
            fints_login.blz,
            fints_login.login_name,
            form.cleaned_data['pin'],
            fints_login.fints_url
        )

        accounts = client.get_sepa_accounts()

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
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')

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


class PinRequestAndDateForm(PinRequestForm):
    fetch_from_date = forms.DateField(label=_("Fetch start date"), required=True)


class FinTSAccountFetchView(SingleObjectMixin, FormView):
    template_name = 'byro_fints/account_fetch.html'
    form_class = PinRequestAndDateForm
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')
    model = FinTSAccount
    context_object_name = 'fints_account'

    @property
    def object(self):
        return self.get_object()

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_account = self.get_object()
        fints_login = fints_account.login
        form.fields['pin'].label = _('PIN for \'{login_name}\' at \'{display_name}\'').format(
            login_name=fints_login.login_name,
            display_name=fints_login.name
        )
        form.fields['fetch_from_date'].initial = fints_account.last_fetch_date or date.today().replace(day=1, month=1)
        # FIXME Check for plus/minus 1 day
        return form

    def form_valid(self, form):
        fints_account = self.get_object()
        fints_login = fints_account.login
        client = FinTS3PinTanClient(
            fints_login.blz,
            fints_login.login_name,
            form.cleaned_data['pin'],
            fints_login.fints_url
        )

        sepa_account = SEPAAccount(
            **{
                name: getattr(fints_account, name)
                for name in SEPAAccount._fields
            }
        )

        transactions = client.get_statement(sepa_account, form.cleaned_data['fetch_from_date'], date.today())

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
            RealTransaction.objects.get_or_create(
                channel=TransactionChannel.BANK,
                value_datetime=t.data.get('date'),  # FIXME Verify that these date fields are correct
                booking_datetime=t.data.get('entry_date'),
                amount=t.data.get('amount').amount,
                importer='byro_fints',
                originator=originator,
                purpose=purpose,
                # defaults={'data': t.data},  # FIXME JSON Fubar
            )

        fints_account.last_fetch_date = date.today()
        fints_account.save()

        return super().form_valid(form)
