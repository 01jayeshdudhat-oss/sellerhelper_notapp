from django.db.models import Q

from .models import (
    DeliverySharingSettings,
    PublicRiderApplication,
    Shop,
    ShopStaff,
    ShopOrder,
)


def public_rider_service_enabled():
    enabled = DeliverySharingSettings.objects.filter(pk=1).values_list(
        "public_rider_service_enabled",
        flat=True,
    ).first()
    return enabled is not False


def sync_public_rider_shop_memberships(application):
    public_memberships = ShopStaff.objects.filter(
        user=application.user,
        is_public_rider=True,
    )
    if application.status != PublicRiderApplication.STATUS_APPROVED:
        public_memberships.update(is_active=False)
        return

    if not public_rider_service_enabled():
        active_shop_ids = ShopOrder.objects.filter(
            Q(assigned_rider__user=application.user)
            | Q(
                rider_accepted_at__isnull=False,
                order__assigned_rider=application.user.username,
            ),
            status__in=[
                "READY",
                "RIDER_ASSIGNED",
                "PICKED_UP",
                "OUT_FOR_DELIVERY",
            ],
            delivery_completed_at__isnull=True,
        ).values_list("shop_id", flat=True).distinct()
        public_memberships.filter(shop_id__in=active_shop_ids).update(
            is_active=True,
        )
        public_memberships.exclude(shop_id__in=active_shop_ids).update(
            is_active=False,
        )
        return

    network_shops = Shop.objects.filter(
        public_rider_network_enabled=True,
    ).only("id")
    for shop in network_shops.iterator():
        public_membership = public_memberships.filter(shop=shop).first()
        if public_membership:
            public_membership.is_active = True
            public_membership.mobile_no = application.phone
            public_membership.save(update_fields=["is_active", "mobile_no", "updated_at"])
            continue
        membership = ShopStaff.objects.filter(
            user=application.user,
            shop=shop,
        ).first()
        if membership:
            continue
        ShopStaff.objects.create(
            user=application.user,
            shop=shop,
            role="RIDER",
            mobile_no=application.phone,
            is_active=True,
            is_public_rider=True,
        )
