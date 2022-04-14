from django import forms
from django.utils.translation import ugettext_lazy as _

from byro_fints.data import get_bank_information_by_blz


class PinRequestForm(forms.Form):
    form_name = _("PIN request")
    login_name = forms.CharField(label=_("Login name"), required=True)
    pin = forms.CharField(
        label=_("PIN"), widget=forms.PasswordInput(render_value=True), required=True
    )
    store_pin = forms.ChoiceField(
        label=_("Store PIN?"),
        choices=[
            ["0", _("Don't store PIN")],
            ["1", _("For this login session only")],
            ["2", _("Store PIN (encrypted with account password)")],
        ],
        initial="0",
    )


class LoginCreateStep1Form(PinRequestForm):
    form_name = _("Create FinTS login")
    field_order = ["blz", "login_name", "pin"]

    blz = forms.CharField(label=_("Routing number (BLZ)"), required=True)
    name = forms.CharField(label=_("Display name"), required=False)
    fints_url = forms.CharField(label=_("FinTS URL"), required=False)

    def clean(self):
        retval = super().clean()
        if not retval["fints_url"]:
            bank_information = get_bank_information_by_blz(retval["blz"])
            fints_url = bank_information.get("PIN/TAN-Zugang URL", "")
            if fints_url:
                retval["fints_url"] = fints_url
            else:
                self.add_error(
                    "fints_url",
                    _(
                        "FinTS URL could not be looked up automatically, please fill it in manually."
                    ),
                )
        return retval


class LoginCreateStep2Form(forms.Form):
    form_name = _("Set TAN mechanism")


class LoginCreateStep3Form(forms.Form):
    form_name = _("Set TAN medium")


class LoginCreateStep4Form(forms.Form):
    form_name = _("Confirm with TAN")


class LoginCreateStep5Form(forms.Form):
    form_name = _("Save FinTS login")
