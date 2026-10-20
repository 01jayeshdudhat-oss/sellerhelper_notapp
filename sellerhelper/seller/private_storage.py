from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateOrderAudioStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(
            location=settings.BASE_DIR / "private_order_audio",
            base_url=None,
        )
