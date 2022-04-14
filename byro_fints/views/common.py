from base64 import b64encode, b64decode

from fints.client import FinTSOperations
from fints.formals import DescriptionRequired

from byro_fints.models import FinTSAccountCapabilities, FinTSAccount

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


def _encode_binary_for_session(data: bytes) -> str:
    return b64encode(data).decode("us-ascii")


def _decode_binary_for_session(data: str) -> bytes:
    return b64decode(data.encode("us-ascii"))
