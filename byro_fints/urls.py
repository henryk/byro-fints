from django.conf.urls import url

from . import views

urlpatterns = [
    url(
        r"^fints/dashboard$", views.Dashboard.as_view(), name="finance.fints.dashboard"
    ),
    url(
        r"^fints/login/add$",
        views.FinTSLoginCreateView.as_view(),
        name="finance.fints.login.add",
    ),
    url(
        r"^fints/login/(?P<pk>[0-9]+)/$",
        views.FinTSLoginEditView.as_view(),
        name="finance.fints.login.edit",
    ),
    url(
        r"^fints/login/(?P<pk>[0-9]+)/refresh$",
        views.FinTSLoginRefreshView.as_view(),
        name="finance.fints.login.refresh",
    ),
    url(
        r"^fints/login/(?P<pk>[0-9]+)/tan/(?P<uuid>[0-9a-fA-F-]+|test_data(?:_2)?)$",
        views.FinTSLoginTANRequestView.as_view(),
        name="finance.fints.login.tan_request",
    ),
    url(
        r"^fints/account/(?P<pk>[0-9]+)/link$",
        views.FinTSAccountLinkView.as_view(),
        name="finance.fints.account.link",
    ),
    url(
        r"^fints/account/(?P<pk>[0-9]+)/fetch$",
        views.FinTSAccountFetchView.as_view(),
        name="finance.fints.account.fetch",
    ),
    url(
        r"^fints/account/(?P<pk>[0-9]+)/transfer$",
        views.FinTSAccountTransferView.as_view(),
        name="finance.fints.account.transfer",
    ),
    url(
        r"^fints/account/(?P<pk>[0-9]+)/information$",
        views.FinTSAccountInformationView.as_view(),
        name="finance.fints.account.information",
    ),
]
