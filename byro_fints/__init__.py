from django.apps import AppConfig
from django.utils.translation import ugettext_lazy


class PluginApp(AppConfig):
    name = 'byro_fints'
    verbose_name = 'Byro FinTS/HBCI 3.0 plugin'

    class ByroPluginMeta:
        name = ugettext_lazy('Byro FinTS/HBCI 3.0 plugin')
        author = 'Henryk Plötz'
        description = ugettext_lazy('Byro plugin to retrieve bank statements via FinTS 3.0 (formerly known as HBCI)')
        visible = True
        version = '0.0.0'

    def ready(self):
        from . import signals  # NOQA


default_app_config = 'byro_fints.PluginApp'
