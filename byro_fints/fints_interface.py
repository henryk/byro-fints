import logging
from argparse import Namespace
from contextvars import ContextVar
from functools import wraps, partial
from typing import Optional, Set, Tuple, Dict, ContextManager

from django.contrib import messages
from django.forms import Form
from django_securebox.utils import Storage
from fints.client import FinTS3PinTanClient, FinTSClientMode
from fints.exceptions import FinTSClientPINError

from byro_fints.models import FinTSLogin, FinTSUserLogin

BYRO_FINTS_PRODUCT_ID = "F41CDA6B1F8E0DADA0DDA29FD"
PIN_CACHED_SENTINEL = "******"

logger = logging.getLogger(__name__)
open_clients: ContextVar[Optional[Set[FinTS3PinTanClient]]] = ContextVar('open_clients', default=None)
resumed_dialogs: ContextVar[Optional[Dict[FinTS3PinTanClient, ContextManager]]] = ContextVar('resumed_dialogs',
                                                                                             default=None)
with_fints_active: ContextVar[int] = ContextVar('with_fints_active', default=0)


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


def get_pin_from_form(request, form: Form, login: FinTSLogin):
    if form.cleaned_data["pin"] == PIN_CACHED_SENTINEL:
        return request.securebox[_cache_label(login)]
    else:
        return form.cleaned_data["pin"]


def save_pin_from_form(request, form: Form, login: FinTSLogin):
    if form.cleaned_data["store_pin"] == "1":
        storage = Storage.TRANSIENT_ONLY
    elif form.cleaned_data["store_pin"] == "2":
        storage = Storage.PERMANENT_ONLY
    else:
        storage = None

    if storage:
        if form.cleaned_data["pin"] != PIN_CACHED_SENTINEL:
            request.securebox.store_value(
                _cache_label(login), form.cleaned_data["pin"], storage=storage
            )
    else:
        request.securebox.delete_value(_cache_label(login))


class FinTSImplementation:
    def __init__(self, request, fints_user_login: FinTSUserLogin):
        self.request = request
        self.fints_user_login = fints_user_login
        self.client: Optional[FinTS3PinTanClient] = None
        self.pin = None
        self.pin_error = False

    def readonly_client(self) -> FinTS3PinTanClient:
        return FinTS3PinTanClient(
            self.fints_user_login.login.blz,
            self.fints_user_login.login_name,
            "XXX",
            self.fints_user_login.login.fints_url,
            product_id=BYRO_FINTS_PRODUCT_ID,
            from_data=self.fints_user_login.fints_client_data,
            mode=FinTSClientMode.OFFLINE,
        )

    def update_from_form(self, form: Form):
        self.fints_user_login.login_name = form.cleaned_data["login_name"]
        self.pin = get_pin_from_form(form, self.fints_user_login.login)

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
        # FIXME HACK HACK HACK The python-fints API with regards to TAN media is not very useful yet
        # Circumvent it here
        if self.fints_user_login.selected_tan_medium:
            from fints.formals import TANMedia5
            tan_medium = TANMedia5(
                tan_medium_name=self.fints_user_login.selected_tan_medium
            )
        else:
            tan_medium = None

        try:
            client = open_client(
                self.fints_user_login.login.blz,
                self.fints_user_login.login_name,
                self.pin,
                self.fints_user_login.login.fints_url,
                tan_medium=tan_medium,
                product_id=BYRO_FINTS_PRODUCT_ID,
                from_data=self.fints_user_login.fints_client_data,
                mode=FinTSClientMode.INTERACTIVE,
            )
            client.add_response_callback(self.fints_callback)
            self.client = client

        except FinTSClientPINError:
            # PIN wrong, clear cached PIN, indicate error
            self.request.securebox.delete_value(_cache_label(self.fints_user_login.login))
            self.pin = None
            self.pin_error = True
            return

    def save_from_form(self, form: Form):
        if self.pin is not None and not self.pin_error:
            save_pin_from_form(self.request, form, self.fints_user_login.login)

    def pause(self) -> str:
        if self.client:
            client_data, dialog_data = pause_client(self.client)
            self.fints_user_login.fints_client_data = client_data
            self.fints_user_login.save(update_fields=["fints_client_data"])
            # FIXME dialog_data
        self.client = None

    def resume(self, resume_id: str) -> FinTS3PinTanClient:
        pass

    def close(self):
        if self.client:
            client_data = close_client(self.client, including_private=True)
            self.fints_user_login.fints_client_data = client_data
            self.fints_user_login.save(update_fields=["fints_client_data"])
        self.client = None

    def is_need_tan(self) -> bool:
        pass

    def is_pin_error(self) -> bool:
        pass


def _cache_label(fints_login):
    return "byro_fints__pin__{}__cache".format(fints_login.pk)
