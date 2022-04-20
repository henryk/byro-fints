from base64 import b64decode, b64encode
from contextlib import contextmanager
from functools import partial
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic.edit import FormMixin
from django_securebox.utils import Storage
from fints.client import (
    FinTS3PinTanClient,
    FinTSClientMode,
    NeedTANResponse,
    ResponseStatus,
)
from fints.exceptions import FinTSClientPINError
from fints.formals import TANMedia5
from fints.hhd.flicker import parse as hhd_flicker_parse

from ..fints_interface import PIN_CACHED_SENTINEL
from ..models import FinTSAccount
from .common import get_flicker_css


class FinTSClientMixin:
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

    def get_tan_context_data(self, tan_request_data):
        context = {"challenge": mark_safe(tan_request_data["response"].challenge_html)}

        if tan_request_data["response"].challenge_hhduc:
            flicker = hhd_flicker_parse(tan_request_data["response"].challenge_hhduc)
            context["challenge_flicker"] = flicker.render()

            css_class = "flicker-{}".format(uuid4())
            context["challenge_flicker_css_class"] = css_class
            context["challenge_flicker_css"] = lambda: get_flicker_css(
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
