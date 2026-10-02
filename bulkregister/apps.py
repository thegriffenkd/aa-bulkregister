from django.apps import AppConfig

from . import __version__


class BulkRegisterConfig(AppConfig):
    name = "bulkregister"
    label = "bulkregister"
    verbose_name = f"Bulk Register v{__version__}"
    default_auto_field = "django.db.models.AutoField"
