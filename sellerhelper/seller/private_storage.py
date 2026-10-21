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


@deconstructible
class PrivateRiderDocumentStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(
            location=settings.BASE_DIR / "private_rider_documents",
            base_url=None,
        )

    def url(self, name):
        raise ValueError("Public URLs are disabled for private rider documents.")
