from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from .live_updates import (
    order_update_is_relevant,
    queue_notification_update,
    queue_order_update,
    queue_rider_location_update,
)
from .models import Notification, OrderVoiceInstruction, ShopOrder, UserActivity
from .push_notifications import queue_expo_push_notifications


@receiver(user_logged_in, dispatch_uid="seller.record_successful_login")
def record_successful_login(sender, request, user, **kwargs):
    UserActivity.objects.create(
        user=user,
        event_type="AUTH_LOGIN",
        path=request.path if request else "",
    )


@receiver(post_save, sender=Notification)
def push_new_notification(sender, instance, created, **kwargs):
    if created:
        queue_expo_push_notifications([instance.pk])
        queue_notification_update(instance)


@receiver(post_save, sender=ShopOrder)
def publish_shop_order_updates(sender, instance, created, update_fields=None, **kwargs):
    if created:
        queue_order_update(instance)
        return
    if order_update_is_relevant(update_fields):
        queue_order_update(instance)
    if update_fields and "rider_last_location" in update_fields:
        queue_rider_location_update(instance)


@receiver(pre_delete, sender=OrderVoiceInstruction)
def delete_order_voice_instruction_audio(sender, instance, **kwargs):
    if instance.audio_file:
        instance.audio_file.delete(save=False)
