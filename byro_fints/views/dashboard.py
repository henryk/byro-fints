from django.views.generic import ListView

from ..models import FinTSAccount, FinTSLogin, FinTSUserLogin


class Dashboard(ListView):
    template_name = "byro_fints/dashboard.html"
    queryset = FinTSLogin.objects.order_by("blz").all()
    context_object_name = "fints_logins"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(
                user_login__in=FinTSUserLogin.objects.filter(user=self.request.user)
            )
        )

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context["fints_accounts"] = FinTSAccount.objects.order_by("iban").all()
        context["inactive_logins"] = (
            super().get_queryset().exclude(pk__in=self.get_queryset())
        )
        return context
