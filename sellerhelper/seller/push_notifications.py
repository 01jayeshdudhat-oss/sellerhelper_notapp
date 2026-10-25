import logging

import requests
from django.db import transaction

from .background_tasks import submit_background_task
from .models import Notification, NotificationDeviceToken

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
ANDROID_NOTIFICATION_CHANNEL = "sellerhelper-alerts-v2"


def send_expo_push_notifications(notification_ids):
    notifications = list(
        Notification.objects.filter(pk__in=notification_ids).select_related("user")
    )
    if not notifications:
        logger.warning("Push delivery skipped because its notifications no longer exist.")
        return

    devices_by_user = {}
    devices = NotificationDeviceToken.objects.filter(
        user_id__in={notification.user_id for notification in notifications},
        is_active=True,
    ).only("id", "user_id", "token")
    for device in devices:
        devices_by_user.setdefault(device.user_id, []).append(device)

    messages = []
    for notification in notifications:
        for device in devices_by_user.get(notification.user_id, []):
            messages.append((notification, device, {
                "to": device.token,
                "title": notification.title,
                "body": notification.message,
                "sound": "default",
                "priority": "high",
                "channelId": ANDROID_NOTIFICATION_CHANNEL,
                "data": {
                    "url": notification.related_url,
                    "notificationId": notification.pk,
                },
            }))

    for offset in range(0, len(messages), 100):
        chunk = messages[offset:offset + 100]
        try:
            response = requests.post(
                EXPO_PUSH_URL,
                json=[message for _notification, _device, message in chunk],
                timeout=(3, 10),
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError):
            logger.exception(
                "Expo push delivery failed for notification %s.",
                [notification.pk for notification, _device, _message in chunk],
            )
            continue

        tickets = result.get("data") if isinstance(result, dict) else None
        if not isinstance(tickets, list) or len(tickets) != len(chunk):
            logger.error(
                "Expo returned an unexpected ticket response for notification %s.",
                [notification.pk for notification, _device, _message in chunk],
            )
            continue

        for (_notification, device, _message), ticket in zip(chunk, tickets):
            if not isinstance(ticket, dict) or ticket.get("status") != "error":
                continue
            details = ticket.get("details")
            if isinstance(details, dict) and details.get("error") == "DeviceNotRegistered":
                NotificationDeviceToken.objects.filter(pk=device.pk).update(
                    is_active=False,
                )
            logger.warning(
                "Expo rejected a push for notification %s: %s",
                _notification.pk,
                ticket.get("message", "unknown error"),
            )


def queue_expo_push_notifications(notification_ids):
    notification_ids = tuple(set(notification_ids))
    if not notification_ids:
        return
    transaction.on_commit(
        lambda: submit_background_task(
            send_expo_push_notifications,
            notification_ids,
            task_name="Expo push delivery",
        ),
    )
