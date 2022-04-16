from django import forms
from django.contrib import messages
from django.db import transaction
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView, UpdateView
from django.views.generic.detail import SingleObjectMixin
from fints.formals import DescriptionRequired

from ..forms import PinRequestForm
from ..models import FinTSLogin
from ._client import FinTSClientFormMixin, FinTSClientMixin
from .common import _fetch_update_accounts


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
