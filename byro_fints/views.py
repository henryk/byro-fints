from functools import partial
from uuid import uuid4
from base64 import b64encode, b64decode
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

from byro.bookkeeping.models import Account, Transaction, Booking
from byro.common.models import Configuration
from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic import UpdateView, FormView, ListView, TemplateView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import FormMixin
from django_securebox.utils import Storage
from fints.client import (
    FinTS3PinTanClient,
    FinTSOperations,
    NeedTANResponse,
    TransactionResponse,
    ResponseStatus,
    FinTSClientMode,
)
from fints.exceptions import *
from fints.hhd.flicker import parse as hhd_flicker_parse
from fints.models import SEPAAccount
from fints.formals import DescriptionRequired, TANMedia5
from localflavor.generic.forms import BICFormField, IBANFormField
from mt940 import models as mt940_models

from .data import get_bank_information_by_blz
from .models import FinTSAccount, FinTSLogin, FinTSAccountCapabilities, FinTSUserLogin

PIN_CACHED_SENTINEL = "******"


def _cache_label(fints_login):
    return "byro_fints__pin__{}__cache".format(fints_login.pk)


CAPABILITY_MAP = {
    FinTSAccountCapabilities.FETCH_TRANSACTIONS: (FinTSOperations.GET_TRANSACTIONS,),
    FinTSAccountCapabilities.SEND_TRANSFER: (
        FinTSOperations.SEPA_TRANSFER_SINGLE,
        FinTSOperations.SEPA_TRANSFER_MULTIPLE,
    ),
    FinTSAccountCapabilities.SEND_TRANSFER_MULTIPLE: (
        FinTSOperations.SEPA_TRANSFER_MULTIPLE,
    ),
}


def _fetch_update_accounts(fints_user_login, client, information=None, view=None):
    fints_login = fints_user_login.login
    accounts = client.get_sepa_accounts()
    information = information or client.get_information()

    if any(
        getattr(e, "description_required", None)
        in (DescriptionRequired.MUST, DescriptionRequired.MAY)
        for e in information["auth"]["tan_mechanisms"].values()
    ):
        tan_media_result = client.get_tan_media()
    else:
        tan_media_result = None

    for account in accounts:
        extra_params = {}
        for acc in information["accounts"]:
            if acc["iban"] == account.iban:
                extra_params["name"] = acc["product_name"]

                caps = 0
                for cap_provided, caps_searched in CAPABILITY_MAP.items():
                    if any(
                        information["bank"]["supported_operations"][cap_searched]
                        and acc["supported_operations"][cap_searched]
                        for cap_searched in caps_searched
                    ):
                        caps = caps | cap_provided.value
                extra_params["caps"] = caps

        account, created = FinTSAccount.objects.get_or_create(
            login=fints_login, defaults=extra_params, **account._asdict()
        )
        if account.caps != caps:
            account.caps = caps
            account.save()
        # FIXME: Create accounts in bookeeping?
        if created:
            account.log(view, ".created")
        else:
            account.log(view, ".refreshed")

    if tan_media_result:
        _usage_option, tan_media = tan_media_result
        tan_media_names = [e.tan_medium_name for e in tan_media]

        fints_user_login.available_tan_media = [{"name": e} for e in tan_media_names]
        fints_user_login.save(update_fields=["available_tan_media"])


def _encode_binary_for_session(data):
    return b64encode(data).decode("us-ascii")


def _decode_binary_for_session(data):
    return b64decode(data.encode("us-ascii"))


class PinRequestForm(forms.Form):
    form_name = _("PIN request")
    login_name = forms.CharField(label=_("Login name"), required=True)
    pin = forms.CharField(
        label=_("PIN"), widget=forms.PasswordInput(render_value=True), required=True
    )
    store_pin = forms.ChoiceField(
        label=_("Store PIN?"),
        choices=[
            ["0", _("Don't store PIN")],
            ["1", _("For this login session only")],
            ["2", _("Store PIN (encrypted with account password)")],
        ],
        initial="0",
    )


class LoginCreateForm(PinRequestForm):
    form_name = _("Create FinTS login")
    field_order = ["blz", "login_name", "pin"]

    blz = forms.CharField(label=_("Routing number (BLZ)"), required=True)
    name = forms.CharField(label=_("Display name"), required=False)
    fints_url = forms.CharField(label=_("FinTS URL"), required=False)


class SEPATransferForm(PinRequestForm):
    form_name = _("SEPA transfer")
    field_order = ["recipient", "iban", "bic", "amount", "purpose"]

    recipient = forms.CharField(label=_("Recipient"), required=True)
    iban = IBANFormField(label=_("IBAN"), required=True)
    bic = BICFormField(label=_("BIC"), required=True)
    amount = forms.DecimalField(label=_("Amount"), required=True)
    purpose = forms.CharField(label=_("Purpose"), required=True)


class FinTSClientMixin:
    @contextmanager
    def fints_client(self, fints_login, form=None):
        fints_user_login, _ignore = fints_login.user_login.get_or_create(
            user=self.request.user
        )
        if form:
            fints_user_login.login_name = form.cleaned_data["login_name"]
            if form.cleaned_data["pin"] == PIN_CACHED_SENTINEL:
                pin = self.request.securebox[_cache_label(fints_login)]
            else:
                pin = form.cleaned_data["pin"]
        else:
            pin = None

        client = FinTS3PinTanClient(
            fints_login.blz,
            fints_user_login.login_name,
            pin,
            fints_login.fints_url,
            product_id="F41CDA6B1F8E0DADA0DDA29FD",
            from_data=fints_user_login.fints_client_data,
            mode=FinTSClientMode.INTERACTIVE,
        )
        client.add_response_callback(self.fints_callback)

        # FIXME HACK HACK HACK The python-fints API with regards to TAN media is not very useful yet
        # Circumvent it here

        if fints_user_login.selected_tan_medium:
            fake_tan_medium = TANMedia5(
                tan_medium_name=fints_user_login.selected_tan_medium
            )
            client.set_tan_medium(fake_tan_medium)

        try:
            yield client
            pin_correct = True

        except FinTSClientPINError:
            # PIN wrong, clear cached PIN, indicate error
            self.request.securebox.delete_value(_cache_label(fints_login))
            if form:
                form.add_error(
                    None, _("Can't establish FinTS dialog: Username/PIN wrong?")
                )
            pin_correct = False

        if pin_correct:
            fints_user_login.fints_client_data = client.deconstruct(
                including_private=True
            )
            fints_user_login.save(update_fields=["login_name", "fints_client_data"])

            if form:
                if form.cleaned_data["store_pin"] == "1":
                    storage = Storage.TRANSIENT_ONLY
                elif form.cleaned_data["store_pin"] == "2":
                    storage = Storage.PERMANENT_ONLY
                else:
                    storage = None

                if storage:
                    if form.cleaned_data["pin"] != PIN_CACHED_SENTINEL:
                        self.request.securebox.store_value(
                            _cache_label(fints_login), pin, storage=storage
                        )
                else:
                    self.request.securebox.delete_value(_cache_label(fints_login))

    def fints_callback(self, segment, response):
        l_ = None
        if response.code.startswith("0"):
            l_ = partial(messages.info, self.request)
        elif response.code.startswith("9"):
            l_ = partial(messages.info, self.request)
        elif response.code.startswith("0"):
            l_ = partial(messages.info, self.request)
        if l_:
            l_(
                "{} \u2014 {}".format(response.code, response.text)
                + ("({})".format(response.parameters) if response.parameters else "")
            )

    def _show_transaction_messages(self, response):
        if response.status == ResponseStatus.UNKNOWN:
            messages.warning(
                self.request, _("Unknown response. Final transaction status unknown.")
            )
        elif response.status == ResponseStatus.ERROR:
            messages.error(self.request, _("Error: Transaction not executed."))
        elif response.status == ResponseStatus.WARNING:
            messages.warning(
                self.request, _("Warning: Transaction warning, see other messages.")
            )
        elif response.status == ResponseStatus.SUCCESS:
            messages.success(self.request, _("Transaction executed successfully."))

    def pause_for_tan_request(self, client, response, **kwargs):
        uuid = str(uuid4())
        data = {
            "tan_mechanism": client.get_current_tan_mechanism(),
            "dialog": client.pause_dialog(),
            "response": response.get_data(),
        }
        data.update(kwargs)

        self.request.securebox.store_value(
            "tan_request_{}".format(uuid), data, Storage.TRANSIENT_ONLY
        )

        return uuid

    def resume_from_tan_request(self, client, uuid):
        data = self._tan_request_data(uuid)
        dialog_data = data.pop("dialog")
        response = data.pop("response")

        return client.resume_dialog(dialog_data), response, data

    def clean_tan_request(self, uuid):
        self.request.securebox.delete_value("tan_request_{}".format(uuid))

    def _tan_request_data(self, uuid):
        # FIXME Raise 404
        if uuid in ("test_data", "test_data_2"):

            class Dummy(NeedTANResponse):
                def __init__(self, *args, **kwargs):
                    pass

            data = {
                "tan_mechanism": None,
                "dialog": None,
                "response": Dummy(),
            }
            data["response"].challenge_html = "Yada"
            if uuid == "test_data":
                data["response"].challenge_hhduc = "02908881344731012345678900515,00"
            else:
                data["response"].challenge_matrix = (
                    "image/png",
                    b64decode(
                        "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"
                    ),
                )
            return data

        data = self.request.securebox.fetch_value("tan_request_{}".format(uuid))
        data["response"] = NeedTANResponse.from_data(data["response"])
        return data


class FinTSClientFormMixin(FormMixin, FinTSClientMixin):
    def _pin_store_location(self, fints_login):
        if (
            self.request.securebox.fetch_value(
                _cache_label(fints_login), Storage.TRANSIENT_ONLY, default=None
            )
            is not None
        ):
            return "1"
        elif (
            self.request.securebox.fetch_value(
                _cache_label(fints_login), Storage.PERMANENT_ONLY, default=None
            )
            is not None
        ):
            return "2"
        return "0"

    def get_form(self, extra_fields={}, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        fints_login = self.get_object()
        self.augment_form(form, fints_login, extra_fields)
        return form

    def augment_form(self, form, fints_login, extra_fields={}):
        if isinstance(fints_login, FinTSAccount):
            fints_login = fints_login.login
        fints_user_login, _ignore = fints_login.user_login.get_or_create(
            user=self.request.user
        )
        form.fields["login_name"].initial = fints_user_login.login_name
        form.fields["pin"].label = _("PIN for '{display_name}'").format(
            display_name=fints_login.name
        )
        form.fields["store_pin"].initial = self._pin_store_location(fints_login)
        form.fields.update(extra_fields)
        if form.fields["store_pin"].initial != "0":
            form.fields["pin"].initial = PIN_CACHED_SENTINEL

            # Hide PIN fields if more than the PIN fields are present
            maybe_hidden_fields = ["login_name", "pin", "store_pin"]
            if any(name not in maybe_hidden_fields for name in form.fields.keys()):
                for name in maybe_hidden_fields:
                    form.fields[name].widget = forms.HiddenInput()
        return form

    def get_tan_form_fields(self, fints_login, tan_request_data, *args, **kwargs):
        with self.fints_client(fints_login) as client:
            tan_param = client.get_tan_mechanisms()[
                tan_request_data["tan_mechanism"] or client.get_current_tan_mechanism()
            ]
            # Do not use tan_param.allowed_format, because IntegerField is not the same as AllowedFormat.NUMERIC
            # FIXME
            tan_field = forms.CharField(
                label=tan_param.text_return_value, max_length=tan_param.max_length_input
            )

        return {"tan": tan_field}

    @staticmethod
    def get_flicker_css(data, css_class):
        stream = [1, 0, 31, 30, 31, 30]
        for i in range(len(data)):
            d = int(data[i ^ 1], 16)
            stream.append(1 | (d << 1))
            stream.append(0 | (d << 1))

        last = 0
        per_frame = 100.0 / float(len(stream))
        duration = 0.025 * len(stream)

        keyframes = [[] for i in range(5)]

        for index, frame in enumerate(stream):
            changed = frame ^ last
            last = frame
            if index == 0:
                changed = 31
            for bit_index in range(5):
                if (frame >> bit_index) & 1:
                    color = "#fff"
                else:
                    color = "#000"
                if (changed >> bit_index) & 1:
                    keyframes[bit_index].append(
                        r"{}% {{ background-color: {}; }}".format(
                            index * per_frame, color
                        )
                    )

        result = [
            "@keyframes {css_class}-bar-{i} {{ {k} }}".format(
                k=" ".join(kf), i=i, css_class=css_class
            )
            for i, kf in enumerate(keyframes)
        ]
        result.extend(
            """
            .flicker-animate-css .flicker-bar {{
                animation-duration: {duration}s;
                animation-iteration-count: infinite;
                animation-timing-function: step-end;
            }}
            .flicker-animate-css.{css_class} .flicker-bar-{i} {{
                animation-name: {css_class}-bar-{i};
            }}""".format(
                i=i, css_class=css_class, duration=duration
            )
            for i in range(5)
        )

        return "\n".join(result)

    def get_tan_context_data(self, tan_request_data):
        context = {"challenge": mark_safe(tan_request_data["response"].challenge_html)}

        if tan_request_data["response"].challenge_hhduc:
            flicker = hhd_flicker_parse(tan_request_data["response"].challenge_hhduc)
            context["challenge_flicker"] = flicker.render()

            css_class = "flicker-{}".format(uuid4())
            context["challenge_flicker_css_class"] = css_class
            context["challenge_flicker_css"] = lambda: self.get_flicker_css(
                flicker.render(), css_class
            )

        if tan_request_data["response"].challenge_matrix:
            context["challenge_matrix_url"] = "data:{};base64,{}".format(
                tan_request_data["response"].challenge_matrix[0],
                b64encode(tan_request_data["response"].challenge_matrix[1]).decode(
                    "us-ascii"
                ),
            )

        return context


class Dashboard(ListView):
    template_name = "byro_fints/dashboard.html"
    queryset = FinTSLogin.objects.order_by("blz").all()
    context_object_name = "fints_logins"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context["fints_accounts"] = FinTSAccount.objects.order_by("iban").all()
        return context


class FinTSLoginCreateView(FinTSClientMixin, FormView):
    template_name = "byro_fints/login_add.html"
    form_class = LoginCreateForm

    @transaction.atomic
    def form_valid(self, form):
        bank_information = get_bank_information_by_blz(form.cleaned_data["blz"])

        fints_url = form.cleaned_data["fints_url"]
        if not fints_url:
            fints_url = bank_information.get("PIN/TAN-Zugang URL", "")

        fints_login = FinTSLogin.objects.create(
            blz=form.cleaned_data["blz"],
            fints_url=fints_url,
            name=form.cleaned_data["name"] or bank_information.get("Institut", ""),
        )

        try:
            if not fints_login.fints_url:
                form.add_error(
                    "fints_url",
                    _(
                        "FinTS URL could not be looked up automatically, please fill it in manually."
                    ),
                )
                return super().form_invalid(form)

            fints_login.log(self, ".created")

            with self.fints_client(fints_login, form) as client:
                fints_user_login = fints_login.user_login.filter(
                    user=self.request.user
                ).first()
                with client:
                    information = client.get_information()

                    if not form.cleaned_data["name"] and information["bank"]["name"]:
                        fints_login.name = information["bank"]["name"]

                    _fetch_update_accounts(
                        fints_user_login, client, information, view=self
                    )

            if form.errors:
                return super().form_invalid(form)

        finally:
            if form.errors:
                fints_login.delete()

        messages.warning(
            self.request, _("Bank login was added, please double-check TAN method")
        )
        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.edit",
                kwargs={"pk": fints_login.pk},
            )
        )


class FinTSLoginEditView(FinTSClientMixin, UpdateView):
    template_name = "byro_fints/login_edit.html"
    model = FinTSLogin
    context_object_name = "fints_login"
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")
    fields = ["name", "fints_url"]

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)

        fints_login = self.get_object()
        fints_user_login = fints_login.user_login.filter(user=self.request.user).first()
        tan_media_choices = []

        with self.fints_client(fints_login) as client:
            information = client.get_information()

        if any(
            getattr(e, "description_required", None)
            in (DescriptionRequired.MUST, DescriptionRequired.MAY)
            for e in information["auth"]["tan_mechanisms"].values()
        ):
            if fints_user_login:
                if fints_user_login.available_tan_media:
                    tan_media_choices = [
                        (v["name"], v["name"])
                        for v in fints_user_login.available_tan_media
                    ]
                else:
                    messages.warning(
                        self.request,
                        _(
                            "TAN media may be required to execute commands. Please synchronize the account."
                        ),
                    )

        tan_choices = [
            (k, v.name) for (k, v) in information["auth"]["tan_mechanisms"].items()
        ]
        form.fields["tan_method"] = forms.ChoiceField(
            label=_("TAN method"),
            choices=tan_choices,
            widget=forms.RadioSelect(),
            initial=information["auth"]["current_tan_mechanism"],
        )

        if tan_media_choices:
            form.fields["tan_medium"] = forms.ChoiceField(
                label=_("TAN medium"),
                choices=tan_media_choices,
                initial=fints_user_login.selected_tan_medium,
            )

        return form

    def form_valid(self, form):
        fints_login = self.get_object()
        if "tan_method" in form.changed_data:
            with self.fints_client(fints_login) as client:
                client.set_tan_mechanism(form.cleaned_data["tan_method"])
        if "tan_medium" in form.changed_data:
            fints_user_login = fints_login.user_login.filter(
                user=self.request.user
            ).first()
            fints_user_login.selected_tan_medium = form.cleaned_data["tan_medium"]
            fints_user_login.save(update_fields=["selected_tan_medium"])
        return super().form_valid(form)


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


class FinTSLoginRefreshView(SingleObjectMixin, FinTSClientFormMixin, FormView):
    template_name = "byro_fints/login_refresh.html"
    form_class = PinRequestForm
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")
    model = FinTSLogin
    context_object_name = "fints_login"

    @property
    def object(self):
        return (
            self.get_object()
        )  # FIXME: WTF?  Apparently I'm supposed to implement a get()/post() that sets self.object?

    @transaction.atomic
    def form_valid(self, form):
        fints_login = self.get_object()
        with self.fints_client(fints_login, form) as client:
            fints_user_login = fints_login.user_login.filter(
                user=self.request.user
            ).first()
            with client:
                _fetch_update_accounts(fints_user_login, client, view=self)

        if form.errors:
            return super().form_invalid(form)

        return super().form_valid(form)


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
    SingleObjectMixin, FinTSClientFormMixin, TemplateView
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
        with self.fints_client(fints_account.login) as client:
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


class FinTSAccountFetchView(SingleObjectMixin, FinTSClientFormMixin, FormView):
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
