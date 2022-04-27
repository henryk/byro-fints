import uuid
from base64 import b64encode
from typing import Optional, Tuple

from django import forms
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView
from fints.exceptions import FinTSClientPINError

from ..fints_interface import (
    with_fints, PinState, AbstractFinTSHelper, FinTSHelper, SessionBasedFinTSHelperMixin,
)
from byro_fints.forms import (
    LoginCreateStep1Form,
    LoginCreateStep2Form,
    LoginCreateStep3Form,
    LoginCreateStep4Form,
    LoginCreateStep5Form,
)
from byro_fints.models import FinTSLogin
from .common import (
    _fetch_update_accounts,
)


class FinTSHelperAddProcess(AbstractFinTSHelper):
    SAVE_PIN_IN_RESUME = True

    def __init__(self, request):
        super().__init__(request)
        self.pin_state_shouldbe: PinState = PinState.NONE
        self.login_pk: Optional[int] = None
        self.client_data: Optional[bytes] = None
        self.blz: Optional[str] = None
        self.login_name: Optional[str] = None
        self.fints_url: Optional[str] = None
        self.display_name: Optional[str] = None
        self.information: Optional[dict] = None
        self.accounts: Optional[dict] = None

    def _get_client_args(self) -> Tuple[str, str, str]:
        return self.blz, self.login_name, self.fints_url

    @property
    def from_data(self) -> bytes:
        return self.client_data

    def _do_save_client_data(self, client_data: bytes):
        # Saves it in the object to be later retrieved by _get_data_for_session
        self.client_data = client_data

    def _get_data_for_session(self) -> Tuple:
        return super()._get_data_for_session() + (
            self.pin_state_shouldbe,
            self.login_pk,
            self.client_data,
            self.blz,
            self.login_name,
            self.fints_url,
            self.display_name,
            self.information,
            self.accounts,
        )

    def _set_data_from_session(self, data):
        super()._set_data_from_session(data[:-9])
        (
            self.pin_state_shouldbe,
            self.login_pk,
            self.client_data,
            self.blz,
            self.login_name,
            self.fints_url,
            self.display_name,
            self.information,
            self.accounts,
        ) = data[-9:]

    @property
    def login(self) -> Optional[FinTSLogin]:
        if not self.login_pk:
            return None
        return FinTSLogin.objects.filter(pk=self.login_pk).first()

    def save_pin(self, pin_state: PinState, pin: str):
        return super().save_pin(PinState.SAVE_ON_RESUME, pin)

    def load_from_login(self, login_pk: int):
        self.login_pk = login_pk
        login = self.login
        self.blz = login.blz
        self.fints_url = login.fints_url
        self.display_name = login.name

    def load_from_form(self, form: forms.Form):
        super().load_from_form(form)
        if form.cleaned_data.get("blz", "").strip():
            self.blz = form.cleaned_data["blz"]
        if form.cleaned_data.get("login_name", "").strip():
            self.login_name = form.cleaned_data["login_name"]
        if form.cleaned_data.get("fints_url", "").strip():
            self.fints_url = form.cleaned_data["fints_url"]
        if form.cleaned_data.get("name", "").strip():
            self.display_name = form.cleaned_data["name"]
        if form.cleaned_data.get("store_pin", "").strip():
            self.pin_state_shouldbe = PinState(form.cleaned_data["store_pin"])

    def do_step2(self, tan_mechanism: Optional[str] = None) -> bool:
        if len(self.tan_mechanisms) > 1 and tan_mechanism is None:
            return False
        tan_mechanism = tan_mechanism or self.tan_mechanism
        if tan_mechanism != self.tan_mechanism:
            self.reopen(tan_mechanism=tan_mechanism)
        return True

    def do_step3(self, tan_medium: Optional[object] = None) -> bool:
        if self.client.is_tan_media_required() and not self.client.selected_tan_medium:
            self.get_tan_media()
            if len(self.tan_media) > 1 and tan_medium is None:
                return False
            tan_medium = tan_medium or self.tan_medium
            if tan_medium != self.tan_medium:
                self.reopen(tan_medium=tan_medium)
        return True

    def do_step4(self, tan: Optional[str] = None):
        if self.init_tan_request:
            if tan is None:
                return False
            else:
                self.client.send_tan(self.init_tan_request, tan)

        self.accounts = self.client.get_sepa_accounts()
        self.information = self.client.get_information()
        return True


class SessionBasedFinTSAddProcessHelperMixin(SessionBasedFinTSHelperMixin):
    HELPER_CLASS = FinTSHelperAddProcess

    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        login = self.request.GET.get("login", None)
        if login:
            login_pk = int(login)
            self.fints.load_from_login(login_pk)


class FinTSLoginCreateStep1View(SessionBasedFinTSAddProcessHelperMixin, FormView):
    template_name = "byro_fints/login_add_1.html"
    form_class = LoginCreateStep1Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        self.fints.augment_form_pin_fields(form)
        if self.fints.login_pk:
            login = self.fints.login
            form.fields["blz"].initial = login.blz
            form.fields["blz"].disabled = True
            form.fields["fints_url"].initial = login.fints_url
            form.fields["fints_url"].disabled = True
            form.fields["name"].initial = login.name
            form.fields["name"].disabled = True
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.fints.load_from_form(form)

        try:
            self.fints.open()

            tan_mechanisms = self.fints.get_tan_mechanisms()
            if len(tan_mechanisms) == 0:
                form.add_error(None, _("Can't find TAN mechanism"))
                return self.form_invalid(form)

            next_step = 2  # select tan mechanism
            if self.fints.do_step2():
                next_step = 3  # select tan media

            if next_step == 3:
                if self.fints.do_step3():
                    next_step = 4  # fetch account info

            if next_step == 4:
                if self.fints.do_step4():
                    next_step = 5

            resume_id = self.fints.save_in_session()

            return HttpResponseRedirect(
                reverse(
                    "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                    kwargs={
                        "resume_id": resume_id,
                    },
                )
            )

        except FinTSClientPINError:
            form.add_error(None, _("Can't establish FinTS dialog: Username/PIN wrong?"))
            return self.form_invalid(form)

        finally:
            self.fints.close()


class FinTSLoginCreateStep2View(SessionBasedFinTSAddProcessHelperMixin, FormView):
    template_name = "byro_fints/login_add_2.html"
    form_class = LoginCreateStep2Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        print(self.fints.tan_mechanisms)
        form.fields["tan_mechanism"] = forms.ChoiceField(
            choices=self.fints.tan_mechanisms.items()
        )
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.fints.open()

        next_step = 2
        if self.fints.do_step2(tan_mechanism=form.cleaned_data["tan_mechanism"]):
            next_step = 3  # select tan media

        if next_step == 3:
            if self.fints.do_step3():
                next_step = 4  # fetch account info

        if next_step == 4:
            if self.fints.do_step4():
                next_step = 5

        resume_id = self.fints.save_in_session()

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep3View(SessionBasedFinTSAddProcessHelperMixin, FormView):
    template_name = "byro_fints/login_add_3.html"
    form_class = LoginCreateStep3Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        print(self.fints.tan_media)
        form.fields["tan_medium"] = forms.ChoiceField(
            choices=[(k, k) for k in self.fints.tan_media]
        )
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.fints.open()

        next_step = 3
        if self.fints.do_step3(tan_medium=form.cleaned_data["tan_medium"]):
            next_step = 4  # fetch account info

        if next_step == 4:
            if self.fints.do_step4():
                next_step = 5

        resume_id = self.fints.save_in_session()

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep4View(SessionBasedFinTSAddProcessHelperMixin, FormView):
    template_name = "byro_fints/login_add_4.html"
    form_class = LoginCreateStep4Form

    def get_context_data(self, **kwargs):
        retval = super().get_context_data(**kwargs)
        tan_request = self.fints.tan_request
        tan_context = self.fints.get_tan_context_data(tan_request)

        return dict(retval, **tan_context)

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        self.fints.augment_form_tan_fields(form)
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form: forms.Form):
        self.fints.open()

        next_step = 4
        if next_step == 4:
            if self.fints.do_step4(tan=form.cleaned_data["tan"]):
                next_step = 5

        resume_id = self.fints.save_in_session()

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep5View(SessionBasedFinTSAddProcessHelperMixin, FormView):
    template_name = "byro_fints/login_add_5.html"
    form_class = LoginCreateStep5Form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        display_name = (
                self.fints.display_name or self.fints.information["bank"]["name"]
        )

        self.fints.open()

        fints_login = self.fints.login
        if not self.fints.login:
            fints_login, _ = FinTSLogin.objects.get_or_create(
                name=display_name,
                blz=self.fints.blz,
                fints_url=self.fints.fints_url,
            )

        fints_user_login, _ = fints_login.user_login.get_or_create(
            user=self.request.user
        )
        fints_user_login.login_name = self.fints.login_name

        _fetch_update_accounts(
            fints_user_login,
            self.fints.client,
            information=self.fints.information,
            accounts=self.fints.accounts,
            view=self,
        )

        self.fints.close()
        fints_user_login.available_tan_media = (
            [{"name": e} for e in self.fints.tan_media]
            if self.fints.tan_media
            else []
        )
        fints_user_login.selected_tan_medium = self.fints.tan_medium
        fints_user_login.fints_client_data = self.fints.from_data
        fints_user_login.save()

        if self.fints.pin_state_shouldbe in (PinState.SAVE_TEMPORARY, PinState.SAVE_PERSISTENT):
            new_wrapper = FinTSHelper(self.request)
            new_wrapper.load_from_user_login(fints_user_login.pk)
            new_wrapper.save_pin(self.fints.pin_state_shouldbe, self.fints.pin)

        self.fints.delete_from_session()

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.edit",
                kwargs={"pk": fints_user_login.login.pk},
            )
        )
