import pickle
import uuid
from base64 import b64encode
from typing import Optional, Dict, List

from django.db import transaction
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView
from django import forms
from django.http import HttpResponseRedirect
from django.urls import reverse
from django_securebox.utils import Storage
from fints.hhd.flicker import parse as hhd_flicker_parse
from fints.client import (
    FinTSClientMode,
    FinTS3PinTanClient,
    NeedTANResponse,
)
from fints.exceptions import FinTSClientPINError
from fints.types import SegmentSequence

from byro_fints.fints_interface import (
    with_fints,
    open_client,
    BYRO_FINTS_PRODUCT_ID,
    close_client,
    pause_client,
    resume_client,
)
from byro_fints.forms import (
    LoginCreateStep1Form,
    LoginCreateStep5Form,
    LoginCreateStep2Form,
    LoginCreateStep3Form,
    LoginCreateStep4Form,
)
from byro_fints.models import FinTSLogin
from byro_fints.views.common import (
    _encode_binary_for_session,
    _decode_binary_for_session,
    _fetch_update_accounts,
)


class FinTSWrapper:
    def __init__(self, blz=None, login_name=None, pin=None, fints_url=None):
        self.resume_id = str(uuid.uuid4())
        self.client: Optional[FinTS3PinTanClient] = None
        self.blz: Optional[str] = blz
        self.login_name: Optional[str] = login_name
        self.pin: Optional[str] = pin
        self.fints_url: Optional[str] = fints_url
        self.display_name: Optional[str] = None
        self.from_data: Optional[bytes] = None
        self.dialog_data: Optional[bytes] = None
        self.tan_request_serialized: Optional[bytes] = None
        self.tan_mechanism: Optional[str] = None
        self.tan_medium: Optional[str] = None
        self.tan_mechanisms: Optional[Dict[str, str]] = None
        self.tan_media: Optional[List[str]] = None
        self.information: Optional[dict] = None
        self.accounts: Optional[dict] = None

    @staticmethod
    def _label(resume_id):
        return "byro_fints:login.add:%s" % resume_id

    def save_in_session(self, request) -> str:
        if self.client:
            self.from_data, self.dialog_data = pause_client(self.client)
            self.client = None

        label = self._label(self.resume_id)

        request.session[label] = _encode_binary_for_session(
            pickle.dumps(
                (
                    self.blz,
                    self.login_name,
                    self.fints_url,
                    self.display_name,
                    self.from_data,
                    self.dialog_data,
                    self.tan_request_serialized,
                    self.tan_mechanism,
                    self.tan_medium,
                    self.tan_mechanisms,
                    self.tan_media,
                    self.information,
                    self.accounts,
                )
            )
        )
        request.securebox.store_value(
            label + "/pin", self.pin, storage=Storage.TRANSIENT_ONLY
        )

        return self.resume_id

    @classmethod
    def restore_from_session(cls, request, resume_id: str):
        label = cls._label(resume_id)

        data = request.session[label]
        pin = request.securebox[label + "/pin"]

        retval = cls()
        (
            retval.blz,
            retval.login_name,
            retval.fints_url,
            retval.display_name,
            retval.from_data,
            retval.dialog_data,
            retval.tan_request_serialized,
            retval.tan_mechanism,
            retval.tan_medium,
            retval.tan_mechanisms,
            retval.tan_media,
            retval.information,
            retval.accounts,
        ) = pickle.loads(_decode_binary_for_session(data))
        retval.pin = pin

        retval.resume_id = resume_id

        return retval

    def delete_from_session(self, request):
        label = self._label(self.resume_id)

        del request.session[label]
        request.securebox.delete_value(label + "/pin")

    @classmethod
    def from_step1(cls, form: forms.Form):
        retval = cls(
            form.cleaned_data["blz"],
            form.cleaned_data["login_name"],
            form.cleaned_data["pin"],
            form.cleaned_data["fints_url"],
        )
        retval.display_name = form.cleaned_data["name"]
        return retval

    def open(self):
        if not self.client:
            args = [self.blz, self.login_name, self.pin, self.fints_url]
            kwargs = dict(
                product_id=BYRO_FINTS_PRODUCT_ID,
                mode=FinTSClientMode.INTERACTIVE,
                tan_mechanism=self.tan_mechanism,
                tan_medium_name=self.tan_medium,
            )
            if self.dialog_data:
                self.client = resume_client(
                    *args,
                    client_data=self.from_data,
                    dialog_data=self.dialog_data,
                    **kwargs,
                )
            else:
                self.client = open_client(*args, from_data=self.from_data, **kwargs)
                if getattr(self.client, "init_tan_response", None):
                    # FIXME See python-fints#114
                    self.tan_request_serialized = SegmentSequence(
                        [self.client.init_tan_response.tan_request]
                    ).render_bytes()

    @property
    def tan_request(self):
        # FIXME See python-fints#114
        if not self.tan_request_serialized:
            return None
        return NeedTANResponse(
            None,
            SegmentSequence(self.tan_request_serialized).segments[0],
            "_continue_dialog_initialization",
            self.get_readonly_client().is_challenge_structured(),
        )

    def reopen(self, **kwargs):
        self.close()
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.open()

    def close(self):
        if self.client:
            self.from_data = close_client(self.client, including_private=True)
            self.client = None

    def get_readonly_client(self) -> FinTS3PinTanClient:
        client = FinTS3PinTanClient(
            self.blz,
            self.login_name,
            "XXX",
            self.fints_url,
            product_id=BYRO_FINTS_PRODUCT_ID,
            from_data=self.from_data,
            mode=FinTSClientMode.OFFLINE,
        )
        client.set_tan_mechanism(self.tan_mechanism)
        return client

    def get_tan_mechanisms(self):
        self.tan_mechanisms = {
            k: f"{k}: {v.name} ({v.tech_id})"
            for (k, v) in self.client.get_tan_mechanisms().items()
        }
        if self.tan_mechanism is None and len(self.tan_mechanisms) > 0:
            self.tan_mechanism = list(self.tan_mechanisms.keys())[0]
        print(self.tan_mechanisms)
        return self.tan_mechanisms

    def get_tan_media(self):
        _usage, tan_media = self.client.get_tan_media()
        self.tan_media = [tm.tan_medium_name for tm in tan_media]
        if self.tan_medium is None and len(self.tan_media) > 0:
            self.tan_medium = list(self.tan_media)[0]
        print(self.tan_media)
        return self.tan_media

    def do_step2(self, tan_mechanism: Optional[str] = None) -> bool:
        if len(self.tan_mechanisms) > 1 and tan_mechanism is None:
            return False
        tan_mechanism = tan_mechanism or self.tan_mechanism
        if tan_mechanism != self.tan_mechanism:
            print("REOPEN Step 2")
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

    def do_step4(self):
        if self.tan_request:
            print("TAN request: {}".format(self.tan_request.challenge))
            return False

        self.accounts = self.client.get_sepa_accounts()
        self.information = self.client.get_information()
        return True


class FinTSLoginCreateStep1View(FormView):
    template_name = "byro_fints/login_add_1.html"
    form_class = LoginCreateStep1Form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        wrapper = FinTSWrapper.from_step1(form)

        try:
            wrapper.open()

            tan_mechanisms = wrapper.get_tan_mechanisms()
            if len(tan_mechanisms) == 0:
                form.add_error(None, _("Can't find TAN mechanism"))
                return self.form_invalid(form)

            next_step = 2  # select tan mechanism
            if wrapper.do_step2():
                next_step = 3  # select tan media

            if next_step == 3:
                if wrapper.do_step3():
                    next_step = 4  # fetch account info

            if next_step == 4:
                if wrapper.do_step4():
                    next_step = 5

            resume_id = wrapper.save_in_session(self.request)

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
            wrapper.close()


class SessionBasedFinTSWrapperMixin:
    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        self.wrapper = FinTSWrapper.restore_from_session(
            self.request, self.kwargs["resume_id"]
        )


class FinTSLoginCreateStep2View(SessionBasedFinTSWrapperMixin, FormView):
    template_name = "byro_fints/login_add_2.html"
    form_class = LoginCreateStep2Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        print(self.wrapper.tan_mechanisms)
        form.fields["tan_mechanism"] = forms.ChoiceField(
            choices=self.wrapper.tan_mechanisms.items()
        )
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.wrapper.open()

        next_step = 2
        if self.wrapper.do_step2(tan_mechanism=form.cleaned_data["tan_mechanism"]):
            next_step = 3  # select tan media

        if next_step == 3:
            if self.wrapper.do_step3():
                next_step = 4  # fetch account info

        if next_step == 4:
            if self.wrapper.do_step4():
                next_step = 5

        resume_id = self.wrapper.save_in_session(self.request)

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep3View(SessionBasedFinTSWrapperMixin, FormView):
    template_name = "byro_fints/login_add_3.html"
    form_class = LoginCreateStep3Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        print(self.wrapper.tan_media)
        form.fields["tan_medium"] = forms.ChoiceField(
            choices=[(k, k) for k in self.wrapper.tan_media]
        )
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.wrapper.open()

        next_step = 3
        if self.wrapper.do_step3(tan_medium=form.cleaned_data["tan_medium"]):
            next_step = 4  # fetch account info

        if next_step == 4:
            if self.wrapper.do_step4():
                next_step = 5

        resume_id = self.wrapper.save_in_session(self.request)

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep4View(SessionBasedFinTSWrapperMixin, FormView):
    template_name = "byro_fints/login_add_4.html"
    form_class = LoginCreateStep4Form

    def get_context_data(self, **kwargs):
        retval = super().get_context_data(**kwargs)
        tan_request = self.wrapper.tan_request
        tan_context = {}

        if tan_request:
            tan_context = {"challenge": mark_safe(tan_request.challenge_html)}

            if tan_request.challenge_hhduc:
                flicker = hhd_flicker_parse(tan_request.challenge_hhduc)
                tan_context["challenge_flicker"] = flicker.render()

                css_class = "flicker-{}".format(uuid.uuid4())
                tan_context["challenge_flicker_css_class"] = css_class
                from . import get_flicker_css

                tan_context["challenge_flicker_css"] = lambda: get_flicker_css(
                    flicker.render(), css_class
                )

            if tan_request.challenge_matrix:
                tan_context["challenge_matrix_url"] = "data:{};base64,{}".format(
                    tan_request.challenge_matrix[0],
                    b64encode(tan_request.challenge_matrix[1]).decode("us-ascii"),
                )

        return dict(retval, **tan_context)

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        client = self.wrapper.get_readonly_client()
        tan_param = client.get_tan_mechanisms()[self.wrapper.tan_mechanism]

        tan_field = forms.CharField(
            label=tan_param.text_return_value, max_length=tan_param.max_length_input
        )

        form.fields["tan"] = tan_field
        return form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        self.wrapper.open()

        next_step = 4
        if next_step == 4:
            if self.wrapper.do_step4():
                next_step = 5

        resume_id = self.wrapper.save_in_session(self.request)

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.add.step%s" % next_step,
                kwargs={
                    "resume_id": resume_id,
                },
            )
        )


class FinTSLoginCreateStep5View(SessionBasedFinTSWrapperMixin, FormView):
    template_name = "byro_fints/login_add_5.html"
    form_class = LoginCreateStep5Form

    @transaction.atomic
    @with_fints
    def form_valid(self, form):
        display_name = (
            self.wrapper.display_name or self.wrapper.information["bank"]["name"]
        )

        self.wrapper.open()

        fints_login = FinTSLogin.objects.create(
            name=display_name, blz=self.wrapper.blz, fints_url=self.wrapper.fints_url
        )
        fints_user_login = fints_login.user_login.create(
            user=self.request.user, login_name=self.wrapper.login_name
        )

        _fetch_update_accounts(
            fints_user_login,
            self.wrapper.client,
            information=self.wrapper.information,
            accounts=self.wrapper.accounts,
            view=self,
        )

        self.wrapper.close()
        fints_user_login.fints_client_data = self.wrapper.from_data
        fints_user_login.save(update_fields=["fints_client_data"])

        self.wrapper.delete_from_session(self.request)

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.edit",
                kwargs={"pk": fints_user_login.login.pk},
            )
        )
