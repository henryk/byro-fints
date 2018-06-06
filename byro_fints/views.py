from django import forms
from django.urls import reverse_lazy
from django.utils.translation import ugettext_lazy as _
from django.views.generic import CreateView, FormView, ListView

from .data import get_bank_information_by_blz
from .models import FinTSLogin


class Dashboard(ListView):
    template_name = 'byro_fints/dashboard.html'
    queryset = FinTSLogin.objects.all()
    context_object_name = "fints_logins"


class FinTSLoginCreateView(CreateView):
    template_name = 'byro_fints/login_add.html'
    model = FinTSLogin
    form_class = forms.modelform_factory(FinTSLogin, fields=['blz', 'login_name', 'name', 'fints_url'])
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        for name, field in form.fields.items():
            if name in ['name', 'fints_url']:
                field.required = False
        return form

    def form_valid(self, form):
        bank_information = get_bank_information_by_blz(form.instance.blz)

        form.instance.fints_url = form.instance.fints_url or bank_information.get('PIN/TAN URL', '')
        form.instance.name = form.instance.name or bank_information.get('Institut', '')

        if not form.instance.fints_url:
            raise forms.ValidationError(_("FinTS URL could not be looked up automatically, please fill it in manually."))
        return super().form_valid(form)


class PinRequestForm(forms.Form):
    form_name = _("PIN request")
    pin = forms.CharField(label=_("PIN"), widget=forms.PasswordInput())


class FinTSLoginRefreshView(FormView):
    template_name = 'byro_fints/login_refresh.html'
    form_class = PinRequestForm
    success_url = reverse_lazy('plugins:byro_fints:fints.dashboard')

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        return form

    def form_valid(self, form):
        print(form.cleaned_data['pin'])
        return super().form_valid(form)
