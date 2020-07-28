import json
import logging
import urllib.parse
from collections import OrderedDict
from decimal import Decimal

import mercadopago
from mercadopago import MP
            
from django import forms
from django.contrib import messages
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext as __, gettext_lazy as _
from i18nfield.strings import LazyI18nString

from pretix.base.decimal import round_decimal
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Quota
from pretix.base.payment import BasePaymentProvider, PaymentException
#from pretix.base.services.mail import SendMailException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri

logger = logging.getLogger('pretix.plugins.mercadopago')

SUPPORTED_CURRENCIES = ['ARS', 'BRL', 'CLP','MXN','COP','PEN','UYU']
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
    payment_form_fields = OrderedDict([
    ])

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
            return _('The MercadoPago sandbox is being used, you can test without actually sending money but you will need a '
                     'MercadoPago sandbox user to log in.')
        return None

    @property
    def settings_form_fields(self):
        if self.settings.connect_client_id and not self.settings.secret:
            # MercadoPago connect
            if self.settings.connect_user_id:
                fields = [
                    ('connect_user_id',
                     forms.CharField(
                         label=_('MercadoPago account'),
                         disabled=True
                     )),
                ]
            else:
                return {}
        else:
            fields = [
                ('client_id',
                 forms.CharField(
                     label=_('Client ID'),
                     max_length=71,
                     min_length=41,
                     help_text=_('{token}<a target="_blank" rel="noopener" href="{docs_url}">{text}</a>').format(
                         token=_('puede usar un token el lugar del client_id o '),
                         text=_('Click here for a tutorial on how to obtain the required keys'),
                         docs_url='https://www.mercadopago.com.ar/developers/es/guides/faqs/credentials'
                     )
                 )),
                ('secret',
                 forms.CharField(
                     label=_('Secret'),
                     max_length=71,
                     min_length=71,
                     required=False
                 )),
                ('endpoint',
                 forms.ChoiceField(
                     label=_('Endpoint'),
                     initial='live',
                     choices=(
                         ('live', 'Live'),
                         ('sandbox', 'Sandbox'),
                     ),
                 )),
            ]

        d = OrderedDict(
            fields + list(super().settings_form_fields.items())
        )

        d.move_to_end('_enabled', False)
        return d

    def get_connect_url(self, request):
        request.session['payment_mercadopago_oauth_event'] = request.event.pk

        self.init_api()
        return Tokeninfo.authorize_url({'scope': 'openid profile email'})

    def settings_content_render(self, request):
        settings_content = ""
        if self.settings.connect_client_id and not self.settings.secret:
            # Use MercadoPAgo connect
            if not self.settings.connect_user_id:
                settings_content = (
                    "<p>{}</p>"
                    "<a href='{}' class='btn btn-primary btn-lg'>{}</a>"
                ).format(
                    _('To accept payments via MercadoPagp, you will need an account at MercadoPago. By clicking on the '
                      'following button, you can either create a new MercadoPago account connect pretix to an existing '
                      'one.'),
                    self.get_connect_url(request),
                    _('Connect with {icon} MercadoPago').format(icon='<i class="fa fa-mercadopago"></i>')
                )
            else:
                settings_content = (
                    "<button formaction='{}' class='btn btn-danger'>{}</button>"
                ).format(
                    reverse('plugins:mercadopago:oauth.disconnect', kwargs={
                        'organizer': self.event.organizer.slug,
                        'event': self.event.slug,
                    }),
                    _('Disconnect from MercadoPago')
                )
        else:
            settings_content = "<div class='alert alert-info'>%s<br /><code>%s</code></div>" % (
                _('Please configure a MercadoPago Webhook to the following endpoint in order to automatically cancel orders '
                  'when payments are refunded externally.'),
                    'TODO link'
                #build_global_uri('plugins:mercadopago:webhook')
            )

        if self.event.currency not in SUPPORTED_CURRENCIES:
            settings_content += (
                '<br><br><div class="alert alert-warning">%s '

                '<a href="ihttps://www.mercadopago.com.ar/developers/es/reference/merchant_orders/resource/">%s</a>'
                '</div>'
            ) % (
                _("MercadoPago does not process payments in your event's currency."),
                _("Please check this MercadoPago page for a complete list of supported currencies.")
            )

        if self.event.currency in LOCAL_ONLY_CURRENCIES:
            settings_content += '<br><br><div class="alert alert-warning">%s''</div>' % (
                _("Your event's currency is supported by MercadoPago as a payment and balance currency for in-country "
                  "accounts only. This means, that the receiving as well as the sending MercadoPago account must have "
                  "been created in the same country and use the same currency. Out of country accounts will not be able "
                  "to send any payments.")
            )

        return settings_content

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return super().is_allowed(request, total) and self.event.currency in SUPPORTED_CURRENCIES

    def init_api(self):
        if self.settings.get('client_id') and not self.settings.get('secret'):
            mp = mercadopago.MP(self.settings.get('client_id'))
        else:
            mp = mercadopago.MP(self.settings.get('client_id'),self.settings.get('secret'))
        return mp


    def payment_is_valid_session(self, request):
        return (request.session.get('payment_mercadopago_id', '') != ''
                and request.session.get('payment_mercadopago_payer', '') != '')

    def payment_form_render(self, request) -> str:
        template = get_template('pretix_mercadopago/checkout_payment_form.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        return template.render(ctx)

    def checkout_prepare(self, request, cart):
        mp = self.init_api()
        kwargs = {}
        if request.resolver_match and 'cart_namespace' in request.resolver_match.kwargs:
            kwargs['cart_namespace'] = request.resolver_match.kwargs['cart_namespace']

        if request.event.settings.payment_mercadopago_connect_user_id:
            try:
                tokeninfo = Tokeninfo.create_with_refresh_token(request.event.settings.payment_mercadopago_connect_refresh_token)
            except BadRequest as ex:
                ex = json.loads(ex.content)
                messages.error(request, '{}: {} ({})'.format(
                    _('We had trouble communicating with PayPal/MP'),
                    ex['error_description'],
                    ex['correlation_id'])
                )
                return

            # Even if the token has been refreshed, calling userinfo() can fail. In this case we just don't
            # get the userinfo again and use the payment_paypal_connect_user_id that we already have on file
            try:
                userinfo = tokeninfo.userinfo()
                request.event.settings.payment_mercadopago_connect_user_id = userinfo.email
            except UnauthorizedAccess:
                pass

            payee = {
                "email": request.event.settings.payment_mercadopago_connect_user_id,
                # If PayPal ever offers a good way to get the MerchantID via the Identifity API,
                # we should use it instead of the merchant's eMail-address
                # "merchant_id": request.event.settings.payment_paypal_connect_user_id,
            }
        else:
            payee = {}


        preference = {
              "items": [
                {
                  "title": __('Order for %s') % str(request.event),
                  "quantity": 1,
                  "currency_id": request.event.currency,
                  "unit_price": float(cart['total'])
                }
              ],
              "back_urls": {"failure": "", "pending": "", "success": "https://delawen.com:444/"}

            }


#        client_id=self.settings.get('client_id')

#        mp = mercadopago.MP(client_id, client_secret)
#        mp = MP(client_id)

        # Get the payment reported by the IPN. Glossary of attributes response in https://developers.mercadopago.com
#        paymentInfo = mp.get_payment(kwargs["id"])
    
        # Show payment information
        #if paymentInfo["status"] == 200:
        #    return paymentInfo["response"]
        #else:
        #    return None


        preferenceResult = mp.create_preference(preference)
        request.session['payment_mercadopago_order'] = None
        request.session['payment_mercadopago_preferece_id'] = str(preferenceResult['response']['id'])
        return self._create_payment(request, preferenceResult)

    @property
    def abort_pending_allowed(self):
        return False

    def _create_payment(self, request, payment):
        try:
            if payment:
                if payment["status"] not in ( 200,201) : #ate not in ('created', 'approved', 'pending'):
                    messages.error(request, _('We had trouble communicating with MercadoPago' + str(payment["response"]["message"])))
                    logger.error('Invalid payment state: ' + str(payment["response"]))
                    return
                request.session['payment_mercadopago_id'] = str(payment["response"]["id"])
                link = payment["response"]["sandbox_init_point"]
#                     messages.error(request, _('Debug ' + str(link) )) 
#                     return str(link)
#                    if link.method == "REDIRECT" and link.rel == "approval_url":
#                     if request.session.get('iframe_session', False):
#                         signer = signing.Signer(salt='safe-redirect')
#                         return (
#                             build_absolute_uri(request.event, 'plugins:mercadopago:redirect') + '?url=' +
#                             urllib.parse.quote(signer.sign(link))
#                         )
#                     else:
                return link
            else:
                messages.error(request, _('We had trouble communicating with MercadoPago' + str(payment["response"])))
                logger.error('Error on creating payment: ' + str(payment["response"]))
        except Exception as e:
#            pass
            messages.error(request, _('We had trouble communicating with ' +
            'MercadoPago ' + str(e) + str(payment["response"])))
            logger.exception('Error on creating payment: ' + str(e))

    def checkout_confirm_render(self, request) -> str:
        """
        Returns the HTML that should be displayed when the user selected this provider
        on the 'confirm order' page.
        """
        template = get_template('pretixplugins/paypal/checkout_payment_confirm.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        return template.render(ctx)

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        if (request.session.get('payment_paypal_id', '') == '' or request.session.get('payment_paypal_payer', '') == ''):
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        self.init_api()
        pp_payment = paypalrestsdk.Payment.find(request.session.get('payment_paypal_id'))
        ReferencedPayPalObject.objects.get_or_create(order=payment.order, payment=payment, reference=pp_payment.id)
        if str(pp_payment.transactions[0].amount.total) != str(payment.amount) or pp_payment.transactions[0].amount.currency \
                != self.event.currency:
            logger.error('Value mismatch: Payment %s vs paypal trans %s' % (payment.id, str(pp_payment)))
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        return self._execute_payment(pp_payment, request, payment)

    def _execute_payment(self, payment, request, payment_obj):
        if payment.state == 'created':
            payment.replace([
                {
                    "op": "replace",
                    "path": "/transactions/0/item_list",
                    "value": {
                        "items": [
                            {
                                "name": __('Order {slug}-{code}').format(slug=self.event.slug.upper(),
                                                                         code=payment_obj.order.code),
                                "quantity": 1,
                                "price": self.format_price(payment_obj.amount),
                                "currency": payment_obj.order.event.currency
                            }
                        ]
                    }
                },
                {
                    "op": "replace",
                    "path": "/transactions/0/description",
                    "value": __('Order {order} for {event}').format(
                        event=request.event.name,
                        order=payment_obj.order.code
                    )
                }
            ])
            try:
                payment.execute({"payer_id": request.session.get('payment_paypal_payer')})
            except Exception as e:
                messages.error(request, _('We had trouble communicating with PayPal'))
                logger.exception('Error on creating payment: ' + str(e))

        for trans in payment.transactions:
            for rr in trans.related_resources:
                if hasattr(rr, 'sale') and rr.sale:
                    if rr.sale.state == 'pending':
                        messages.warning(request, _('PayPal has not yet approved the payment. We will inform you as '
                                                    'soon as the payment completed.'))
                        payment_obj.info = json.dumps(payment.to_dict())
                        payment_obj.state = OrderPayment.PAYMENT_STATE_PENDING
                        payment_obj.save()
                        return

        payment_obj.refresh_from_db()
        if payment.state == 'pending':
            messages.warning(request, _('PayPal has not yet approved the payment. We will inform you as soon as the '
                                        'payment completed.'))
            payment_obj.info = json.dumps(payment.to_dict())
            payment_obj.state = OrderPayment.PAYMENT_STATE_PENDING
            payment_obj.save()
            return

        if payment.state != 'approved':
            payment_obj.fail(info=payment.to_dict())
            logger.error('Invalid state: %s' % str(payment))
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        if payment_obj.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.warning('PayPal success event even though order is already marked as paid')
            return

        try:
            payment_obj.info = json.dumps(payment.to_dict())
            payment_obj.save(update_fields=['info'])
            payment_obj.confirm()
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e))

        except SendMailException:
            messages.warning(request, _('There was an error sending the confirmation mail.'))
        return None

    def payment_pending_render(self, request, payment) -> str:
        retry = True
        try:
            if payment.info and payment.info_data['state'] == 'pending':
                retry = False
        except KeyError:
            pass
        template = get_template('pretix_mercadopago/pending.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings,
               'retry': retry, 'order': payment.order}
        return template.render(ctx)

    def matching_id(self, payment: OrderPayment):
        sale_id = None
        for trans in payment.info_data.get('transactions', []):
            for res in trans.get('related_resources', []):
                if 'sale' in res and 'id' in res['sale']:
                    sale_id = res['sale']['id']
        return sale_id or payment.info_data.get('id', None)

    def api_payment_details(self, payment: OrderPayment):
        sale_id = None
        for trans in payment.info_data.get('transactions', []):
            for res in trans.get('related_resources', []):
                if 'sale' in res and 'id' in res['sale']:
                    sale_id = res['sale']['id']
        return {
            "payer_email": payment.info_data.get('payer', {}).get('payer_info', {}).get('email'),
            "payer_id": payment.info_data.get('payer', {}).get('payer_info', {}).get('payer_id'),
            "cart_id": payment.info_data.get('cart', None),
            "payment_id": payment.info_data.get('id', None),
            "sale_id": sale_id,
        }

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment):
        template = get_template('pretixplugins/paypal/control.html')
        sale_id = None
        for trans in payment.info_data.get('transactions', []):
            for res in trans.get('related_resources', []):
                if 'sale' in res and 'id' in res['sale']:
                    sale_id = res['sale']['id']
        ctx = {'request': request, 'event': self.event, 'settings': self.settings,
               'payment_info': payment.info_data, 'order': payment.order, 'sale_id': sale_id}
        return template.render(ctx)

    def payment_partial_refund_supported(self, payment: OrderPayment):
        return False

    def payment_refund_supported(self, payment: OrderPayment):
        return False

    def execute_refund(self, refund: OrderRefund):
        raise PaymentException(_('Refunding is not supported.'))

    def payment_prepare(self, request, payment_obj):
        self.init_api()

        if request.event.settings.payment_paypal_connect_user_id:
            try:
                tokeninfo = Tokeninfo.create_with_refresh_token(request.event.settings.payment_paypal_connect_refresh_token)
            except BadRequest as ex:
                ex = json.loads(ex.content)
                messages.error(request, '{}: {} ({})'.format(
                    _('We had trouble communicating with PayPal'),
                    ex['error_description'],
                    ex['correlation_id'])
                )
                return

            # Even if the token has been refreshed, calling userinfo() can fail. In this case we just don't
            # get the userinfo again and use the payment_paypal_connect_user_id that we already have on file
            try:
                userinfo = tokeninfo.userinfo()
                request.event.settings.payment_paypal_connect_user_id = userinfo.email
            except UnauthorizedAccess:
                pass

            payee = {
                "email": request.event.settings.payment_paypal_connect_user_id,
                # If PayPal ever offers a good way to get the MerchantID via the Identifity API,
                # we should use it instead of the merchant's eMail-address
                # "merchant_id": request.event.settings.payment_paypal_connect_user_id,
            }
        else:
            payee = {}

#Agrego mercadopago order hardcode para testear
        preference = {
              "items": [
                {
                  "title": __('Order {slug}-{code}').format(slug=self.event.slug.upper(),
                                                            code=payment_obj.order.code),
                  "quantity": 1,
                  "unit_price": 0.5, #self.format_price(payment_obj.amount),
                  "currency_id": payment_obj.order.event.currency
                }
              ]
            }
        client_id=self.settings.get('client_id')
        mp = mercadopago.MP(client_id)
#        mp.sandbox_mode('true')
        preferenceResult = mp.create_preference(preference)

#       payment = mercadopago.Payment({
#            'header': {'MercadoPago-Partner-Attribution-Id': 'manumanu'},
#            'intent': 'sale',
#            'payer': {
#                "payment_method": "mercadopago",
#            },
#            "redirect_urls": {
#                "return_url": build_absolute_uri(request.event, 'plugins:mercadopago:return'),
#                "cancel_url": build_absolute_uri(request.event, 'plugins:mercadopago:abort'),
#            },
#            "transactions": [
#                {
#                    "item_list": {
#                        "items": [
#                            {
#                                "name": __('Order {slug}-{code}').format(slug=self.event.slug.upper(),
#                                                                         code=payment_obj.order.code),
#                                "quantity": 1,
#                                "price": self.format_price(payment_obj.amount),
#                                "currency": payment_obj.order.event.currency
#                            }
#                        ]
#                    },
#                    "amount": {
#                        "currency": request.event.currency,
#                        "total": self.format_price(payment_obj.amount)
#                    },
#                    "description": __('Order {order} for {event}').format(
#                        event=request.event.name,
#                        order=payment_obj.order.code
#                    ),
#                    "payee": payee
#                }
#            ]
#        })
        request.session['payment_mercadopago_order'] = payment_obj.order.pk
        request.session['payment_mercadopago_payment'] = payment_obj.pk
        return self._create_payment(request, preferenceResult)

    def shred_payment_info(self, obj):
        if obj.info:
            d = json.loads(obj.info)
            new = {
                'id': d.get('id'),
                'payer': {
                    'payer_info': {
                        'email': '█'
                    }
                },
                'update_time': d.get('update_time'),
                'transactions': [
                    {
                        'amount': t.get('amount')
                    } for t in d.get('transactions', [])
                ],
                '_shredded': True
            }
            obj.info = json.dumps(new)
            obj.save(update_fields=['info'])

        for le in obj.order.all_logentries().filter(action_type="pretix.plugins.paypal.event").exclude(data=""):
            d = le.parsed_data
            if 'resource' in d:
                d['resource'] = {
                    'id': d['resource'].get('id'),
                    'sale_id': d['resource'].get('sale_id'),
                    'parent_payment': d['resource'].get('parent_payment'),
                }
            le.data = json.dumps(d)
            le.shredded = True
            le.save(update_fields=['data', 'shredded'])

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        if order.status == Order.STATUS_PAID:
            if payment.info_data.get('id', None):
                try:
                    return '{}\r\n{}: {}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('PayPal payment ID'),
                        payment.info_data['id'],
                        _('PayPal sale ID'),
                        payment.info_data['transactions'][0]['related_resources'][0]['sale']['id']
                    )
                except (KeyError, IndexError):
                    return '{}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('PayPal payment ID'),
                        payment.info_data['id']
                    )
            else:
                return super().render_invoice_text(order, payment)

        return self.settings.get('_invoice_text', as_type=LazyI18nString, default='')
