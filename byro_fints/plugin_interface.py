from .models import FinTSUserLogin, FinTSLogin
from .views import FinTSClientFormMixin, PinRequestForm

from fints.client import NeedTANResponse


class FinTSInterface(FinTSClientFormMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fintsinterface_form_cache = {}

    @classmethod
    def with_request(cls, request):
        retval = cls()
        retval.request = request
        return retval

    def get_bank_connections(self):
        result = {}

        for fints_login in FinTSLogin.objects.all():
            with self.fints_client(fints_login) as client:
                result[fints_login.pk] = client.get_information()

        return result

    def _common_get_form(self, form_type, fints_login, extra_fields={}):
        cache_key = (form_type, fints_login)
        if cache_key in self._fintsinterface_form_cache:
            return self._fintsinterface_form_cache[cache_key]

        kwargs = {
            'prefix': "fints_form_{}".format(form_type),
        }

        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })

        form = PinRequestForm(**kwargs)
        self.augment_form(form, fints_login, extra_fields)

        self._fintsinterface_form_cache[cache_key] = form

        return form

    def _get_sepa_debit_form(self, fints_login):
        return self._common_get_form('sepa_debit', fints_login)

    def _get_tan_request_form(self, fints_login, tan_request_data):
        extra_fields = self.get_tan_form_fields(
            fints_login,
            tan_request_data,
        )
        return self._common_get_form('tan_request', fints_login, extra_fields=extra_fields)

    def sepa_debit_init(self, login_pk):
        fints_login = FinTSLogin.objects.filter(pk=login_pk).first()
        form = self._get_sepa_debit_form(fints_login)
        return {
            'form': form,
        }

    def sepa_debit_do(self, login_pk, account_iban, **kwargs):
        fints_login = FinTSLogin.objects.filter(pk=login_pk).first()
        form = self._get_sepa_debit_form(fints_login)

        with self.fints_client(fints_login, form) as client:
            with client:
                account_list = client.get_sepa_accounts()
                for account in account_list:
                    if account.iban.upper() == account_iban.upper():
                        break
                else:
                    return "ACCOUNT NOT AVAILABLE"

                response = client.sepa_debit(
                    account=account,
                    **kwargs
                )

                # FIXME This API

                if isinstance(response, NeedTANResponse):
                    transfer_uuid = self.pause_for_tan_request(client, response)
                    return transfer_uuid
                else:
                    return response

    def tan_request_init(self, login_pk, transfer_uuid):
        fints_login = FinTSLogin.objects.filter(pk=login_pk).first()
        tan_request_data = self._tan_request_data(transfer_uuid)

        form = self._get_tan_request_form(fints_login, tan_request_data)
        context = self.get_tan_context_data(tan_request_data)

        return {
            'form': form,
            'context': context,
            'template': 'byro_fints/snippet_tan_request.html',
        }

    def tan_request_send_tan(self, login_pk, transfer_uuid):
        fints_login = FinTSLogin.objects.filter(pk=login_pk).first()
        tan_request_data = self._tan_request_data(transfer_uuid)

        form = self._get_tan_request_form(fints_login, tan_request_data)
        with self.fints_client(fints_login, form) as client:
            with client:
                resume_dialog, response, other_data = self.resume_from_tan_request(client, transfer_uuid)

                with resume_dialog:
                    response = client.send_tan(response, form.cleaned_data['tan'].strip())
                    return response

    def tan_request_fini(self, transfer_uuid):
        self.clean_tan_request(transfer_uuid)
