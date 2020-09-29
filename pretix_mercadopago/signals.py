import json
from collections import OrderedDict

from django import forms
from django.utils.translation import gettext as __, gettext_lazy as _
from django.dispatch import receiver

from pretix.base.forms import SecretKeySettingsField
from pretix.base.signals import (
    logentry_display, register_global_settings, register_payment_providers,
    requiredaction_display
)

from pretix.presale.signals import (
    contact_form_fields, question_form_fields 
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

@receiver(contact_form_fields, dispatch_uid='mercadopago_contact_form_fields')
def register_contact_form_fields(sender, **kwargs):
    return OrderedDict([               
        ('invoicing_type_tax_id', forms.ChoiceField(
                label=_('Type of Identification'),
                help_text=_('All sales will have an associated invoice. ' + 
                            'VAT identification number of the individual or entity that ' + 
                            'is the recipient of the invoice. Must be legal on the ' + 
                            'country stated on the invoice information.' + 
                            'If you do not have one, you can user your passport.'),
                widget=forms.RadioSelect,
                choices=(
                    ('PASS', _('Passport')),
                    ('DNI', _('DNI Argentina')),
                    ('VAT', _('International VAT'))
                ),
                required=True
            )),
        ('invoicing_tax_id_pass', forms.CharField(
            widget=forms.TextInput(
                attrs={
                    'data-display-dependency': '#id_invoicing_type_tax_id_0',
                    'data-required-if': '#id_invoicing_type_tax_id_0'
                }
            ),
            label=_('Passport Number'),
            help_text=_('Write your passport number and letters.'),
            max_length=9,
            min_length=9,
            required=False
        )),
        ('invoicing_tax_id_dni', forms.CharField(
            widget=forms.TextInput(
                attrs={
                    'data-display-dependency': '#id_invoicing_type_tax_id_1',
                    'data-required-if': '#id_invoicing_type_tax_id_1'
                }
            ),
            label=_('DNI Argentina'),
            help_text=_('Only argentinian ID.'),
            max_length=20,
            min_length=4,
            required=False
        )),
        ('invoicing_tax_id_vat', forms.CharField(
            widget=forms.TextInput(
                attrs={
                    'data-display-dependency': '#id_invoicing_type_tax_id_2',
                    'data-required-if': '#id_invoicing_type_tax_id_2'
                }
            ),
            label=_('VAT Identification Number'),
            help_text=_('International VAT Identification Number.'),
            max_length=20,
            min_length=4,
            required=False
        ))
    ])


@receiver(question_form_fields, dispatch_uid='mercadopago_question_form_fields')
def register_question_form_fields(sender, **kwargs):
    return OrderedDict([
        ('invoicing_identifier', forms.CharField(
            label=_('Attendee ID'),
            help_text=_('Identifier type and number of the individual that ' + 
                        'is attending the event. ' + 
                        'For example: "PASSPORT 123456ABC" or "DNI 1234567X".'),
            required=False
        ))
    ])
