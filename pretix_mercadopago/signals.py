import json
from collections import OrderedDict

from django import forms
# Register your receivers here
from django.dispatch import receiver

from pretix.base.forms import SecretKeySettingsField
from pretix.base.signals import (
    logentry_display, register_global_settings, register_payment_providers,
    requiredaction_display,
)


@receiver(register_payment_providers, dispatch_uid="payment_mercadopago")
def register_payment_provider(sender, **kwargs):
    from .payment import Mercadopago
    return Mercadopago



@receiver(signal=logentry_display, dispatch_uid="mercadopago_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if logentry.action_type != 'pretix.plugins.mercadopago.event':
        return

    data = json.loads(logentry.data)
    event_type = data.get('event_type')
    text = None
    plains = {
        'PAYMENT.SALE.COMPLETED': _('Payment completed.'),
        'PAYMENT.SALE.DENIED': _('Payment denied.'),
        'PAYMENT.SALE.REFUNDED': _('Payment refunded.'),
        'PAYMENT.SALE.REVERSED': _('Payment reversed.'),
    }

    if event_type in plains:
        text = plains[event_type]
    else:
        text = event_type

    if text:
        return _('MercadoPago reported an event: {}').format(text)

@receiver(signal=requiredaction_display, dispatch_uid="mercadopago_requiredaction_display")
def pretixcontrol_action_display(sender, action, request, **kwargs):
    if not action.action_type.startswith('pretix.plugins.mercadopago'):
        return

    data = json.loads(action.data)

#    if action.action_type == 'pretix.plugins.paypal.refund':
#        template = get_template('pretixplugins/paypal/action_refund.html')
#    elif action.action_type == 'pretix.plugins.paypal.overpaid':
#        template = get_template('pretixplugins/paypal/action_overpaid.html')
#    elif action.action_type == 'pretix.plugins.paypal.double':
#        template = get_template('pretixplugins/paypal/action_double.html')

    ctx = {'data': data, 'event': sender, 'action': action}
    return template.render(ctx, request)

@receiver(register_global_settings, dispatch_uid='mercadopago_global_settings')
def register_global_settings(sender, **kwargs):
    return OrderedDict([
        ('payment_mercadopago_connect_client_id', forms.CharField(
            label=_('MercadoPago Connect: Client ID'),
            required=False,
        )),
        ('payment_mercadopago_connect_secret_key', SecretKeySettingsField(
            label=_('MercadoPago Connect: Secret key'),
            required=False,
        )),
        ('payment_mercadopago_connect_endpoint', forms.ChoiceField(
            label=_('MercadoPago Connect Endpoint'),
            initial='live',
            choices=(
                ('live', 'Live'),
                ('sandbox', 'Sandbox'),
            ),
        )),
    ])
