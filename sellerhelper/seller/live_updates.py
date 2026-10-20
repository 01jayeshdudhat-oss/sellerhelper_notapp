import logging
from functools import partial

from asgiref.sync import async_to_sync
from channels.exceptions import ChannelFull
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import transaction
from redis.exceptions import RedisError

from .models import SharedRiderGroupShop, ShopOrder, ShopStaff


logger = logging.getLogger(__name__)


def _order_update_user_ids(shop_order):
    user_ids = set()
    if shop_order.shop.owner_id:
        user_ids.add(shop_order.shop.owner_id)

    user_ids.update(
        shop_order.shop.staff_members.filter(is_active=True)
        .values_list("user_id", flat=True)
    )
    if shop_order.assigned_packer_id:
        user_ids.add(shop_order.assigned_packer.user_id)
    if shop_order.assigned_rider_id:
        user_ids.add(shop_order.assigned_rider.user_id)

    rider_shop_ids = {shop_order.shop_id}
    approved_group_ids = SharedRiderGroupShop.objects.filter(
        shop_id=shop_order.shop_id,
        status=SharedRiderGroupShop.STATUS_APPROVED,
    ).values_list("group_id", flat=True)
    rider_shop_ids.update(
        SharedRiderGroupShop.objects.filter(
            group_id__in=approved_group_ids,
            status=SharedRiderGroupShop.STATUS_APPROVED,
        ).values_list("shop_id", flat=True)
    )
    riders = ShopStaff.objects.filter(is_active=True, role="RIDER")
    if not shop_order.shop.share_riders_with_nearby_shops:
        riders = riders.filter(shop_id__in=rider_shop_ids)
    user_ids.update(riders.values_list("user_id", flat=True))

    order_username = shop_order.order.user
    if order_username:
        customer_id = (
            get_user_model().objects.filter(username=order_username)
            .values_list("pk", flat=True)
            .first()
        )
        if customer_id:
            user_ids.add(customer_id)
    return user_ids


def _send_user_event(user_id, event_type, data=None):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.error("Live update event %s has no configured channel layer.", event_type)
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"live_user_{user_id}",
            {"type": "live.event", "event_type": event_type, "data": data or {}},
        )
    except (ChannelFull, OSError, TimeoutError, RedisError):
        logger.exception(
            "Could not deliver live update event %s to user %s.",
            event_type,
            user_id,
        )


def queue_order_update(shop_order):
    user_ids = _order_update_user_ids(shop_order)
    for user_id in user_ids:
        transaction.on_commit(partial(_send_user_event, user_id, "orders.updated"))


def queue_rider_location_update(shop_order):
    user_ids = set()
    if shop_order.shop.owner_id:
        user_ids.add(shop_order.shop.owner_id)
    user_ids.update(
        shop_order.shop.staff_members.filter(
            is_active=True,
            role="MANAGER",
        ).values_list("user_id", flat=True)
    )
    location = shop_order.rider_last_location
    if not isinstance(location, dict):
        return
    event_data = {
        "shop_id": shop_order.shop_id,
        "rider": {
            "rider_id": shop_order.assigned_rider_id,
            "rider_name": (
                shop_order.assigned_rider.user.get_full_name()
                or shop_order.assigned_rider.user.username
                if shop_order.assigned_rider_id
                else ""
            ),
            "order_id": shop_order.order.order_id,
            "location": location,
            "updated_at": shop_order.updated_at.isoformat(),
        },
    }
    for user_id in user_ids:
        transaction.on_commit(
            partial(
                _send_user_event,
                user_id,
                "rider.location.updated",
                event_data,
            )
        )


def queue_notification_update(notification):
    user_id = notification.user_id
    transaction.on_commit(partial(_send_user_event, user_id, "notifications.created"))


def order_update_is_relevant(update_fields):
    if update_fields is None:
        return True
    return bool(
        {
            "status",
            "assigned_packer",
            "assigned_packer_id",
            "assigned_rider",
            "assigned_rider_id",
            "total_amount",
            "product_count",
            "delivery_fee",
            "delivery_completed_at",
            "cancellation_source",
        }.intersection(update_fields)
    )


def order_update_recipients(shop_order):
    return _order_update_user_ids(shop_order)
