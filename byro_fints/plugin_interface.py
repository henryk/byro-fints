from .models import FinTSUserLogin, FinTSLogin
from .views import FinTSClientFormMixin, PinRequestForm

from fints.models import SEPAAccount

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

        for fints_user_login in FinTSUserLogin.objects.filter(user=self.request.user).all():
            fints_login = fints_user_login.login

            with self.fints_client(fints_login) as client:
                result[fints_login.pk] = client.get_information()

        return result

    def _get_sepa_debit_form(self, fints_login):
        if fints_login in self._fintsinterface_form_cache:
            return self._fintsinterface_form_cache[fints_login]

        kwargs = {
            'prefix': "sepa_debit_form",
        }

        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })

        form = PinRequestForm(**kwargs)
        self.augment_form(form, fints_login)

        self._fintsinterface_form_cache[fints_login] = form

        return form

    def sepa_debit_init(self, login_pk):
        fints_login = FinTSLogin.objects.filter(pk=login_pk, user_login__user=self.request.user).first()
        form = self._get_sepa_debit_form(fints_login)
        return {
            'form': form,
        }

    def sepa_debit_do(self, login_pk, account_iban, **kwargs):
        fints_login = FinTSLogin.objects.filter(pk=login_pk, user_login__user=self.request.user).first()
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

        return response
