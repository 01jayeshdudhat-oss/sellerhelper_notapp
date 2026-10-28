import math
from datetime import datetime, timedelta
from secrets import randbelow

from django.db import transaction
from django.db.models import Count, Max
from django.urls import reverse
from django.utils import timezone

from .models import (
    DeliverySharingSettings,
    Notification,
    PublicRiderApplication,
    ShopOrder,
    ShopStaff,
)
from .public_riders import public_rider_service_enabled
PUBLIC_RIDER_OFFER_SECONDS = 60
PUBLIC_RIDER_OFFER_BATCH_SIZE = 5
PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS = 12
PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS = PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS
RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS = 10
PUBLIC_RIDER_LOCATION_MAX_AGE_SECONDS = 600


def _public_rider_offer_settings():
    configured = DeliverySharingSettings.objects.filter(pk=1).values_list(
        "public_rider_offer_timeout_seconds",
        "public_rider_offer_riders_per_round",
    ).first()
    if configured is None:
        return PUBLIC_RIDER_OFFER_SECONDS, PUBLIC_RIDER_OFFER_BATCH_SIZE
    timeout_seconds, riders_per_round = configured
    return (
        timeout_seconds or PUBLIC_RIDER_OFFER_SECONDS,
        riders_per_round or PUBLIC_RIDER_OFFER_BATCH_SIZE,
    )


def public_rider_max_active_offers():
    configured = DeliverySharingSettings.objects.filter(pk=1).values_list(
        "public_rider_max_active_offers",
        flat=True,
    ).first()
    return configured or 2


def _next_public_rider_offer_timeout(maximum_seconds):
    maximum_seconds = max(
        PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS,
        int(maximum_seconds),
    )
    return (
        PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS
        + randbelow(
            maximum_seconds - PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS + 1,
        )
    )


def uses_public_rider_network(shop_order):
    return bool(
        not shop_order.public_rider_fallback_to_staff
        and public_rider_service_enabled()
        and shop_order.shop.public_rider_network_enabled
        and (
            shop_order.public_rider_network_requested
            or (
                shop_order.shop.rider_delivery_preference == "PUBLIC"
                and not shop_order.assigned_rider_id
            )
        )
    )


def _coordinates(location):
    if not isinstance(location, dict):
        return None

    latitude = location.get("lat")
    longitude = location.get("lng")
    coordinates = location.get("coordinates")
    if (
        (latitude is None or longitude is None)
        and isinstance(coordinates, (list, tuple))
        and len(coordinates) >= 2
    ):
        latitude, longitude = coordinates[:2]
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return latitude, longitude


def _point(value):
    if isinstance(value, dict):
        return _coordinates(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _coordinates({"lat": value[0], "lng": value[1]})
    return None


def validate_work_area(value):
    if not isinstance(value, dict):
        raise ValueError("Draw a circle or polygon before saving your work area.")

    if value.get("type") == "circle":
        center = _point(value.get("center"))
        try:
            radius_km = float(value.get("radius_km"))
        except (TypeError, ValueError):
            center = None
            radius_km = 0
        if (
            not center
            or not math.isfinite(radius_km)
            or not 0.05 <= radius_km <= 1000
        ):
            raise ValueError("The drawn circle has invalid coordinates or radius.")
        return {
            "type": "circle",
            "center": {"lat": center[0], "lng": center[1]},
            "radius_km": radius_km,
        }

    if value.get("type") == "polygon":
        raw_points = value.get("points")
        if not isinstance(raw_points, list) or not 3 <= len(raw_points) <= 250:
            raise ValueError("Draw a polygon with between 3 and 250 points.")
        points = [_point(point) for point in raw_points]
        if any(point is None for point in points):
            raise ValueError("The drawn polygon contains invalid coordinates.")
        if len(set(points)) < 3:
            raise ValueError("The drawn polygon must contain 3 different points.")
        return {
            "type": "polygon",
            "points": [
                {"lat": latitude, "lng": longitude}
                for latitude, longitude in points
            ],
        }

    raise ValueError("Choose a circle or polygon for your work area.")


def location_in_work_area(location, work_area):
    point = _point(location)
    if not point or not isinstance(work_area, dict):
        return False
    if work_area.get("type") == "circle":
        center = _point(work_area.get("center"))
        try:
            radius_km = float(work_area.get("radius_km"))
        except (TypeError, ValueError):
            return False
        return bool(
            center
            and _distance_km(point, center) <= radius_km
        )
    if work_area.get("type") != "polygon":
        return False
    vertices = [
        _point(vertex)
        for vertex in work_area.get("points", [])
    ]
    if len(vertices) < 3 or any(vertex is None for vertex in vertices):
        return False
    latitude, longitude = point
    inside = False
    previous_latitude, previous_longitude = vertices[-1]
    for current_latitude, current_longitude in vertices:
        cross_product = (
            (longitude - previous_longitude)
            * (current_latitude - previous_latitude)
            - (latitude - previous_latitude)
            * (current_longitude - previous_longitude)
        )
        if (
            abs(cross_product) <= 1e-10
            and min(previous_latitude, current_latitude) - 1e-10
            <= latitude
            <= max(previous_latitude, current_latitude) + 1e-10
            and min(previous_longitude, current_longitude) - 1e-10
            <= longitude
            <= max(previous_longitude, current_longitude) + 1e-10
        ):
            return True
        crosses = (current_latitude > latitude) != (previous_latitude > latitude)
        if crosses:
            crossing_longitude = (
                (previous_longitude - current_longitude)
                * (latitude - current_latitude)
                / (previous_latitude - current_latitude)
                + current_longitude
            )
            if longitude < crossing_longitude:
                inside = not inside
        previous_latitude, previous_longitude = (
            current_latitude,
            current_longitude,
        )
    return inside


def _distance_km(first, second):
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    latitude_delta = latitude2 - latitude1
    longitude_delta = longitude2 - longitude1
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(min(1, haversine)))


def _order_points(shop_order):
    pickup = _point(shop_order.shop.location)
    delivery_location = shop_order.order.placed_location
    delivery = _point(delivery_location)
    if delivery is None:
        try:
            latitude, longitude = (
                value.strip()
                for value in shop_order.order.delivery_location.split(",", 1)
            )
            delivery = _point([latitude, longitude])
        except (AttributeError, ValueError):
            delivery = None
    return pickup, delivery


def _rider_batch_allows_order(
    rider,
    application,
    shop_order,
    active_orders,
    batch_radius_km,
):
    if not active_orders:
        return True
    first_order = active_orders[0]
    now = timezone.now()
    if (
        not application.public_rider_batch_accepting
        or not application.public_rider_batch_started_at
        or application.public_rider_batch_started_at + timedelta(minutes=5) <= now
        or any(
            active.status not in {"READY", "RIDER_ASSIGNED"}
            for active in active_orders
        )
    ):
        return False

    candidate_pickup, candidate_delivery = _order_points(shop_order)
    first_pickup, first_delivery = _order_points(first_order)
    if not all((candidate_pickup, candidate_delivery, first_pickup, first_delivery)):
        return False
    return (
        _distance_km(candidate_pickup, first_pickup) <= batch_radius_km
        and _distance_km(candidate_delivery, first_delivery) <= batch_radius_km
    )


def _distance_to_shop(rider, shop):
    rider_coordinates = _recent_rider_coordinates(rider)
    shop_location = shop.location
    shop_coordinates = _coordinates(shop_location)
    if rider_coordinates is None or not shop_coordinates:
        return None

    return _distance_km(rider_coordinates, shop_coordinates)


def _recent_rider_coordinates(rider):
    rider_location = rider.last_location
    coordinates = _coordinates(rider_location)
    if not coordinates:
        return None
    observed_at = rider_location.get("observed_at")
    try:
        location_age = timezone.now().timestamp() - float(observed_at)
    except (TypeError, ValueError):
        return None
    if location_age < 0 or location_age > PUBLIC_RIDER_LOCATION_MAX_AGE_SECONDS:
        return None
    return coordinates


def _homeward_schedule_is_active(application, now=None):
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    current_seconds = (
        local_now.hour * 3600
        + local_now.minute * 60
        + local_now.second
    )
    start_time = application.return_home_start_time
    start_seconds = (
        start_time.hour * 3600
        + start_time.minute * 60
        + start_time.second
    )
    seconds_since_start = (current_seconds - start_seconds) % 86400
    return seconds_since_start < application.return_home_after_minutes * 60


def _homeward_offer_allowed(rider, application, delivery):
    if (
        not application.return_home_enabled
        or not isinstance(application.home_area, dict)
        or application.home_area.get("type") != "circle"
        or not _homeward_schedule_is_active(application)
    ):
        return True
    rider_location = _recent_rider_coordinates(rider)
    delivery_location = _point(delivery)
    home_center = _point(application.home_area.get("center"))
    if not rider_location or not delivery_location or not home_center:
        return False
    return (
        _distance_km(delivery_location, home_center)
        < _distance_km(rider_location, home_center)
    )


def eligible_public_riders(shop_order):
    if not public_rider_service_enabled():
        return []
    _, riders_per_round = _public_rider_offer_settings()
    riders = list(
        ShopStaff.objects.filter(
            shop=shop_order.shop,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
            user__public_rider_application__status=(
                PublicRiderApplication.STATUS_APPROVED
            ),
            user__public_rider_application__is_online=True,
        ).select_related("user")
    )
    applications_by_user = {
        application.user_id: application
        for application in PublicRiderApplication.objects
        .filter(user_id__in=[rider.user_id for rider in riders])
    }
    active_orders_by_user = {}
    rider_user_ids = [rider.user_id for rider in riders]
    recently_completed_at = {}
    if rider_user_ids:
        recently_completed_at = dict(
            ShopOrder.objects.filter(
                assigned_rider__user_id__in=rider_user_ids,
                order__is_valid_order=True,
                delivery_completed_at__gte=(
                    timezone.now()
                    - timedelta(
                        seconds=RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
                    )
                ),
            )
            .values("assigned_rider__user_id")
            .annotate(last_completed_at=Max("delivery_completed_at"))
            .values_list("assigned_rider__user_id", "last_completed_at")
        )
        for active_order in (
            ShopOrder.objects.filter(
                assigned_rider__user_id__in=rider_user_ids,
                order__is_valid_order=True,
                status__in=[
                    "READY",
                    "RIDER_ASSIGNED",
                    "PICKED_UP",
                    "OUT_FOR_DELIVERY",
                ],
                delivery_completed_at__isnull=True,
            )
            .exclude(status__in=["DELIVERED", "COMPLETED", "CANCELLED", "RETURNED"])
            .select_related("shop", "order")
            .order_by("rider_accepted_at", "created_at", "pk")
        ):
            active_orders_by_user.setdefault(
                active_order.assigned_rider.user_id,
                [],
            ).append(active_order)
    configured_batch_radius = (
        DeliverySharingSettings.objects.filter(pk=1)
        .values_list("public_rider_batch_radius_km", flat=True)
        .first()
    )
    batch_radius_km = float(configured_batch_radius or 1.0)
    pickup, delivery = _order_points(shop_order)
    eligible_riders = []
    for rider in riders:
        application = applications_by_user.get(rider.user_id)
        work_areas = (
            [
                area
                for area in application.work_areas
                if isinstance(area, dict)
            ]
            if application and isinstance(application.work_areas, list)
            else []
        )
        if not work_areas and application and application.work_area:
            work_areas = [application.work_area]
        if (
            not application
            or rider.user_id in recently_completed_at
            or not work_areas
            or not any(
                location_in_work_area(pickup, work_area)
                for work_area in work_areas
            )
            or not any(
                location_in_work_area(delivery, work_area)
                for work_area in work_areas
            )
            or not _homeward_offer_allowed(
                rider,
                application,
                delivery,
            )
            or not _rider_batch_allows_order(
                rider,
                application,
                shop_order,
                active_orders_by_user.get(rider.user_id, []),
                batch_radius_km,
            )
        ):
            continue
        eligible_riders.append(rider)
    riders = eligible_riders
    distance_by_rider = {
        rider.pk: _distance_to_shop(rider, shop_order.shop) for rider in riders
    }
    riders.sort(
        key=lambda rider: (
            distance_by_rider[rider.pk] is None,
            distance_by_rider[rider.pk]
            if distance_by_rider[rider.pk] is not None
            else float("inf"),
            rider.user_id,
        )
    )
    return riders[:riders_per_round]


@transaction.atomic
def advance_public_rider_offer(shop_order_id, declined_user_id=None):
    shop_order = (
        ShopOrder.objects.select_for_update()
        .select_related("shop", "order")
        .filter(pk=shop_order_id)
        .first()
    )
    if (
        not shop_order
        or shop_order.status != "READY"
        or shop_order.assigned_rider_id
        or shop_order.delivery_completed_at
        or not uses_public_rider_network(shop_order)
    ):
        return None

    now = timezone.now()
    now_timestamp = now.timestamp()
    offer_timeout_seconds, riders_per_round = _public_rider_offer_settings()
    retry_at = shop_order.public_rider_offer_retry_at
    decline_cooldown_minutes = (
        DeliverySharingSettings.objects.filter(pk=1)
        .values_list("public_rider_decline_cooldown_minutes", flat=True)
        .first()
    )
    decline_cooldown = timedelta(
        minutes=float(decline_cooldown_minutes or 1.5)
    )
    declined_until = (
        shop_order.public_rider_offer_declined_until
        if isinstance(shop_order.public_rider_offer_declined_until, dict)
        else {}
    )
    active_declines = {}
    for user_id, until_timestamp in declined_until.items():
        try:
            until_timestamp = float(until_timestamp)
        except (TypeError, ValueError):
            continue
        if until_timestamp > now_timestamp:
            active_declines[str(user_id)] = until_timestamp
    if declined_user_id and declined_user_id == shop_order.public_rider_offer_user_id:
        active_declines[str(declined_user_id)] = (
            now + decline_cooldown
        ).timestamp()

    riders = eligible_public_riders(shop_order)
    eligible_user_ids = {rider.user_id for rider in riders}
    applications = list(
        PublicRiderApplication.objects.select_for_update()
        .filter(user_id__in=eligible_user_ids)
        .order_by("user_id")
        .values_list("user_id", "public_rider_offer_cooldown_until")
    )
    cooldown_by_user_id = dict(applications)
    current_user_id = shop_order.public_rider_offer_user_id
    current_offer_active = bool(
        current_user_id
        and shop_order.public_rider_offer_expires_at
        and shop_order.public_rider_offer_expires_at > now
    )
    if (
        current_offer_active
        and current_user_id in eligible_user_ids
        and declined_user_id != current_user_id
    ):
        return current_user_id

    if declined_user_id and declined_user_id == current_user_id:
        next_offer_at = now + timedelta(
            seconds=PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS,
        )
        shop_order.public_rider_offer_expires_at = now
        shop_order.public_rider_offer_retry_at = next_offer_at
        shop_order.public_rider_offer_declined_until = active_declines
        shop_order.save(update_fields=[
            "public_rider_offer_expires_at",
            "public_rider_offer_retry_at",
            "public_rider_offer_declined_until",
            "updated_at",
        ])
        return None

    if (
        current_user_id
        and shop_order.public_rider_offer_expires_at
        and shop_order.public_rider_offer_expires_at <= now
    ):
        next_offer_at = shop_order.public_rider_offer_expires_at + timedelta(
            seconds=PUBLIC_RIDER_OFFER_MIN_GAP_SECONDS,
        )
        if now < next_offer_at:
            shop_order.public_rider_offer_user = None
            shop_order.public_rider_offer_expires_at = None
            if shop_order.public_rider_offer_retry_at != next_offer_at:
                shop_order.public_rider_offer_retry_at = next_offer_at
            shop_order.save(update_fields=[
                "public_rider_offer_user",
                "public_rider_offer_expires_at",
                "public_rider_offer_retry_at",
                "updated_at",
            ])
            return None

    attempted_user_ids = [
        user_id
        for user_id in shop_order.public_rider_offer_attempted_user_ids
        if isinstance(user_id, int)
    ]
    if current_user_id:
        attempted_user_ids.append(current_user_id)
    attempted_user_ids = list(dict.fromkeys(attempted_user_ids))

    maximum_active_offers = public_rider_max_active_offers()
    active_offer_counts = dict(
        ShopOrder.objects.filter(
            public_rider_offer_user_id__in=eligible_user_ids,
            public_rider_offer_expires_at__gt=now,
            status="READY",
            assigned_rider__isnull=True,
            delivery_completed_at__isnull=True,
        )
        .exclude(pk=shop_order.pk)
        .values("public_rider_offer_user_id")
        .annotate(offer_count=Count("pk"))
        .values_list("public_rider_offer_user_id", "offer_count")
    )
    retry_elapsed = bool(retry_at and retry_at <= now)
    if retry_at and retry_at > now:
        has_available_rider = any(
            rider.user_id not in attempted_user_ids
            and str(rider.user_id) not in active_declines
            and active_offer_counts.get(rider.user_id, 0)
            < maximum_active_offers
            and not (
                cooldown_by_user_id.get(rider.user_id)
                and cooldown_by_user_id[rider.user_id] > now
            )
            for rider in riders
        )
        if not has_available_rider:
            return None

    if retry_elapsed or len(attempted_user_ids) >= riders_per_round:
        attempted_user_ids = []
    available_riders = [
        rider for rider in riders
        if rider.user_id not in attempted_user_ids
        and str(rider.user_id) not in active_declines
        and active_offer_counts.get(rider.user_id, 0)
        < maximum_active_offers
        and not (
            cooldown_by_user_id.get(rider.user_id)
            and cooldown_by_user_id[rider.user_id] > now
        )
    ]
    if not available_riders:
        if riders:
            shop_order.public_rider_offer_user = None
            shop_order.public_rider_offer_expires_at = None
            cooldown_deadlines = [
                until_timestamp
                for rider in riders
                if (until_timestamp := active_declines.get(str(rider.user_id)))
            ]
            offer_slot_waiting = any(
                rider.user_id not in attempted_user_ids
                and str(rider.user_id) not in active_declines
                and (
                    active_offer_counts.get(rider.user_id, 0)
                    >= maximum_active_offers
                    or (
                        cooldown_by_user_id.get(rider.user_id)
                        and cooldown_by_user_id[rider.user_id] > now
                    )
                )
                for rider in riders
            )
            if cooldown_deadlines:
                shop_order.public_rider_offer_retry_at = datetime.fromtimestamp(
                    min(cooldown_deadlines),
                    tz=now.tzinfo,
                )
            elif offer_slot_waiting:
                shop_order.public_rider_offer_retry_at = None
            else:
                shop_order.public_rider_offer_retry_at = (
                    now + timedelta(seconds=offer_timeout_seconds)
                )
            shop_order.public_rider_offer_attempted_user_ids = attempted_user_ids
            shop_order.public_rider_offer_declined_until = active_declines
            shop_order.save(update_fields=[
                "public_rider_offer_user",
                "public_rider_offer_expires_at",
                "public_rider_offer_retry_at",
                "public_rider_offer_attempted_user_ids",
                "public_rider_offer_declined_until",
                "updated_at",
            ])
            return None

    next_rider = available_riders[0] if available_riders else None
    next_offer_timeout_seconds = (
        _next_public_rider_offer_timeout(offer_timeout_seconds)
        if next_rider
        else None
    )
    shop_order.public_rider_offer_user = next_rider.user if next_rider else None
    shop_order.public_rider_offer_expires_at = (
        now + timedelta(seconds=next_offer_timeout_seconds)
        if next_rider
        else None
    )
    shop_order.public_rider_offer_retry_at = None
    shop_order.public_rider_offer_attempted_user_ids = attempted_user_ids
    shop_order.public_rider_offer_declined_until = active_declines
    shop_order.save(update_fields=[
        "public_rider_offer_user",
        "public_rider_offer_expires_at",
        "public_rider_offer_attempted_user_ids",
        "public_rider_offer_declined_until",
        "updated_at",
    ])

    if next_rider:
        order_reference = (
            shop_order.shop_order_number or shop_order.order.order_id
        )
        Notification.objects.create(
            user=next_rider.user,
            title="Delivery offer",
            message=(
                f"Order {order_reference} from {shop_order.shop.name} is "
                f"available for {next_offer_timeout_seconds} seconds. "
                "Accept it before the offer expires."
            ),
            notification_type="ORDER",
            related_url=reverse("rider_dashboard") + "?view=available",
        )
        return next_rider.user_id

    return None
