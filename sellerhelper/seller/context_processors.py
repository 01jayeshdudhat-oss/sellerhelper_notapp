from .models import (
    ClientProfile,
    Notification,
    Shop,
    ShopStaff,
    SiteBrandingSettings,
)


def unread_notifications_count(request):
    site_branding = SiteBrandingSettings.objects.first()
    if not request.user.is_authenticated:
        return {
            "unread_notifications_count": 0,
            "market_delivery_address": None,
            "market_delivery_addresses": [],
            "is_shop_owner_portal": False,
            "is_staff_portal": False,
            "is_customer_portal": True,
            "staff_permissions": None,
            "active_portal": "CUSTOMER",
            "site_branding": site_branding,
        }

    count = Notification.objects.filter(user=request.user, is_read=False).count()
    profile = ClientProfile.objects.filter(user=request.user).first()
    active_portal = request.session.get("active_portal", "CUSTOMER")
    staff_membership = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
    ).select_related("shop").first()
    owns_shops = Shop.objects.filter(owner=request.user).exists()
    is_staff_portal = active_portal == "SHOP_STAFF" and staff_membership is not None
    is_owner_portal = active_portal == "SHOP_OWNER" and owns_shops
    is_customer_portal = not is_staff_portal and not is_owner_portal and (
        active_portal == "CUSTOMER" or profile is not None
    )
    address = None
    addresses = []
    if profile:
        addresses = list(profile.delivery_addresses.filter(
            is_active=True,
        ).order_by("-is_default", "-id"))
        address = next(
            (item for item in addresses if item.is_default),
            addresses[0] if addresses else None,
        )
    return {
        "unread_notifications_count": count,
        "market_delivery_address": address,
        "market_delivery_addresses": addresses,
        "is_shop_owner_portal": is_owner_portal,
        "is_staff_portal": is_staff_portal,
        "is_customer_portal": is_customer_portal,
        "staff_permissions": staff_membership,
        "active_portal": active_portal,
        "site_branding": site_branding,
    }
