from django import forms
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView
from django.views.generic.detail import SingleObjectMixin
from fints.client import NeedTANResponse, TransactionResponse
from fints.models import SEPAAccount
from localflavor.generic.forms import BICFormField, IBANFormField

from ..forms import PinRequestForm
from ..models import FinTSAccount, FinTSLogin
from ._client import FinTSClientFormMixin


class SEPATransferForm(PinRequestForm):
    form_name = _("SEPA transfer")
    field_order = ["recipient", "iban", "bic", "amount", "purpose"]

    recipient = forms.CharField(label=_("Recipient"), required=True)
    iban = IBANFormField(label=_("IBAN"), required=True)
    bic = BICFormField(label=_("BIC"), required=True)
    amount = forms.DecimalField(label=_("Amount"), required=True)
    purpose = forms.CharField(label=_("Purpose"), required=True)


class FinTSAccountTransferView(SingleObjectMixin, FinTSClientFormMixin, FormView):
    template_name = "byro_fints/account_transfer.html"
    form_class = SEPATransferForm
    model = FinTSAccount
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")

    @property
    def object(self):
        return self.get_object()

    def form_valid(self, form):
        config = Configuration.get_solo()
        fints_account = self.get_object()
        sepa_account = SEPAAccount(
            **{name: getattr(fints_account, name) for name in SEPAAccount._fields}
        )
        transfer_log_data = {
            k: v for k, v in form.cleaned_data.items() if not k in ("pin", "store_pin")
        }
        transfer_log_data["source_account"] = sepa_account._asdict()
        with self.fints_client(fints_account.login, form) as client:
            with client:
                try:
                    response = client.simple_sepa_transfer(
                        sepa_account,
                        form.cleaned_data["iban"],
                        form.cleaned_data["bic"],
                        form.cleaned_data["recipient"],
                        form.cleaned_data["amount"],
                        config.name,
                        form.cleaned_data["purpose"],
                    )
                    if isinstance(response, TransactionResponse):
                        fints_account.log(
                            self,
                            ".transfer.completed",
                            transfer=transfer_log_data,
                            response_status=response.status,
                            response_messages=response.responses,
                            response_data=response.data,
                        )
                        self._show_transaction_messages(response)
                    elif isinstance(response, NeedTANResponse):
                        transfer_uuid = self.pause_for_tan_request(client, response)
                        fints_account.log(
                            self,
                            ".transfer.started",
                            transfer=transfer_log_data,
                            uuid=transfer_uuid,
                        )

                        return HttpResponseRedirect(
                            reverse(
                                "plugins:byro_fints:finance.fints.login.tan_request",
                                kwargs={
                                    "pk": fints_account.login.pk,
                                    "uuid": transfer_uuid,
                                },
                            )
                        )

                    else:
                        fints_account.log(
                            self, ".transfer.internal_error", transfer=transfer_log_data
                        )
                        messages.error(
                            self.request, _("Invalid response: {}".format(response))
                        )
                except:
                    fints_account.log(
                        self, ".transfer.exception", transfer=transfer_log_data
                    )
                    raise
        return super().form_valid(form)


class FinTSLoginTANRequestView(SingleObjectMixin, FinTSClientFormMixin, FormView):
    template_name = "byro_fints/tan_request.html"
    form_class = PinRequestForm
    model = FinTSLogin
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")

    @property
    def object(self):
        return self.get_object()

    def get_form(self, *args, **kwargs):
        return super().get_form(
            extra_fields=self.get_tan_form_fields(
                self.object, self._tan_request_data(self.kwargs["uuid"])
            ),
            *args,
            **kwargs,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            self.get_tan_context_data(self._tan_request_data(self.kwargs["uuid"]))
        )
        return context

    def form_valid(self, form):
        fints_login = self.get_object()
        # fints_account = fints_login. ... # FIXME

        with self.fints_client(fints_login, form) as client:
            resume_dialog, response, other_data = self.resume_from_tan_request(
                client, self.kwargs["uuid"]
            )
            with resume_dialog:
                try:
                    response = client.send_tan(
                        response, form.cleaned_data["tan"].strip()
                    )
                    if isinstance(response, TransactionResponse):
                        fints_login.log(
                            self,
                            ".transfer.completed",
                            response_status=response.status,
                            response_messages=response.responses,
                            response_data=response.data,
                            uuid=self.kwargs["uuid"],
                        )
                        self._show_transaction_messages(response)
                    else:
                        fints_login.log(
                            self, ".transfer.internal_error", uuid=self.kwargs["uuid"]
                        )
                        messages.error(
                            self.request, _("Invalid response: {}".format(response))
                        )
                except:
                    fints_login.log(
                        self, ".transfer.exception", uuid=self.kwargs["uuid"]
                    )
        self.clean_tan_request(self.kwargs["uuid"])
        return super().form_valid(form)
