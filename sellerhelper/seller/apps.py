from django.apps import AppConfig


class SellerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'seller'
    verbose_name = 'Seller'

    def ready(self):
        from . import signals  # noqa: F401
