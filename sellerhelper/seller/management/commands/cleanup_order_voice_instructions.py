from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from seller.models import OrderVoiceInstruction


class Command(BaseCommand):
    help = "Delete expired order voice recordings and abandoned checkout recordings."

    def handle(self, *args, **options):
        now = timezone.now()
        expired_recordings = (
            OrderVoiceInstruction.objects
            .filter(
                shop_order__delivery_completed_at__lte=now - timedelta(hours=1),
            )
            .exclude(audio_file="")
        )
        deleted_recordings = 0
        for instruction in expired_recordings.iterator():
            instruction.audio_file.delete(save=False)
            instruction.audio_content_type = ""
            instruction.save(update_fields=["audio_file", "audio_content_type"])
            deleted_recordings += 1

        expired_drafts = OrderVoiceInstruction.objects.filter(
            shop_order__isnull=True,
            created_at__lte=now - timedelta(hours=24),
        )
        deleted_drafts = 0
        for instruction in expired_drafts.iterator():
            if instruction.audio_file:
                instruction.audio_file.delete(save=False)
            instruction.delete()
            deleted_drafts += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_recordings} expired recording(s) and "
                f"{deleted_drafts} abandoned draft(s)."
            )
        )
