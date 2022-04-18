import abc
import pickle
import uuid
from base64 import b64encode
from enum import Enum
from typing import Dict, List, Optional, Tuple

from django import forms
from django.db import transaction
from django.db.transaction import atomic
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView
from django_securebox.utils import Storage
from fints.client import FinTS3PinTanClient, FinTSClientMode, NeedTANResponse
from fints.exceptions import FinTSClientPINError
from fints.hhd.flicker import parse as hhd_flicker_parse
from fints.types import SegmentSequence

from byro_fints.fints_interface import (
    BYRO_FINTS_PRODUCT_ID,
    close_client,
    open_client,
    pause_client,
    resume_client,
    with_fints,
)
from byro_fints.forms import (
    LoginCreateStep1Form,
    LoginCreateStep2Form,
    LoginCreateStep3Form,
    LoginCreateStep4Form,
    LoginCreateStep5Form,
)
from byro_fints.models import FinTSLogin, FinTSUserLogin
from byro_fints.views.common import (
    _decode_binary_for_session,
    _encode_binary_for_session,
    _fetch_update_accounts,
)


class PinState(Enum):
    NONE = "none"
    DONTSAVE = "dontsave"
    SAVE_ON_RESUME = "save_on_resume"
    SAVE_TEMPORARY = "save_temporary"
    SAVE_PERSISTENT = "save_persistent"


class AbstractFinTSWrapper(metaclass=abc.ABCMeta):
    SAVE_PIN_IN_RESUME = False

    def __init__(self, request):
        # Volatile state
        self.request = request
        self.resume_id: Optional[str] = str(uuid.uuid4())
        self._pin: Optional[str] = None
        self.client: Optional[FinTS3PinTanClient] = None

        # Saved state
        self.pin_state: PinState = PinState.NONE
        self.dialog_data: Optional[bytes] = None
        self.tan_request_serialized: Optional[bytes] = None
        self.tan_mechanism: Optional[str] = None
        self.tan_medium: Optional[str] = None
        self.tan_mechanisms: Optional[Dict[str, str]] = None
        self.tan_media: Optional[List[str]] = None

    def augment_form_pin_fields(self, form: forms.Form):
        # FIXME Implement sentinel initial

        if "pin" not in form.fields:
            form.fields["pin"] = forms.CharField(
                label=_("PIN"),
                widget=forms.PasswordInput(render_value=True),
                required=True,
            )
        if "store_pin" not in form.fields:
            form.fields["store_pin"] = forms.ChoiceField(
                label=_("Store PIN?"),
                choices=[
                    [PinState.DONTSAVE.value, _("Don't store PIN")],
                    [PinState.SAVE_TEMPORARY.value, _("For this login session only")],
                    [
                        PinState.SAVE_PERSISTENT.value,
                        _("Store PIN (encrypted with account password)"),
                    ],
                ],
                initial=self.pin_state.value,
            )

    @property
    def pin(self) -> Optional[str]:
        if self.pin_state in (
            PinState.NONE,
            PinState.DONTSAVE,
            PinState.SAVE_ON_RESUME,
        ):
            return self._pin
        else:
            return self.request.securebox[
                self.resume_label + "/pin"
            ]  # FIXME By fints user login

    def load_from_form(self, form: forms.Form):
        if "pin" in form.cleaned_data:
            if "store_pin" in form.cleaned_data:
                store_pin = PinState(form.cleaned_data["store_pin"])
            else:
                store_pin = PinState.DONTSAVE

            # FIXME Compare with SENTINEL
            # FIXME Store pin by fints user login

            if self.SAVE_PIN_IN_RESUME and store_pin == PinState.DONTSAVE:
                self._pin = form.cleaned_data["pin"]
                self.pin_state = PinState.SAVE_ON_RESUME
            else:
                self.pin_state = store_pin

    @property
    def resume_label(self):
        return "byro_fints:resume:%s" % self.resume_id

    def _get_data_for_session(self) -> Tuple:
        return (
            self.pin_state,
            self.dialog_data,
            self.tan_request_serialized,
            self.tan_mechanism,
            self.tan_medium,
            self.tan_mechanisms,
            self.tan_media,
        )

    @atomic
    def save_in_session(self) -> str:
        if self.client:
            client_data, self.dialog_data = pause_client(self.client)
            self._do_save_client_data(client_data)
            self.client = None

        data = (self.__class__.__name__,) + self._get_data_for_session()
        self.request.session[self.resume_label] = _encode_binary_for_session(
            pickle.dumps(data)
        )

        # PIN saved under resume_id is saved here
        if self.pin_state is PinState.SAVE_ON_RESUME:
            self.request.securebox.store_value(
                self.resume_label + "/pin", self._pin, storage=Storage.TRANSIENT_ONLY
            )

        return self.resume_id

    def _set_data_from_session(self, data):
        (
            self.pin_state,
            self.dialog_data,
            self.tan_request_serialized,
            self.tan_mechanism,
            self.tan_medium,
            self.tan_mechanisms,
            self.tan_media,
        ) = data

    @classmethod
    def restore_from_session(cls, request, resume_id: str):
        retval = cls(request)
        retval.resume_id = resume_id
        data = pickle.loads(
            _decode_binary_for_session(request.session[retval.resume_label])
        )
        assert data[0] == retval.__class__.__name__
        retval._set_data_from_session(data[1:])

        if retval.pin_state is PinState.SAVE_ON_RESUME:
            retval._pin = request.securebox[retval.resume_label + "/pin"]

        return retval

    def delete_from_session(self):
        del self.request.session[self.resume_label]
        self.request.securebox.delete_value(self.resume_label + "/pin")

    def open(self):
        if not self.client:
            client_args = self._get_client_args()
            args = client_args[0:2] + (self.pin,) + client_args[2:]
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
                self.dialog_data = None
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
            (self.client if self.client else self.get_readonly_client()).is_challenge_structured(),
        )

    def reopen(self, **kwargs):
        self.close()
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.open()

    def close(self):
        if self.client:
            from_data = close_client(self.client, including_private=True)
            self._do_save_client_data(from_data)
            self.client = None

    def get_readonly_client(self) -> FinTS3PinTanClient:
        base_args = self._get_client_args()
        client = FinTS3PinTanClient(
            base_args[0],
            base_args[1],
            "XXX",
            base_args[2],
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

    @abc.abstractmethod
    def _do_save_client_data(self, client_data: bytes):
        """Take client_data and save it"""

    @abc.abstractmethod
    def _get_client_args(self) -> Tuple[str, str, str]:
        """Must return (blz, login_name, fints_url)"""
        pass

    @property
    @abc.abstractmethod
    def from_data(self) -> bytes:
        pass


class FinTSWrapper(AbstractFinTSWrapper):
    def __init__(self, request):
        super().__init__(request)
        self.user_login_pk: Optional[int] = None

    def load_from_user_login(self, user_login_pk: int):
        self.user_login_pk = user_login_pk
        # FIXME Set PIN state

    @property
    def pin_label(self):
        user_login = self.get_user_login()
        assert user_login is not None
        return "byro_fints__pin__{}__cache".format(user_login.login.pk)

    @property
    def pin(self) -> str:
        raise NotImplemented  # FIXME

    def save_pin(self, pin_state: PinState, pin: str):
        raise NotImplemented  # FIXME

    def _get_client_args(self) -> Tuple[str, str, str]:
        user_login = self.get_user_login()
        return user_login.login.blz, user_login.login_name, user_login.login.fints_url

    @property
    def from_data(self) -> bytes:
        return self.get_user_login().fints_client_data

    def get_user_login(self) -> Optional[FinTSUserLogin]:
        if self.user_login_pk is None:
            return None
        return (
            FinTSUserLogin.objects.filter(pk=self.user_login_pk, user=self.request.user)
            .select_related("login")
            .first()
        )

    def _do_save_client_data(self, client_data: bytes):
        user_login = self.get_user_login()
        user_login.fints_client_data = client_data
        user_login.save(update_fields=["fints_client_data"])


class FinTSWrapperAddProcess(AbstractFinTSWrapper):
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
        if self.tan_request:
            if tan is None:
                return False
            else:
                self.client.send_tan(self.tan_request, tan)

        self.accounts = self.client.get_sepa_accounts()
        self.information = self.client.get_information()
        return True


class SessionBasedFinTSWrapperMixin:
    def __init__(self):
        super().__init__()
        self.wrapper: Optional[FinTSWrapperAddProcess] = None

    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        if "resume_id" in self.kwargs:
            self.wrapper = FinTSWrapperAddProcess.restore_from_session(
                self.request, self.kwargs["resume_id"]
            )
        else:
            self.wrapper = FinTSWrapperAddProcess(self.request)
            login = self.request.GET.get("login", None)
            if login:
                login_pk = int(login)
                self.wrapper.load_from_login(login_pk)


class FinTSLoginCreateStep1View(SessionBasedFinTSWrapperMixin, FormView):
    template_name = "byro_fints/login_add_1.html"
    form_class = LoginCreateStep1Form

    def get_form(self, *args, **kwargs):
        form: forms.Form = super().get_form(*args, **kwargs)
        self.wrapper.augment_form_pin_fields(form)
        if self.wrapper.login_pk:
            login = self.wrapper.login
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
        self.wrapper.load_from_form(form)

        try:
            self.wrapper.open()

            tan_mechanisms = self.wrapper.get_tan_mechanisms()
            if len(tan_mechanisms) == 0:
                form.add_error(None, _("Can't find TAN mechanism"))
                return self.form_invalid(form)

            next_step = 2  # select tan mechanism
            if self.wrapper.do_step2():
                next_step = 3  # select tan media

            if next_step == 3:
                if self.wrapper.do_step3():
                    next_step = 4  # fetch account info

            if next_step == 4:
                if self.wrapper.do_step4():
                    next_step = 5

            resume_id = self.wrapper.save_in_session()

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
            self.wrapper.close()


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

        resume_id = self.wrapper.save_in_session()

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

        resume_id = self.wrapper.save_in_session()

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
                from .common import get_flicker_css

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
    def form_valid(self, form: forms.Form):
        self.wrapper.open()

        next_step = 4
        if next_step == 4:
            if self.wrapper.do_step4(tan=form.cleaned_data["tan"]):
                next_step = 5

        resume_id = self.wrapper.save_in_session()

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

        if self.wrapper.login:
            fints_login = self.wrapper.login
        else:
            fints_login, _ = FinTSLogin.objects.get_or_create(
                name=display_name,
                blz=self.wrapper.blz,
                fints_url=self.wrapper.fints_url,
            )

        fints_user_login, _ = fints_login.user_login.get_or_create(
            user=self.request.user
        )
        fints_user_login.login_name = self.wrapper.login_name

        _fetch_update_accounts(
            fints_user_login,
            self.wrapper.client,
            information=self.wrapper.information,
            accounts=self.wrapper.accounts,
            view=self,
        )

        self.wrapper.close()
        fints_user_login.available_tan_media = (
            [{"name": e} for e in self.wrapper.tan_media]
            if self.wrapper.tan_media
            else []
        )
        fints_user_login.selected_tan_medium = self.wrapper.tan_medium
        fints_user_login.fints_client_data = self.wrapper.from_data
        fints_user_login.save()

        # FIXME
        # if self.wrapper.pin_state_shouldbe in (PinState.SAVE_TEMPORARY, PinState.SAVE_PERSISTENT):
        #     new_wrapper = FinTSWrapper(self.request)
        #     new_wrapper.load_from_user_login(fints_user_login.pk)
        #     new_wrapper.save_pin(self.wrapper.pin_state_shouldbe, self.wrapper.pin)

        self.wrapper.delete_from_session()

        return HttpResponseRedirect(
            reverse(
                "plugins:byro_fints:finance.fints.login.edit",
                kwargs={"pk": fints_user_login.login.pk},
            )
        )
