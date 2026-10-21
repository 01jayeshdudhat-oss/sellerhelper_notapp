from .models import (
    DeliverySharingSettings,
    PublicRiderApplication,
    Shop,
    ShopStaff,
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
    if (
        application.status != PublicRiderApplication.STATUS_APPROVED
        or not public_rider_service_enabled()
    ):
        public_memberships.update(is_active=False)
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
