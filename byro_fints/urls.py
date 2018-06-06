from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^fints/dashboard$', views.Dashboard.as_view(), name='fints.dashboard'),
    url(r'^fints/login/add$', views.FinTSLoginCreateView.as_view(), name='fints.login.add'),
    url(r'^fints/login/(?P<pk>[0-9]+)/refresh$', views.FinTSLoginRefreshView.as_view(), name='fints.login.refresh'),
]
