import logging
import json
from collections import OrderedDict
from decimal import Decimal

import mercadopago 

from django import forms
from django.contrib import messages
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext as __, gettext_lazy as _
from i18nfield.strings import LazyI18nString

from pretix.base.models import Event, OrderPayment, OrderRefund, Order
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri

logger = logging.getLogger('pretix.plugins.mercadopago')

SUPPORTED_CURRENCIES = ['ARS', 'BRL', 'CLP', 'MXN', 'COP', 'PEN', 'UYU']
# ARS Peso argentino.
# BRL Real brasilero.
# CLP Peso chileno.
# MXN Peso mexicano.
# COP Peso colombiano.
# PEN Sol peruano.
# UYU Peso uruguayo.

LOCAL_ONLY_CURRENCIES = ['ARS']


class Mercadopago(BasePaymentProvider):
    identifier = 'pretix_mercadopago'
    verbose_name = _('MercadoPago')

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'mercadopago', event)

    @property
    def test_mode_message(self):
        if self.settings.connect_client_id and not self.settings.secret:
            # in OAuth mode, sandbox mode needs to be set global
            is_sandbox = self.settings.connect_endpoint == 'sandbox'
        else:
            is_sandbox = self.settings.get('endpoint') == 'sandbox'
        if is_sandbox:
            return _('The MercadoPago sandbox is being used, you can test without '
                     'actually sending money but you will need a '
                     'MercadoPago sandbox user to log in.')
        return None

    ####################################################################
    #                           No Refunds                             #
    ####################################################################

    def payment_partial_refund_supported(self, payment: OrderPayment):
        return False

    def payment_refund_supported(self, payment: OrderPayment):
        return False

    def execute_refund(self, refund: OrderRefund):
        raise PaymentException(_('Refunding is not supported.'))

    ####################################################################
    #                       Plugin Settings                            #
    ####################################################################

    @property
    def settings_form_fields(self):
        fields = [
            ('client_id',
                forms.CharField(
                    label=_('Client ID'),
                    max_length=71,
                    min_length=10,
                    help_text=_('{token}<a target="_blank" rel="noopener" '
                                'href="{docs_url}">{text}</a>').format(
                        token=_('puede usar un token el lugar del client_id o '),
                        text=_('Click here for a tutorial on how to obtain the required keys'),
                        docs_url='https://www.mercadopago.com.ar/developers/es/guides/faqs/credentials'
                    )
                )),
            ('secret',
                forms.CharField(
                    label=_('Secret'),
                    max_length=71,
                    min_length=10,
                    required=False
                )),
            ('endpoint',
                forms.ChoiceField(
                    label=_('Endpoint'),
                    initial='live',
                    required=True,
                    choices=(
                        ('live', 'Live'),
                        ('sandbox', 'Sandbox'),
                    ),
                )),
            ('currency',
                forms.ChoiceField(
                    label=_('Currency'),
                    initial='ARS',
                    required=True,
                    choices=(
                        ('ARS', 'ARS'),
                        ('BRL', 'BRL'),
                        ('CLP', 'CLP'),
                        ('MXN', 'MXN'),
                        ('COP', 'COP'),
                        ('PEN', 'PEN'),
                        ('UYU', 'UYU'),
                    ),
                )),
            ('exchange_rate',
                forms.DecimalField(
                    label=_('Exchange Rate'),
                    required=True,
                    min_value=0,
                    decimal_places=2,
                    help_text=_('Exchange rate to apply to the event currency. Use "1" to not apply any exchange rate.')
                    )
                ),
        ]

        d = OrderedDict(
            fields + list(super().settings_form_fields.items())
        )

        return d

    def settings_content_render(self, request):
        settings_content = ""
        if not self.settings.get('client_id'):
            settings_content = (
                "<p>{}</p>"
                "<a href='{}' class='btn btn-primary btn-lg'>{}</a>"
            ).format(
                _('To accept payments via MercadoPagp, you will need an account at MercadoPago. '
                    'By clicking on the following button, you can either create a new MercadoPago '
                    'account connect pretix to an existing one.'),
                self.get_connect_url(request),
                _('Connect with {icon} MercadoPago').format(icon='<i class="fa fa-mercadopago"></i>')
            )
        else:
            settings_content = "<div class='alert alert-info'>%s<br /><code>%s</code></div>" % (
                _('Please configure a MercadoPago Webhook to the following endpoint in order '
                  'to automatically cancel orders when payments are refunded externally.'),
                build_absolute_uri(request.event, 'plugins:pretix_mercadopago:webhook')
            )

        if self.event.currency is not self.settings.get('currency'):
            settings_content += (
                '<br><br><div class="alert alert-warning">%s '
                '<a href="ihttps://www.mercadopago.com.ar/developers/es/reference/merchant_orders/resource/">%s</a>'
                '</div>'
            ) % (
                _("MercadoPago does not process payments in your event's currency."),
                _("Please make sure you are using the proper exchange rate.")
            )

        return settings_content

    def init_api(self) -> mercadopago.MP:
        if self.settings.get('client_id') and not self.settings.get('secret'):
            mp = mercadopago.MP(self.settings.get('client_id'))
        else:
            mp = mercadopago.MP(self.settings.get('client_id'), self.settings.get('secret'))
        return mp

    ####################################################################
    #                       MercadoPago Interaction                    #
    ####################################################################
    def payment_form_render(self, request) -> str:
        # When the user selects this provider
        # as their preferred payment method,
        # they will be shown the HTML you return from this method.
        return "You will be redirected to MercadoPago now."

    def payment_is_valid_session(self, request):
        # This is called at the time the user tries to place the order.
        # It should return True if the user’s session is valid and all data
        # your payment provider requires in future steps is present.
        return True

    def execute_payment(self, request: HttpRequest, payment_obj: OrderPayment):
        try:
            # After the user has confirmed their purchase,
            # this method will be called to complete the payment process.
            mp = self.init_api()
            order = payment_obj.order
            meta_info = json.loads(order.meta_info)
            form_data = meta_info.get('contact_form_data', {})

            address = {}
            company = ''
            name = ''
            if hasattr(Order, 'invoice_address'):
                address = {
                        "zip_code": order.invoice_address.zipcode,
                        "street_name":  order.invoice_address.street
                    }
                company = order.invoice_address.company
                name = str(order.invoice_address.name_parts)

            identification_type = form_data.get('invoicing_type_tax_id', '')

            if identification_type == 'PASS':
                identification_number = form_data.get('invoicing_tax_id_pass', '')
            elif identification_type == 'VAT':
                identification_number = form_data.get('invoicing_tax_id_vat', '')
            else:
                identification_number = form_data.get('invoicing_tax_id_dni', '')

            price = float(payment_obj.amount)
            if self.settings.get('currency') is not order.event.currency:
                price = price * float(self.settings.get('exchange_rate'))
            price = round(price, 2)

            order_url = build_absolute_uri(request.event, 
            'presale:event.order', 
                kwargs={
                    'order': order.code,
                    'secret': order.secret
                }
            )
            
            preference = {
                "items": [
                    {
                        "title": 
                                __('Order {slug}-{code}').format(
                                        slug=self.event.slug,
                                        code=order.code),
                        "quantity": 1,
                        "unit_price": price,
                        "currency_id": self.settings.get('currency')
                    }
                ],
                "auto_return": 'all', 
                "back_urls": {
                    "failure": order_url,
                    "pending":
                        build_absolute_uri(request.event,
                            'plugins:pretix_mercadopago:return'),
                    "success":
                        build_absolute_uri(request.event,
                            'plugins:pretix_mercadopago:return')
                },
                "notification_url": 
                        build_absolute_uri(request.event,
                            'plugins:pretix_mercadopago:return'),
                "statement_descriptor": __('Order {slug}-{code}').format(
                                        slug=self.event.slug,
                                        code=order.code),
                "external_reference": str(payment_obj.id),
      #          "additional_info": json.dumps(order.invoice_address)[:600],
                "payer": {
                    "name": name,
                    "surname": company,
                    "email": form_data.get('email', ''),
                    "identification": {
                        "type": identification_type,
                        "number": identification_number
                    },
                    "address": address
                },
                "payment_methods": {
                    "installments" : 1
                }
            }


            # Get the payment reported by the IPN.
            # Glossary of attributes response in https://developers.mercadopago.com
            #        paymentInfo = mp.get_payment(kwargs["id"])

            preferenceResult = mp.create_preference(preference)
            payment_obj.info = json.dumps(preferenceResult, indent=4)
            payment_obj.save()
            request.session['payment_mercadopago_preferece_id'] = str(preferenceResult['response']['id'])
            request.session['payment_mercadopago_collector_id'] = str(
                preferenceResult['response']['collector_id'])
            request.session['payment_mercadopago_order'] = order.pk
            request.session['payment_mercadopago_payment'] = payment_obj.pk

            try:
                if preferenceResult:
                    if preferenceResult["status"] not in (200, 201): # ate not in ('created', 'approved', 'pending'):
                        messages.error(request, _('We had trouble communicating with MercadoPago' + str(preferenceResult["response"]["message"])))
                        logger.error('Invalid payment state: ' + str(preferenceResult["response"]))
                        return
                    request.session['payment_mercadopago_id'] = str(preferenceResult["response"]["id"])
                    if (self.test_mode_message == None):
                        link = preferenceResult["response"]["init_point"]
                    else:
                        link = preferenceResult["response"]["sandbox_init_point"]
                    return link
                else:
                    messages.error(request, _('We had trouble communicating with MercadoPago' + str(preferenceResult["response"])))
                    logger.error('Error on creating payment: ' + str(preferenceResult["response"]))
            except Exception as e:
                messages.error(request, _('We had trouble communicating with ' +
                'MercadoPago ' + str(e) + str(preferenceResult["response"])))
                logger.exception('Error on creating payment: ' + str(e))

        except Exception as e:
            messages.error(request, _('We had trouble preparing the order for ' +
            'MercadoPago ' + str(e)))
            logger.exception('Error on creating payment: ' + str(e))

    def checkout_confirm_render(self, request) -> str:
        # Returns the HTML that should be displayed when the user selected this provider
        # on the 'confirm order' page.

        try:
            # TODO weird error that doesn't include templates on our path folder
            template = get_template('../../pretix_mercadopago/templates/pretix_mercadopago/checkout_payment_confirm.html')
        except Exception as e:
            template = get_template('pretixplugins/paypal/checkout_payment_confirm.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        return template.render(ctx)

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        if order.status == Order.STATUS_PAID:
            if payment.info_data.get('id', None):
                try:
                    return '{}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('Payment ID'),
                        payment.info_data['response']['id'],
                    )
                except (KeyError, IndexError):
                    return '{}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('Payment ID'),
                        payment.info_data['response']['id']
                    )
            else:
                return super().render_invoice_text(order, payment)

        return self.settings.get('_invoice_text', as_type=LazyI18nString, default='')
        

    def matching_id(self, payment: OrderPayment):
        # Will be called to get an ID for a matching this payment when comparing
        # pretix records with records of an external source.
        # This should return the main transaction ID for your API.
        return payment.info_data.get('external_reference', None)

    def api_payment_details(self, payment: OrderPayment):
        # Will be called to populate the details parameter 
        # of the payment in the REST API.
        res = {
            "payment_info": payment.info
        }

        try:
            res = json.loads(payment.info)
        except Exception as e:
            logger.exception('Could not parse json payment.info')

        return res

    ####################################################################
    #                          Utility functions                       #
    ####################################################################
    def get_connect_url(self, request):
        request.session['payment_mercadopago_oauth_event'] = request.event.pk

        self.init_api()
        return Tokeninfo.authorize_url({'scope': 'openid profile email'})
