from .models import (
    ClientProfile,
    Notification,
    PublicRiderApplication,
    Shop,
    ShopOrder,
    ShopStaff,
    SiteBrandingSettings,
    SiteThemeSettings,
)
from .public_riders import public_rider_service_enabled
from django.db.models import Q
from django.utils import timezone


def _cart_count(request, addresses=()):
    requested_address_id = request.session.get("cart_delivery_address_id")
    address = next(
        (
            item for item in addresses
            if str(item.pk) == str(requested_address_id or "")
        ),
        None,
    )
    if address is None:
        address = next(
            (item for item in addresses if item.is_default),
            addresses[0] if addresses else None,
        )
    address_key = str(address.pk) if address else "unassigned"

    carts = request.session.get("carts_by_delivery_address")
    if not isinstance(carts, dict):
        carts = {}
    cart = request.session.get("cart", {})
    if not isinstance(cart, dict):
        cart = {}
    cart_is_current = (
        address_key in carts
        and request.session.get("cart_delivery_address_id") == address_key
        and cart != carts[address_key]
    )
    if not cart_is_current:
        if address_key in carts:
            cart = carts[address_key]
        elif "unassigned" in carts and address_key != "unassigned":
            cart = carts["unassigned"]
        elif carts:
            cart = {}
    if not isinstance(cart, dict):
        return 0

    count = 0
    for quantity in cart.values():
        try:
            count += max(0, int(quantity))
        except (TypeError, ValueError):
            continue
    return count


def unread_notifications_count(request):
    site_branding = SiteBrandingSettings.objects.first()
    site_theme = SiteThemeSettings.objects.first()
    valid_sections = ("header", "content", "footer")
    desktop_order = (
        site_theme.desktop_layout_order.split(",")
        if site_theme else list(valid_sections)
    )
    mobile_order = (
        site_theme.mobile_layout_order.split(",")
        if site_theme else list(valid_sections)
    )
    if set(desktop_order) != set(valid_sections) or len(desktop_order) != 3:
        desktop_order = list(valid_sections)
    if set(mobile_order) != set(valid_sections) or len(mobile_order) != 3:
        mobile_order = list(valid_sections)
    theme_orders = {
        "desktop": {
            section: desktop_order.index(section) + 1
            for section in valid_sections
        },
        "mobile": {
            section: mobile_order.index(section) + 1
            for section in valid_sections
        },
    }
    homepage_order = (
        site_theme.homepage_section_order.split(",")
        if site_theme else ["hero", "features"]
    )
    if set(homepage_order) != {"hero", "features"} or len(homepage_order) != 2:
        homepage_order = ["hero", "features"]
    homepage_theme_order = {
        section: homepage_order.index(section) + 1
        for section in ("hero", "features")
    }
    active_portal = request.session.get("active_portal", "CUSTOMER")
    app_zoom_fields = {
        "CUSTOMER": "mobile_app_customer_no_zoom_enabled",
        "B2B_CUSTOMER": "mobile_app_b2b_customer_no_zoom_enabled",
        "SHOP_OWNER": "mobile_app_shop_owner_no_zoom_enabled",
        "SHOP_STAFF": "mobile_app_shop_staff_no_zoom_enabled",
        "ADMIN": "mobile_app_admin_no_zoom_enabled",
    }
    app_zoom_field = app_zoom_fields.get(active_portal)
    mobile_app_customer_no_zoom = bool(
        site_theme
        and app_zoom_field
        and getattr(site_theme, app_zoom_field)
    )
    mobile_app_content_zoom_enabled = (
        site_theme is None or site_theme.mobile_app_content_zoom_enabled
    )
    app_content_zoom_fields = {
        "CUSTOMER": "mobile_app_customer_content_zoom_enabled",
        "B2B_CUSTOMER": "mobile_app_b2b_customer_content_zoom_enabled",
        "SHOP_OWNER": "mobile_app_shop_owner_content_zoom_enabled",
        "SHOP_STAFF": "mobile_app_shop_staff_content_zoom_enabled",
    }
    app_content_zoom_field = app_content_zoom_fields.get(active_portal)
    mobile_app_portal_content_zoom_enabled = (
        getattr(site_theme, app_content_zoom_field)
        if site_theme and app_content_zoom_field
        else True
    )
    mobile_app_desktop_view_enabled = bool(
        site_theme and site_theme.mobile_app_desktop_view_enabled
    )
    if not request.user.is_authenticated:
        return {
            "unread_notifications_count": 0,
            "cart_count": _cart_count(request),
            "market_delivery_address": None,
            "market_delivery_addresses": [],
            "is_shop_owner_portal": False,
            "is_staff_portal": False,
            "can_manage_app_zoom": False,
            "show_app_zoom_controls": False,
            "is_public_rider_portal": False,
            "public_rider_online": None,
            "public_rider_offer_count": 0,
            "is_customer_portal": True,
            "staff_permissions": None,
            "active_portal": active_portal,
            "mobile_app_customer_no_zoom": mobile_app_customer_no_zoom,
            "mobile_app_content_zoom_enabled": mobile_app_content_zoom_enabled,
            "mobile_app_portal_content_zoom_enabled": mobile_app_portal_content_zoom_enabled,
            "mobile_app_desktop_view_enabled": mobile_app_desktop_view_enabled,
            "site_branding": site_branding,
            "site_theme": site_theme,
            "site_theme_orders": theme_orders,
            "site_homepage_theme_order": homepage_theme_order,
        }

    count = Notification.objects.filter(user=request.user, is_read=False).count()
    profile = ClientProfile.objects.filter(user=request.user).first()
    public_rider_application = (
        PublicRiderApplication.objects.filter(
            user=request.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).only("is_online").first()
        if active_portal == "RIDER"
        else None
    )
    is_public_rider_portal = public_rider_application is not None
    public_rider_offer_count = 0
    if (
        is_public_rider_portal
        and public_rider_application.is_online
        and public_rider_service_enabled()
    ):
        public_rider_offer_count = (
            ShopOrder.objects.filter(
                public_rider_offer_user=request.user,
                public_rider_offer_expires_at__gt=timezone.now(),
                status="READY",
                assigned_rider__isnull=True,
                delivery_completed_at__isnull=True,
                shop__public_rider_network_enabled=True,
            )
            .filter(
                Q(public_rider_network_requested=True)
                | Q(shop__rider_delivery_preference="PUBLIC"),
            )
            .count()
        )
    staff_membership = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
    ).select_related("shop").first()
    owns_shops = Shop.objects.filter(owner=request.user).exists()
    has_shop_staff_access = staff_membership is not None
    is_staff_portal = active_portal == "SHOP_STAFF" and staff_membership is not None
    is_owner_portal = active_portal == "SHOP_OWNER" and owns_shops
    can_manage_app_zoom = has_shop_staff_access or owns_shops
    show_app_zoom_controls = is_public_rider_portal or (
        can_manage_app_zoom
        and active_portal not in {"CUSTOMER", "B2B_CUSTOMER"}
    )
    if can_manage_app_zoom and active_portal not in {"CUSTOMER", "B2B_CUSTOMER"}:
        mobile_app_customer_no_zoom = True
        mobile_app_content_zoom_enabled = True
        mobile_app_portal_content_zoom_enabled = True
    is_customer_portal = (
        not is_staff_portal
        and not is_owner_portal
        and not is_public_rider_portal
        and (
            active_portal == "CUSTOMER" or profile is not None
        )
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
    cart_count = _cart_count(request, addresses)
    return {
        "unread_notifications_count": count,
        "cart_count": cart_count,
        "market_delivery_address": address,
        "market_delivery_addresses": addresses,
        "is_shop_owner_portal": is_owner_portal,
        "can_manage_app_zoom": can_manage_app_zoom,
        "show_app_zoom_controls": show_app_zoom_controls,
        "is_staff_portal": is_staff_portal,
        "is_public_rider_portal": is_public_rider_portal,
        "public_rider_offer_count": public_rider_offer_count,
        "public_rider_online": (
            public_rider_application.is_online
            if public_rider_application
            else None
        ),
        "is_customer_portal": is_customer_portal,
        "staff_permissions": staff_membership,
        "active_portal": active_portal,
        "mobile_app_customer_no_zoom": mobile_app_customer_no_zoom,
        "mobile_app_content_zoom_enabled": mobile_app_content_zoom_enabled,
        "mobile_app_portal_content_zoom_enabled": mobile_app_portal_content_zoom_enabled,
        "mobile_app_desktop_view_enabled": mobile_app_desktop_view_enabled,
        "site_branding": site_branding,
        "site_theme": site_theme,
        "site_theme_orders": theme_orders,
        "site_homepage_theme_order": homepage_theme_order,
    }
