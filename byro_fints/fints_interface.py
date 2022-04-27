import abc
import logging
import pickle
import uuid
from argparse import Namespace
from base64 import b64encode, b64decode
from contextvars import ContextVar
from enum import Enum
from functools import wraps, partial
from typing import Optional, Set, Tuple, Dict, ContextManager, List, TypeVar, Callable
from fints.hhd.flicker import parse as hhd_flicker_parse

import django.http
from django import forms
from django.contrib import messages
from django.db.transaction import atomic
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django_securebox.utils import Storage
from fints.client import FinTS3PinTanClient, FinTSClientMode, NeedTANResponse
from fints.exceptions import FinTSClientPINError
from fints.types import SegmentSequence

from .models import FinTSUserLogin

BYRO_FINTS_PRODUCT_ID = "F41CDA6B1F8E0DADA0DDA29FD"
PIN_CACHED_SENTINEL = "******"

logger = logging.getLogger(__name__)
open_clients: ContextVar[Optional[Set[FinTS3PinTanClient]]] = ContextVar('open_clients', default=None)
resumed_dialogs: ContextVar[Optional[Dict[FinTS3PinTanClient, ContextManager]]] = ContextVar('resumed_dialogs',
                                                                                             default=None)
with_fints_active: ContextVar[int] = ContextVar('with_fints_active', default=0)


def _encode_binary_for_session(data: bytes) -> str:
    return b64encode(data).decode("us-ascii")


def _decode_binary_for_session(data: str) -> bytes:
    return b64decode(data.encode("us-ascii"))


def with_fints(wrapped):
    @wraps(wrapped)
    def f(*args, **kwargs):
        try:
            with_fints_active.set(with_fints_active.get() + 1)
            return wrapped(*args, **kwargs)
        finally:
            try:
                _ensure_close_clients()
            finally:
                with_fints_active.set(with_fints_active.get() - 1)

    return f


def _ensure_close_clients():
    rd = resumed_dialogs.get() or dict()
    for client in (open_clients.get() or set()):
        logger.error("Client %s was not closed", client)
        if client in rd:
            rd[client].__exit__(None, None, None)
        else:
            client.__exit__(None, None, None)
    open_clients.set(set())
    resumed_dialogs.set(dict())


def _inner_open_client(*args, tan_medium_name=None, tan_mechanism=None, **kwargs) -> FinTS3PinTanClient:
    assert with_fints_active.get(), "May only call open_client() in function marked with @with_fints."
    client = FinTS3PinTanClient(*args, **kwargs)

    # Note: This doesn't belong here, but needs to be called before __enter__
    if tan_mechanism is not None:
        client.set_tan_mechanism(tan_mechanism)
    if tan_medium_name is not None:
        # HACK HACK HACK We can't restore the TanMedia objects, and set_tan_medium uses only the name anyway
        fake_tan_medium = Namespace(tan_medium_name=tan_medium_name)
        client.set_tan_medium(fake_tan_medium)

    return client


def open_client(*args, **kwargs) -> FinTS3PinTanClient:
    """Return an open FinTS3PinTanClient. Function *must* be annotated with @with_fints.
    The client will be created with an open dialog. You may need to handle a TAN request
    if client.init_tan_response is set. You need to remember to call close_client()
    within the same method."""
    ocl = open_clients.get() or set()
    client = _inner_open_client(*args, **kwargs)
    client.__enter__()
    ocl.add(client)
    open_clients.set(ocl)
    return client


def pause_client(client: FinTS3PinTanClient) -> Tuple[bytes, bytes]:
    """Pause the ongoing dialog and return a tuple of client_data, dialog_data.
    You cannot call client methods afterwards. You generally should only pause
    a dialog if you need to get user TAN input. Normal exit should close the client.
    Note: FinTS3PinTanClient.deconstruct() will always be called with
    including_private=True, since it doesn't make sense to pause a dialog otherwise."""
    ocl = open_clients.get() or set()
    assert client in ocl
    dialog_data = client.pause_dialog()
    client_data = close_client(client, True)
    return client_data, dialog_data


def resume_client(*args, client_data, dialog_data, **kwargs) -> FinTS3PinTanClient:
    """Resume a previously paused dialog. You need to provide all FinTS3PinTanClient
    constructor arguments and client_data and dialog_data keyword arguments (as returned
    from pause_client)."""
    ocl = open_clients.get() or set()
    rd = resumed_dialogs.get() or dict()
    client = _inner_open_client(from_data=client_data, *args, **kwargs)
    rd[client] = client.resume_dialog(dialog_data)
    rd[client].__enter__()
    ocl.add(client)
    open_clients.set(ocl)
    resumed_dialogs.set(rd)
    return client


def close_client(client: FinTS3PinTanClient, including_private: bool = False) -> bytes:
    """Close a client (and dialog). Call this before exiting your function.
    Same argument and return value as FinTS3PinTanClient.deconstruct()."""
    ocl = open_clients.get() or set()
    rd = resumed_dialogs.get() or dict()
    assert client in ocl
    client_data = client.deconstruct(including_private=including_private)
    ocl.remove(client)
    open_clients.set(ocl)
    if client in rd:
        rd[client].__exit__(None, None, None)
        del rd[client]
        resumed_dialogs.set(rd)
    else:
        client.__exit__(None, None, None)
    return client_data


class PinState(Enum):
    NONE = "none"
    DONTSAVE = "dontsave"
    SAVE_ON_RESUME = "save_on_resume"
    SAVE_TEMPORARY = "save_temporary"
    SAVE_PERSISTENT = "save_persistent"


class AbstractFinTSHelper(metaclass=abc.ABCMeta):
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
        self.init_tan_request_serialized: Optional[bytes] = None
        self.tan_request_serialized: Optional[bytes] = None
        self.tan_mechanism: Optional[str] = None
        self.tan_medium: Optional[str] = None
        self.tan_mechanisms: Optional[Dict[str, str]] = None
        self.tan_media: Optional[List[str]] = None

    def augment_form_pin_fields(self, form: forms.Form):
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

    def augment_form_tan_fields(self, form: forms.Form):
        client = self.get_readonly_client()
        tan_param = client.get_tan_mechanisms()[client.get_current_tan_mechanism()]
        tan_field = forms.CharField(
            label=tan_param.text_return_value, max_length=tan_param.max_length_input
        )
        form.fields["tan"] = tan_field

    @property
    def pin(self) -> Optional[str]:
        if self.pin_state in (
            PinState.NONE,
            PinState.DONTSAVE,
            PinState.SAVE_ON_RESUME,
        ):
            return self._pin
        raise NotImplemented

    def save_pin(self, pin_state: PinState, pin: str):
        """Based on PinState, save pin in corresponding place, then update self.pin_state"""
        if pin_state in (PinState.SAVE_TEMPORARY, PinState.SAVE_PERSISTENT):
            raise NotImplemented
        # Note: We're *not* saving in session for PinState.SAVE_ON_RESUME here, but instead
        # that's handled by store and resume
        self._pin = pin
        self.pin_state = pin_state

    def load_from_form(self, form: forms.Form):
        if "pin" in form.cleaned_data:
            if "store_pin" in form.cleaned_data:
                store_pin = PinState(form.cleaned_data["store_pin"])
            else:
                store_pin = PinState.DONTSAVE

            if self.SAVE_PIN_IN_RESUME and store_pin == PinState.DONTSAVE:
                store_pin = PinState.SAVE_ON_RESUME

            pin = form.cleaned_data["pin"]
            if pin != PIN_CACHED_SENTINEL:
                self.save_pin(store_pin, pin)

    @property
    def resume_label(self):
        return "byro_fints:resume:%s" % self.resume_id

    def _get_data_for_session(self) -> Tuple:
        return (
            self.pin_state,
            self.dialog_data,
            self.init_tan_request_serialized,
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
            self.init_tan_request_serialized,
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
                    self.init_tan_request_serialized = SegmentSequence(
                        [self.client.init_tan_response.init_tan_request]
                    ).render_bytes()
            self.client.add_response_callback(self.fints_callback)
            # FIXME Handle FinTSClientPINError
        # except FinTSClientPINError:
        #     # PIN wrong, clear cached PIN, indicate error
        #     self.request.securebox.delete_value(_cache_label(fints_login))
        #     if form:
        #         form.add_error(
        #             None, _("Can't establish FinTS dialog: Username/PIN wrong?")
        #         )
        #     pin_correct = False
        #

    @property
    def init_tan_request(self):
        # FIXME See python-fints#114
        if not self.init_tan_request_serialized:
            return None
        return NeedTANResponse(
            None,
            SegmentSequence(self.init_tan_request_serialized).segments[0],
            "_continue_dialog_initialization",
            (self.client if self.client else self.get_readonly_client()).is_challenge_structured(),
        )

    @property
    def tan_request(self):
        if not self.tan_request_serialized:
            return None
        return NeedTANResponse.from_data(self.tan_request_serialized)

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
        if self.tan_mechanism is not None:
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

    @staticmethod
    def get_tan_context_data(tan_request):
        tan_context = {}
        if tan_request:
            tan_context = {"challenge": mark_safe(tan_request.challenge_html)}

            if tan_request.challenge_hhduc:
                flicker = hhd_flicker_parse(tan_request.challenge_hhduc)
                tan_context["challenge_flicker"] = flicker.render()

                css_class = "flicker-{}".format(uuid.uuid4())
                tan_context["challenge_flicker_css_class"] = css_class
                from .views.common import get_flicker_css

                tan_context["challenge_flicker_css"] = lambda: get_flicker_css(
                    flicker.render(), css_class
                )

            if tan_request.challenge_matrix:
                tan_context["challenge_matrix_url"] = "data:{};base64,{}".format(
                    tan_request.challenge_matrix[0],
                    b64encode(tan_request.challenge_matrix[1]).decode("us-ascii"),
                )
        return tan_context

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


class FinTSHelper(AbstractFinTSHelper):
    def __init__(self, request):
        super().__init__(request)
        self.user_login_pk: Optional[int] = None

    def _get_data_for_session(self) -> Tuple:
        return super()._get_data_for_session() + (
            self.user_login_pk,
        )

    def _set_data_from_session(self, data):
        super()._set_data_from_session(data[:-1])
        (self.user_login_pk, ) = data[-1:]

    def augment_form_pin_fields(self, form: forms.Form):
        super().augment_form_pin_fields(form)
        user_login = self.get_user_login()
        if user_login:
            if 'login_name' in form.fields:
                form.fields['login_name'].initial = user_login.login_name
        if self.pin_state in (PinState.DONTSAVE, PinState.SAVE_TEMPORARY, PinState.SAVE_PERSISTENT):
            form.fields["store_pin"].initial = self.pin_state.value
        if self.pin_state in (PinState.SAVE_PERSISTENT, PinState.SAVE_TEMPORARY):
            form.fields["pin"].initial = PIN_CACHED_SENTINEL

    def load_from_form(self, form: forms.Form):
        super().load_from_form(form)
        user_login = self.get_user_login()
        if user_login:
            if 'login_name' in form.fields and form.cleaned_data.get("login_name", "").strip():
                user_login.login_name = form.cleaned_data['login_name'].strip()
                user_login.save(update_fields=["login_name"])

    def _restore_pin_state_from_securebox(self):
        if self.user_login_pk is None:
            return
        if self.request.securebox.fetch_value(self.pin_label, Storage.TRANSIENT_ONLY, default=None) is not None:
            self.pin_state = PinState.SAVE_TEMPORARY
        elif self.request.securebox.fetch_value(self.pin_label, Storage.PERMANENT_ONLY, default=None) is not None:
            self.pin_state = PinState.SAVE_PERSISTENT

    def load_from_user_login(self, user_login_pk: int):
        self.user_login_pk = user_login_pk
        self._restore_pin_state_from_securebox()
        user_login: FinTSUserLogin = self.get_user_login()
        self.tan_medium = user_login.selected_tan_medium

    @classmethod
    def restore_from_session(cls, request, resume_id: str):
        retval: FinTSHelper = super().restore_from_session(request, resume_id)
        retval._restore_pin_state_from_securebox()
        return retval

    @property
    def pin_label(self):
        user_login = self.get_user_login()
        assert user_login is not None
        return "byro_fints__pin__{}__cache".format(user_login.login.pk)

    @property
    def pin(self) -> str:
        if self.user_login_pk is not None:
            pin = self.request.securebox.fetch_value(self.pin_label, default=None)
            if pin is not None:
                return pin
        return super().pin

    def save_pin(self, pin_state: PinState, pin: str):
        """Save pin in securebox, if requested."""
        storage = None
        if pin_state == PinState.SAVE_TEMPORARY:
            storage = Storage.TRANSIENT_ONLY
        elif pin_state == PinState.SAVE_PERSISTENT:
            storage = Storage.PERMANENT_ONLY
        if storage is not None:
            self.request.securebox.store_value(self.pin_label, pin, storage=storage)
            self.pin_state = pin_state
        else:
            return super().save_pin(pin_state, pin)

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


_HELPER_CLASS_CLASS = TypeVar("_HELPER_CLASS_CLASS")


class SessionBasedFinTSHelperMixin:
    HELPER_CLASS: _HELPER_CLASS_CLASS = FinTSHelper
    request: django.http.HttpRequest
    kwargs: dict

    def __init__(self):
        super().__init__()
        self.fints: Optional[_HELPER_CLASS_CLASS] = None

    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        if "resume_id" in self.kwargs:
            self.fints = self.HELPER_CLASS.restore_from_session(
                self.request, self.kwargs["resume_id"]
            )
        else:
            self.fints = self.HELPER_CLASS(self.request)
