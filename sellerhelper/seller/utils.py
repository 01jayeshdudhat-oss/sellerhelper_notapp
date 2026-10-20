
import math
from .models import Shop, ShopStaff


def distance_km_between_points(lat1, lng1, lat2, lng2):
    try:
        lat1 = math.radians(float(lat1))
        lng1 = math.radians(float(lng1))
        lat2 = math.radians(float(lat2))
        lng2 = math.radians(float(lng2))
    except (TypeError, ValueError):
        return None

    delta_lat = lat2 - lat1
    delta_lng = lng2 - lng1
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return 6371 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))

def point_in_polygon(lat, lng, polygon):
    """
    Check whether a latitude/longitude point
    is inside a polygon.

    polygon format:

    [
        {"lat": 28.6001, "lng": 77.2001},
        {"lat": 28.6101, "lng": 77.2101},
        {"lat": 28.6201, "lng": 77.2001},
        ...
    ]
    """

    if not polygon or len(polygon) < 3:
        return False

    inside = False

    j = len(polygon) - 1

    for i in range(len(polygon)):

        try:
            lat_i = float(polygon[i]["lat"])
            lng_i = float(polygon[i]["lng"])

            lat_j = float(polygon[j]["lat"])
            lng_j = float(polygon[j]["lng"])

        except (KeyError, TypeError, ValueError):
            j = i
            continue

        intersects = (
            (lat_i > lat) != (lat_j > lat)
            and
            lng < (
                (lng_j - lng_i)
                * (lat - lat_i)
                / (lat_j - lat_i)
                + lng_i
            )
        )

        if intersects:
            inside = not inside

        j = i

    return inside

import json
import math

def normalize_pincodes(value):
    """
    Convert shop delivery_pincode into a clean set of strings.

    Supports:
    - ["400001", "400002"]
    - "400001,400002"
    - "400001, 400002"
    - ["400001"]
    - JSON string: '["400001", "400002"]'
    - single pincode: "400001"
    """

    if not value:
        return set()

    if isinstance(value, list):
        result = set()

        for item in value:
            if item is not None:
                pin = str(item).strip()
                if pin:
                    result.add(pin)

        return result

    if isinstance(value, dict):
        result = set()

        for item in value.values():
            if isinstance(item, list):
                for pin in item:
                    if pin is not None:
                        result.add(str(pin).strip())

            elif item is not None:
                result.add(str(item).strip())

        return result

    if isinstance(value, str):

        value = value.strip()

        if not value:
            return set()

        try:
            parsed = json.loads(value)

            if isinstance(parsed, list):
                return {
                    str(pin).strip()
                    for pin in parsed
                    if str(pin).strip()
                }

        except (json.JSONDecodeError, TypeError):
            pass

        value = value.replace("\n", ",")

        return {
            pin.strip()
            for pin in value.split(",")
            if pin.strip()
        }

    return {str(value).strip()}

def get_address_pincode(address):
    """
    Get client pincode from ClientDeliveryAddress.

    First checks address.pincode if available.
    Then checks location['pincode'].
    """

    if not address:
        return None

    pincode = getattr(address, "pincode", None)

    if pincode:
        return str(pincode).strip()

    location = address.location or {}

    if isinstance(location, dict):

        pincode = (
            location.get("pincode")
            or location.get("postal_code")
            or location.get("postcode")
            or location.get("zip")
        )

        if pincode:
            return str(pincode).strip()

    return None

def shop_delivers_to_pincode(shop, pincode):
    """
    Return True if the shop delivers to the given pincode.
    """

    if not pincode:
        return False

    client_pincode = str(pincode).strip()

    shop_pincodes = normalize_pincodes(
        shop.delivery_pincode
    )
    if not shop_pincodes:
        return True

    return client_pincode in shop_pincodes

def shop_delivers_to_location(
    shop,
    lat=None,
    lng=None,
    pincode=None,
    allow_scheduled_delivery=False,
):
    """
    Determine whether a shop can deliver to a client.

    Checks:

    1. Shop is open, unless scheduled delivery is enabled
    2. Pincode matches, if configured
    3. Location is inside one of the shop's delivery polygons
    """

    if not shop:
        return False

    if (
        not getattr(shop, "is_shop_open", True)
        and not allow_scheduled_delivery
    ):
        return False

    if pincode:

        delivery_pincode = shop.delivery_pincode

        if delivery_pincode:

            if isinstance(delivery_pincode, list):

                client_pincode = str(pincode).strip()

                allowed_pincodes = [
                    str(x).strip()
                    for x in delivery_pincode
                ]

                if client_pincode in allowed_pincodes:
                    return True

            elif isinstance(delivery_pincode, dict):

                client_pincode = str(pincode).strip()

                allowed_pincodes = [
                    str(x).strip()
                    for x in delivery_pincode.values()
                ]

                if client_pincode in allowed_pincodes:
                    return True

            elif isinstance(delivery_pincode, str):

                allowed_pincodes = [
                    x.strip()
                    for x in delivery_pincode.split(",")
                ]

                if str(pincode).strip() in allowed_pincodes:
                    return True

    if lat is None or lng is None:
        return False

    try:

        lat = float(lat)
        lng = float(lng)

    except (TypeError, ValueError):
        return False

    delivery_area = shop.delivery_area
    delivery_pincode = shop.delivery_pincode

    if not delivery_area and not delivery_pincode:
        return True

    if not delivery_area:
        return False

    polygons = delivery_area

    if isinstance(delivery_area, dict):

        polygons = delivery_area.get(
            "polygons",
            []
        )

    if not isinstance(polygons, list):
        return False

    for polygon in polygons:

        if isinstance(polygon, list):

            if point_in_polygon(
                lat,
                lng,
                polygon
            ):
                return True

        elif isinstance(polygon, dict):

            points = polygon.get(
                "points",
                []
            )

            if point_in_polygon(
                lat,
                lng,
                points
            ):
                return True

    return False

def location_inside_shop(shop, lat, lng):

    polygons = shop.delivery_area or []

    for polygon in polygons:

        if point_in_polygon(
            lat,
            lng,
            polygon
        ):
            return True

    return False

def set_staff_role_permissions(staff):

    if staff.role == "PACKER":

        staff.can_view_orders = True
        staff.can_confirm_orders = False
        staff.can_pack_orders = True
        staff.can_manage_products = False
        staff.can_manage_stock = True
        staff.can_cancel_orders = False
        staff.can_assign_staff = False
        staff.can_view_reports = False

    elif staff.role in ["ORDER_MANAGER", "ORDER"]:

        staff.can_view_orders = True
        staff.can_confirm_orders = True
        staff.can_pack_orders = True
        staff.can_manage_products = False
        staff.can_manage_stock = True
        staff.can_cancel_orders = True
        staff.can_assign_staff = False
        staff.can_view_reports = True

    elif staff.role in ["SHOP_ADMIN", "MANAGER"]:

        staff.can_view_orders = True
        staff.can_confirm_orders = True
        staff.can_pack_orders = True
        staff.can_manage_products = True
        staff.can_manage_stock = True
        staff.can_cancel_orders = True
        staff.can_assign_staff = True
        staff.can_manage_staff = True
        staff.can_view_reports = True

    elif staff.role in ["RIDER", "DELIVERY"]:

        staff.can_view_orders = True
        staff.can_confirm_orders = False
        staff.can_pack_orders = False
        staff.can_manage_products = False
        staff.can_manage_stock = False
        staff.can_cancel_orders = False
        staff.can_assign_staff = False
        staff.can_view_reports = False

    staff.save()

def user_can_access_shop(user, shop):

    if user.is_superuser:
        return True

    if shop.owner_id == user.id:
        return True

    return ShopStaff.objects.filter(
        shop=shop,
        user=user,
        is_active=True
    ).exists()

def user_can_manage_orders(user, shop):

    if shop.owner_id == user.id:
        return True

    staff = ShopStaff.objects.filter(
        shop=shop,
        user=user,
        is_active=True
    ).first()

    if not staff:
        return False

    return staff.can_view_orders

def user_can_change_status(user, shop):

    if shop.owner_id == user.id:
        return True

    staff = ShopStaff.objects.filter(
        shop=shop,
        user=user,
        is_active=True
    ).first()

    if not staff:
        return False

    return staff.can_change_order_status
