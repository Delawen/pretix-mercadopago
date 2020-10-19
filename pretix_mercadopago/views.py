import logging
from decimal import Decimal

import mercadopago
from django.contrib import messages
from django.core import signing
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Quota
from pretix.base.payment import PaymentException
from pretix.control.permissions import event_permission_required
from pretix.multidomain.urlreverse import eventreverse
from pretix_mercadopago.payment import Mercadopago

logger = logging.getLogger('pretix.plugins.meli')


def admin_view(request, *args, **kwargs):
    r = render(request, 'pretix_mercadopago/admin.html', {
    })
    r._csp_ignore = True
    return r


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    signer = signing.Signer(salt='safe-redirect')
    try:
        url = signer.unsign(request.GET.get('url', ''))
    except signing.BadSignature:
        return HttpResponseBadRequest('Invalid parameter')

    r = render(request, 'pretix_mercadopago/redirect.html', {
        'url': url,
    })
    r._csp_ignore = True
    return r

# Return url for MercadoPago when payment is pending or success
@csrf_exempt
def success(request, *args, **kwargs):
    collection_id = request.GET.get('collection_id')
    status = request.GET.get('collection_status')

    # Ask MercadoPago again about the status
    # to avoid pishing!
    # (don't trust any call to this url)
    mp = Mercadopago(request.event).init_api()
    paymentInfo = mp.get_payment(collection_id)

    payment = None
    orderid = None
    if paymentInfo["status"] == 200:
        orderid = paymentInfo['response']['external_reference']
        payment = OrderPayment.objects.get(pk=orderid)
    else:
        messages.error(request, _('Invalid attempt update payment details of ' + collection_id))
        return None

    # Documentation for payment object:
    # https://www.mercadopago.com.ar/developers/es/reference/payments/resource/
    if payment:
        order = payment.order
        mpstatus = paymentInfo['response']['status']

        # Something fishy detected
        if status != mpstatus:
            messages.error(request, _('Invalid attempt to pay order ' + orderid))

        # Update with what MercadoPago has
        if mpstatus == 'approved':
            payment.order.status = Order.STATUS_PAID
            try:
                payment.confirm()
            except Quota.QuotaExceededException:
                messages.error(request, _('Quota exceeded with order ' + orderid))
        elif (mpstatus == 'pending') or (mpstatus == 'authorized') or (mpstatus == 'in_process') or (mpstatus == 'in_mediation'):
            payment.order.status = Order.STATUS_PENDING
            payment.state = 'pending'
        elif (mpstatus == 'cancelled'):
            payment.order.status = Order.STATUS_CANCELED
            payment.fail(info={
                'error': True,
                'message': _('Payment Cancelled'),
            })
        elif (mpstatus == 'rejected'):
            payment.order.status = Order.STATUS_CANCELED
            payment.fail(info={
                'error': True,
                'message': _('Payment Rejected'),
            })
        elif (mpstatus == 'refunded') or (mpstatus == 'charged_back'):
            payment.order.status = Order.STATUS_CANCELED
            payment.state = 'refunded'

        payment.info = paymentInfo['response']['status_detail']
        payment.order.save()
        payment.save()

        return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
            'order': payment.order.code,
            'secret': payment.order.secret
        }) + ('?paid=yes' if payment.order.status == Order.STATUS_PAID else ''))
    else:
        messages.error(request, _('Invalid attempt update payment details of ' + collection_id))
        urlkwargs['step'] = 'confirm'
        return redirect(eventreverse(request.event, 'presale:event.checkout', kwargs=urlkwargs))


@event_permission_required('can_change_event_settings')
@require_POST
def oauth_disconnect(request, **kwargs):
    del request.event.settings.payment_mercadopago_connect_refresh_token
    del request.event.settings.payment_mercadopago_connect_user_id
    request.event.settings.payment_mercadopago__enabled = False
    messages.success(request, _('Your MercadoPago account has been disconnected.'))

    return redirect(reverse('control:event.settings.payment.provider', kwargs={
        'organizer': request.event.organizer.slug,
        'event': request.event.slug,
        'provider': 'mercadopago'
    }))
