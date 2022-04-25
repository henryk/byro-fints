from base64 import b64decode, b64encode

from fints.client import FinTSOperations
from fints.formals import DescriptionRequired

from ..fints_interface import SessionBasedFinTSHelperMixin
from ..models import FinTSAccount, FinTSAccountCapabilities

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


def _fetch_update_accounts(
    fints_user_login, client, accounts=None, information=None, view=None
):
    fints_login = fints_user_login.login
    accounts = accounts or client.get_sepa_accounts()
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
                    r"{}% {{ background-color: {}; }}".format(index * per_frame, color)
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


class SessionBasedExisitingUserLoginFinTSHelperMixin(SessionBasedFinTSHelperMixin):
    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        if self.fints.user_login_pk is None:
            login = self.get_object()
            if login:
                user_login = login.user_login.filter(
                    user=self.request.user
                ).first()
                if user_login:
                    self.fints.load_from_user_login(user_login.pk)
