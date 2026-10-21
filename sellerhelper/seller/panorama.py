import cv2
import numpy as np
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

MAX_SOURCE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PANORAMA_BYTES = 5 * 1024 * 1024


def stitch_shop_panorama(source_images):
    if len(source_images) not in {4, 6, 8}:
        raise ValidationError("Capture 4, 6, or 8 overlapping images for a 360° panorama.")

    total_source_bytes = 0
    decoded_images = []
    for index, uploaded_image in enumerate(source_images, start=1):
        if uploaded_image.size > MAX_SOURCE_IMAGE_BYTES:
            raise ValidationError(f"Panorama image {index} must be 5 MB or smaller.")
        total_source_bytes += uploaded_image.size
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValidationError("All panorama images together must be 20 MB or smaller.")

        uploaded_image.seek(0)
        image_bytes = b"".join(uploaded_image.chunks())
        uploaded_image.seek(0)
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if image is None:
            raise ValidationError(f"Panorama image {index} could not be decoded.")
        decoded_images.append(image)

    try:
        stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
        status, panorama = stitcher.stitch(decoded_images)
    except cv2.error as error:
        raise ValidationError(
            "These photos could not be stitched. Capture them in order, "
            "from one fixed position, with at least 30% overlap."
        ) from error

    if status != cv2.Stitcher_OK or panorama is None:
        raise ValidationError(
            "These photos could not be stitched into a panorama. Keep the camera "
            "at one position, rotate around it, and overlap neighboring photos "
            "by at least 30%."
        )

    height, width = panorama.shape[:2]
    if width > 4096:
        resized_height = max(1, round(height * 4096 / width))
        panorama = cv2.resize(
            panorama,
            (4096, resized_height),
            interpolation=cv2.INTER_AREA,
        )

    for quality in (90, 84, 78, 72):
        success, encoded = cv2.imencode(
            ".jpg",
            panorama,
            [cv2.IMWRITE_JPEG_QUALITY, quality],
        )
        if success and encoded.nbytes <= MAX_PANORAMA_BYTES:
            return ContentFile(encoded.tobytes(), name="shop-360-panorama.jpg")

    raise ValidationError(
        "The stitched panorama is larger than 5 MB. Capture lower-resolution photos and try again."
    )
