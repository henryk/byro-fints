from django import forms
from django.contrib import messages
from django.db import transaction
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView, UpdateView
from django.views.generic.detail import SingleObjectMixin
from fints.client import FinTS3PinTanClient
from fints.formals import DescriptionRequired

from ..fints_interface import with_fints, FinTSHelper
from ..forms import PinRequestForm
from ..models import FinTSLogin
from .common import _fetch_update_accounts, SessionBasedExisitingUserLoginFinTSHelperMixin
from ..plugin_interface import FinTSPluginInterface


class FinTSLoginEditView(SessionBasedExisitingUserLoginFinTSHelperMixin, UpdateView):
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

        client = self.fints.get_readonly_client()
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

    @with_fints
    def form_valid(self, form):
        fints_login = self.get_object()
        if "tan_method" in form.changed_data:
            fints_user_login = fints_login.user_login.filter(
                user=self.request.user
            ).first()
            client: FinTS3PinTanClient = self.fints.get_readonly_client()
            # FIXME Better API (without opening a dialog)
            client.set_tan_mechanism(form.cleaned_data["tan_method"])
            fints_user_login.fints_client_data = client.deconstruct(including_private=True)
            fints_user_login.save(update_fields=["fints_client_data"])
        if "tan_medium" in form.changed_data:
            fints_user_login = fints_login.user_login.filter(
                user=self.request.user
            ).first()
            fints_user_login.selected_tan_medium = form.cleaned_data["tan_medium"]
            fints_user_login.save(update_fields=["selected_tan_medium"])
        return super().form_valid(form)


class FinTSLoginRefreshView(SingleObjectMixin, FormView):
    template_name = "byro_fints/login_refresh.html"
    form_class = PinRequestForm
    success_url = reverse_lazy("plugins:byro_fints:finance.fints.dashboard")
    model = FinTSLogin
    context_object_name = "fints_login"
    fints_interface: FinTSPluginInterface
    fints_helper: FinTSHelper

    @property
    def object(self):
        return (
            self.get_object()
        )  # FIXME: WTF?  Apparently I'm supposed to implement a get()/post() that sets self.object?

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.fints_interface = FinTSPluginInterface.with_request(self.request)
        self.fints_helper = self.fints_interface.get_fints(self.get_object().user_login.filter(
                user=self.request.user
            ).first().pk)

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        self.fints_helper.augment_form_pin_fields(form)
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        fints_user_login = self.object.user_login.filter(
            user=self.request.user
        ).first()
        self.fints_helper.open()

        try:
            _fetch_update_accounts(fints_user_login, self.fints_helper.client, view=self)
        finally:
            self.fints_helper.close()

        if form.errors:
            return super().form_invalid(form)

        return super().form_valid(form)
