from django.conf.urls import include, url
import pretix_mercadopago.views as views

from pretix.multidomain import event_url

from .views import (
    oauth_disconnect, redirect_view, success,
)

event_patterns = [
    url(r'^mercadopago/', include([
        url(r'^return/$', success, name='return'),
        url(r'^redirect/$', redirect_view, name='redirect'),

        url(r'w/(?P<cart_namespace>[a-zA-Z0-9]{16})/return/', success, name='return'),

        event_url(r'^webhook/$', success, name='webhook', require_live=False),
    ])),
]


urlpatterns = [
    url(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/mercadopago/',
        views.admin_view, name='backend'),
    url(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/mercadopago/disconnect/',
        oauth_disconnect, name='oauth.disconnect'),
    url(r'^_mercadopago/webhook/$', success, name='webhook'),
]
