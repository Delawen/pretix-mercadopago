from django.utils.translation import gettext_lazy

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")

__version__ = '1.0.0'


class PluginMeli(PluginConfig):
    name = 'pretix_mercadopago'
    verbose_name = 'Mercado Pago Plugin'

    class PretixPluginMeta:
        name = gettext_lazy('Mercado Pago Plugin')
        author = 'FOSS4G Teams'
        description = gettext_lazy('Plugin para MercadoPago como medio de pago para las entradas.')
        visible = True
        version = __version__
        category = 'PAYMENT'
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA


default_app_config = 'pretix_mercadopago.PluginMeli'
