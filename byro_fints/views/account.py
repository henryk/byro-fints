from datetime import date

from django import forms
from django.db import transaction
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView, TemplateView
from django.views.generic.detail import SingleObjectMixin
from fints.client import FinTSOperations
from fints.models import SEPAAccount
from mt940 import models as mt940_models
from byro.bookkeeping.models import Account

from .common import SessionBasedExisitingUserLoginFinTSHelperMixin
from ..fints_interface import FinTSHelper
from ..forms import PinRequestForm
from ..models import FinTSAccount


# FIXME: Allow inline create
# FIXME: Name of default accounts?
class FinTSAccountLinkView(SingleObjectMixin, FormView):
    template_name = "byro_fints/account_link.html"
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")

    model = FinTSAccount
    context_object_name = "fints_account"

    @property
    def object(self):
        return self.get_object()

    def get_form_class(self):
        class LinkForm(forms.Form):
            existing_account = forms.ChoiceField(
                choices=[
                    (a.pk, a.name)
                    for a in Account.objects.all()
                    if not hasattr(a, "fints_account")
                ],
                initial=self.object.account.pk if self.object.account else None,
            )

        return LinkForm

    @transaction.atomic
    def form_valid(self, form):
        account = self.get_object()
        account.account = Account.objects.get(pk=form.cleaned_data["existing_account"])
        account.save()
        account.log(self, ".linked", account=account.account)
        return super().form_valid(form)


class FinTSAccountInformationView(
    SingleObjectMixin, TemplateView
):
    template_name = "byro_fints/account_information.html"
    model = FinTSAccount
    context_object_name = "fints_account"
    form_class = PinRequestForm

    @property
    def object(self):
        return self.get_object()

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        fints_account = self.get_object()
        fints = FinTSHelper(self.request)
        fints.load_from_user_login(fints_account.login.user_login.filter(
                user=self.request.user
            ).first().pk)
        client = fints.get_readonly_client()
        context["information"] = client.get_information()
        for account in context["information"]["accounts"]:
            if (account["iban"] == fints_account.iban) or (
                account["account_number"] == fints_account.accountnumber
                and account["subaccount_number"] == fints_account.subaccount
            ):
                context["account_information"] = account
                break
            else:
                context["account_information"] = None
        context["OPERATIONS"] = list(FinTSOperations)
        return context


class PinRequestAndDateForm(PinRequestForm):
    fetch_from_date = forms.DateField(label=_("Fetch start date"), required=True)


class FinTSAccountFetchView(SessionBasedExisitingUserLoginFinTSHelperMixin, SingleObjectMixin, FormView):
    template_name = "byro_fints/account_fetch.html"
    form_class = PinRequestAndDateForm
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")
    model = FinTSAccount
    context_object_name = "fints_account"

    @property
    def object(self):
        return self.get_object()

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_account = self.get_object()
        form.fields[
            "fetch_from_date"
        ].initial = fints_account.last_fetch_date or date.today().replace(
            day=1, month=1
        )
        # FIXME Check for plus/minus 1 day
        return form

    @transaction.atomic
    def form_valid(self, form):
        fints_account = self.get_object()

        sepa_account = SEPAAccount(
            **{name: getattr(fints_account, name) for name in SEPAAccount._fields}
        )

        with self.fints_client(fints_account.login, form) as client:
            with client:
                transactions = client.get_transactions(
                    sepa_account, form.cleaned_data["fetch_from_date"], date.today()
                )
                fints_account.log(self, ".transactions_fetched")

        if form.errors:
            return super().form_invalid(form)

        for t in transactions:
            originator = "{} {} {}".format(
                t.data.get("applicant_name") or "",
                t.data.get("applicant_bin") or "",
                t.data.get("applicant_iban") or "",
            )
            purpose = "{} {} | {}".format(
                t.data.get("purpose") or "",
                t.data.get("additional_purpose") or "",
                t.data.get("posting_text") or "",
            )

            # Handle JSON "But I cant't serialize that?!" nonsense
            mt940_data = dict()
            for k, v in t.data.items():
                if isinstance(v, mt940_models.Amount):
                    v = {
                        "amount": str(v.amount),
                        "currency": v.currency,
                    }
                elif isinstance(v, mt940_models.Date):
                    v = v.isoformat()

                mt940_data[k] = v

            amount = t.data.get("amount").amount
            if amount < 0:
                amount = -amount
                status = "D"
            else:
                status = "C"

            data = dict(
                mt940_data=mt940_data,
                other_party=originator,
            )

            args = dict(
                booking_datetime=t.data.get("entry_date"),
                amount=amount,
                importer="byro_fints",
                memo=purpose,
            )

            # About the status:
            #  From the bank's perspective our bank account (an asset to us)
            #  is a liability. Money we have on the account is money they owe
            #  us. From the banks's perspective, money we get into our account
            #  is credited, it increases their liabilities.
            #  So from our perspective we have to invert that.

            if status == "C":
                args["debit_account"] = fints_account.account
            else:
                args["credit_account"] = fints_account.account

            for booking in Booking.objects.filter(
                transaction__value_datetime=t.data.get("date"),
                **args,
            ).all():
                if booking.data == data:
                    break
            else:
                tr = Transaction.objects.create(
                    value_datetime=t.data.get("date"),
                    user_or_context="FinTS fetch transactions",
                )
                if "debit_account" in args:
                    args["account"] = args.pop("debit_account")
                    tr.debit(
                        data=data, user_or_context="FinTS fetch transactions", **args
                    )
                else:
                    args["account"] = args.pop("credit_account")
                    tr.credit(
                        data=data, user_or_context="FinTS fetch transactions", **args
                    )

        fints_account.last_fetch_date = date.today()
        fints_account.save()

        return super().form_valid(form)
