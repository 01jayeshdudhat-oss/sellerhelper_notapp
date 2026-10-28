import io
import json
import csv
import hashlib
import hmac
import ipaddress
import logging
import math
import random
import re
import secrets
import uuid
import zipfile
import posixpath
from xml.sax.saxutils import escape
from xml.etree import ElementTree
from urllib.parse import quote, urlencode, urlsplit
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace

import requests
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core import signing
from django.db import transaction
from django.http import (
    FileResponse,
    JsonResponse,
    HttpResponse,
    HttpResponseForbidden,
    Http404,
)
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .legal_content import (
    LEGAL_PAGES,
    REQUIRED_LEGAL_DOCUMENTS,
    legal_document_version,
)
from .offline_keyword_dictionary import (
    dictionary_is_ready,
    format_search_keywords_by_language,
    normalize_search_text,
)
from rapidfuzz.fuzz import ratio
from django.db.models import Count, F, Max, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from .forms import (
    AdminProductCatalogForm,
    _join_product_unit,
    _split_product_unit,
    JobApplicationForm,
    JobOpeningForm,
    MarketplaceLayoutSettingsForm,
    ProductCloneSettingsForm,
    SiteBrandingSettingsForm,
    SiteThemeSettingsForm,
    ShopStaffPayrollSettingsForm,
    StaffLeaveApplicationForm,
    StaffResignationForm,
    PublicRiderSettingsForm,
    CustomerSignupForm,
    OrderForm,
    ProductCategoryForm,
    ProductForm,
    SharedRiderGroupCreateForm,
    SharedRiderShopRequestForm,
    StorefrontProductEditForm,
    PublicRiderApplicationForm,
    ShopDeliveryTaxSettingsForm,
    ShopForm,
    ShopStaffPaymentSettingsForm,
)
from .models import (
    AdminProductCatalog,
    HSNCodeDescription,
    ClientProfile,
    ClientDeliveryAddress,
    INDIAN_GST_STATE_CHOICES,
    CheckoutAttempt,
    CustomerProductFeedback,
    FavoriteProduct,
    FavoriteShop,
    MarketplaceLayoutSettings,
    ProductCloneSettings,
    SiteBrandingSettings,
    SiteThemeRevision,
    SiteThemeSettings,
    LegalAcceptance,
    Order,
    OrderVoiceInstruction,
    PublicRiderApplication,
    PublicRiderWallet,
    PublicRiderWalletTransaction,
    OrderItem,
    Product,
    ProductCategory,
    SACCodeDescription,
    Shop,
    SHOP_STOREFRONT_RESERVED_SLUGS,
    ShopOrder,
    ShopStaff,
    ShopStaffWallet,
    ShopStaffWalletTransaction,
    ProductShop,
    JobOpening,
    JobApplication,
    Notification,
    NotificationDeviceToken,
    Coupon,
    ClientWalletTransaction,
    UserActivity,
    UserPresence,
    StockMovement,
    ShopProductForecast,
    ShopVendor,
    VendorPurchase,
    VendorPurchaseItem,
    VendorPurchasePayment,
    VendorPurchaseReturn,
    VendorPurchaseReturnItem,
    DeliverySharingSettings,
    StaffDutySession,
    StaffLeaveApplication,
    StaffResignationRequest,
    StaffPayrollRecord,
    SharedRiderGroup,
    SharedRiderGroupShop,
    OrderStatusHistory,
    ShopWallet,
    ShopWalletTransaction,
    SharedRiderDeliveryPayment,
    ShopStorefrontSettings,
    RiderCashLedgerEntry,
    CODBalancePayment,
    CODPaymentApprovalRequest,
    SiteWallet,
    SiteWalletTransaction,
    ShopPayout,
    SavedBasket,
    SavedBasketItem,
    get_product_category_choices,
    generate_otp_code,
)
from .utils import (
    distance_km_between_points,
    normalize_pincodes,
    shop_delivers_to_location,
)
from .live_updates import queue_order_update
from .public_riders import (
    public_rider_service_enabled,
    sync_public_rider_shop_memberships,
)
from .qr_codes import qr_code_data_uri as _qr_code_data_uri
from .road_routing import local_road_route_distance_km
from .public_rider_dispatch import (
    PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS,
    RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
    advance_public_rider_offer,
    eligible_public_riders,
    validate_work_area,
    uses_public_rider_network,
)

logger = logging.getLogger(__name__)

PORTAL_LABELS = {
    "CUSTOMER": "Customer",
    "B2B_CUSTOMER": "B2B customer",
    "SHOP_OWNER": "Shop owner",
    "SHOP_STAFF": "Shop staff",
    "RIDER": "Public rider",
    "ADMIN": "Admin",
}


def _nearby_shop_sort_key(entry):
    distance_km = entry["distance_km"]
    return (
        distance_km is None,
        distance_km if distance_km is not None else float("inf"),
        -entry["reputation"]["rank_score"],
        entry["shop"].name.casefold(),
    )


def site_manifest(request):
    branding = SiteBrandingSettings.objects.first()
    site_name = branding.site_name if branding else "Seller Helper"
    icons = []
    if branding and branding.app_icon:
        icons.append({
            "src": request.build_absolute_uri(branding.app_icon.url),
            "sizes": "any",
            "purpose": "any maskable",
        })
    return JsonResponse({
        "name": site_name,
        "short_name": site_name[:12],
        "description": branding.tagline if branding else "",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f5f7fb",
        "theme_color": "#0b5ed7",
        "icons": icons,
    })


def _record_required_legal_acceptances(user):
    LegalAcceptance.objects.bulk_create(
        [
            LegalAcceptance(
                user=user,
                document_slug=document_slug,
                document_version=legal_document_version(document_slug),
            )
            for document_slug in REQUIRED_LEGAL_DOCUMENTS
        ],
        ignore_conflicts=True,
    )


def _category_product_name_key(item):
    return " ".join((item.product.product_name or "").casefold().split())


def _interleave_category_products(items, shop_order):
    products_by_name = defaultdict(list)
    shop_rank = {
        shop_id: index
        for index, shop_id in enumerate(shop_order)
    }
    for item in items:
        products_by_name[_category_product_name_key(item)].append(item)

    for products in products_by_name.values():
        products.sort(key=lambda item: (
            shop_rank.get(item.shop_id, len(shop_rank)),
            item.product_id or 0,
            item.pk,
        ))

    product_names = list(products_by_name)
    random.shuffle(product_names)

    ordered_items = []
    max_products_per_name = max(
        (len(products) for products in products_by_name.values()),
        default=0,
    )
    for product_index in range(max_products_per_name):
        for product_name in product_names:
            products = products_by_name[product_name]
            if product_index < len(products):
                ordered_items.append(products[product_index])

    return ordered_items


def _checkout_charge_totals(
    subtotal,
    discount,
    wallet_amount,
    payment_method,
    additional_amount=Decimal("0.00"),
):
    cent = Decimal("0.01")
    subtotal = Decimal(subtotal)
    discount = Decimal(discount)
    wallet_amount = Decimal(wallet_amount)
    additional_amount = Decimal(additional_amount)
    platform_charge = (
        Decimal(str(settings.PLATFORM_CHARGE_FIXED))
        + subtotal * Decimal(str(settings.PLATFORM_CHARGE_PERCENT)) / Decimal("100")
    ).quantize(cent, rounding=ROUND_HALF_UP)
    amount_before_gateway_fee = max(
        Decimal("0.00"),
        subtotal - discount - wallet_amount + platform_charge + additional_amount,
    )
    razorpay_fee = (
        amount_before_gateway_fee
        * Decimal(str(settings.RAZORPAY_CUSTOMER_FEE_PERCENT))
        / Decimal("100")
    ).quantize(cent, rounding=ROUND_HALF_UP) if payment_method == "RAZORPAY" else Decimal("0.00")
    return {
        "platform_charge": platform_charge,
        "razorpay_fee": razorpay_fee,
        "amount_due": amount_before_gateway_fee + razorpay_fee,
    }


def _checkout_uses_wallet_balance(request):
    submitted_values = request.POST.getlist("use_wallet_balance")
    if not submitted_values:
        return True
    return "1" in submitted_values


def _is_b2b_customer(request, client=None):
    if request.session.get("active_portal", "CUSTOMER") != "B2B_CUSTOMER":
        return False
    if client is None:
        client = (
            ClientProfile.objects.filter(user=request.user).first()
            if request.user.is_authenticated
            else None
        )
    gstin = (getattr(client, "gstin", "") or "").strip().upper()
    return bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin))


def _cart_line_key(product_shop_id, *, is_bulk_order=False):
    prefix = "bulk:" if is_bulk_order else ""
    return f"{prefix}{product_shop_id}"


def _parse_cart_line_key(value):
    raw_value = str(value)
    is_bulk_order = raw_value.startswith("bulk:")
    assignment_id = raw_value[5:] if is_bulk_order else raw_value
    if not assignment_id.isdigit():
        return None
    return int(assignment_id), is_bulk_order


def _cart_quantity(cart, product_shop_id, *, is_bulk_order=False):
    try:
        return max(
            0,
            int(cart.get(
                _cart_line_key(product_shop_id, is_bulk_order=is_bulk_order),
                0,
            )),
        )
    except (AttributeError, TypeError, ValueError):
        return 0


_UNASSIGNED_CART_ADDRESS = "unassigned"


def _address_scoped_cart(request, client=None):
    if client is None and request.user.is_authenticated:
        client = (
            ClientProfile.objects.filter(user=request.user).first()
            if request.user.is_authenticated
            else None
        )

    addresses = (
        client.delivery_addresses.filter(is_active=True).order_by("-is_default", "id")
        if client
        else ClientDeliveryAddress.objects.none()
    )
    requested_address_id = request.session.get("cart_delivery_address_id")
    address = (
        addresses.filter(pk=int(requested_address_id)).first()
        if str(requested_address_id or "").isdigit()
        else None
    )
    if address is None:
        address = addresses.filter(is_default=True).first() or addresses.first()
    address_key = str(address.pk) if address else _UNASSIGNED_CART_ADDRESS

    carts = request.session.get("carts_by_delivery_address")
    if not isinstance(carts, dict):
        carts = {}
    legacy_cart = request.session.get("cart", {})
    if not isinstance(legacy_cart, dict):
        legacy_cart = {}

    if (
        address_key in carts
        and request.session.get("cart_delivery_address_id") == address_key
        and legacy_cart != carts[address_key]
    ):
        carts[address_key] = legacy_cart

    if address_key not in carts:
        if _UNASSIGNED_CART_ADDRESS in carts and address_key != _UNASSIGNED_CART_ADDRESS:
            carts[address_key] = carts.pop(_UNASSIGNED_CART_ADDRESS)
        elif not carts:
            carts[address_key] = legacy_cart
        else:
            carts[address_key] = {}

    cart = carts[address_key]
    if not isinstance(cart, dict):
        cart = {}
        carts[address_key] = cart

    if (
        request.session.get("carts_by_delivery_address") != carts
        or request.session.get("cart_delivery_address_id") != address_key
        or request.session.get("cart") != cart
    ):
        request.session["carts_by_delivery_address"] = carts
        request.session["cart_delivery_address_id"] = address_key
        request.session["cart"] = cart
        request.session.modified = True
    return cart, address


def _save_address_scoped_cart(request, cart):
    _, address = _address_scoped_cart(request)
    address_key = str(address.pk) if address else _UNASSIGNED_CART_ADDRESS
    carts = request.session.get("carts_by_delivery_address", {})
    if not isinstance(carts, dict):
        carts = {}
    carts[address_key] = cart
    request.session["carts_by_delivery_address"] = carts
    request.session["cart_delivery_address_id"] = address_key
    request.session["cart"] = cart
    request.session.modified = True


def _clear_address_scoped_carts(request, address):
    address_key = str(address.pk) if address else _UNASSIGNED_CART_ADDRESS
    empty_cart = {}
    request.session["carts_by_delivery_address"] = {address_key: empty_cart}
    request.session["cart_delivery_address_id"] = address_key
    request.session["cart"] = empty_cart
    request.session.modified = True


def _cart_current_location(request):
    latitude = request.POST.get("current_lat") or request.GET.get("current_lat")
    longitude = request.POST.get("current_lng") or request.GET.get("current_lng")
    if latitude and longitude:
        submitted_location = {"lat": latitude, "lng": longitude}
        if _valid_route_coordinates(submitted_location):
            request.session["marketplace_current_location"] = {
                "lat": float(latitude),
                "lng": float(longitude),
            }
            request.session.modified = True

    location = request.session.get("marketplace_current_location")
    if not isinstance(location, dict) or not _valid_route_coordinates(location):
        return None
    return {"lat": float(location["lat"]), "lng": float(location["lng"])}


def _switch_address_scoped_cart(request, address):
    address_key = str(address.pk)
    carts = request.session.get("carts_by_delivery_address", {})
    if not isinstance(carts, dict) or not carts:
        legacy_cart = request.session.get("cart", {})
        if not isinstance(legacy_cart, dict):
            legacy_cart = {}
        carts = {}
        if legacy_cart:
            carts[address_key] = legacy_cart
    else:
        _address_scoped_cart(request)
        carts = request.session.get("carts_by_delivery_address", {})
        if not isinstance(carts, dict):
            carts = {}
    cart = carts.setdefault(address_key, {})
    if not isinstance(cart, dict):
        cart = {}
        carts[address_key] = cart
    request.session["carts_by_delivery_address"] = carts
    request.session["cart_delivery_address_id"] = address_key
    request.session["cart"] = cart
    request.session.modified = True


def _zero_unavailable_address_cart_items(request, address):
    _address_scoped_cart(request)
    address_key = str(address.pk)
    carts = request.session.get("carts_by_delivery_address", {})
    if not isinstance(carts, dict):
        carts = {}
    cart = carts.get(address_key, {})
    if not isinstance(cart, dict):
        cart = {}

    assignment_ids = {
        parsed[0]
        for key in cart
        if (parsed := _parse_cart_line_key(key)) is not None
    }
    assignments = ProductShop.objects.select_related("shop").filter(
        pk__in=assignment_ids,
    )
    unavailable_assignment_ids = {
        assignment.pk
        for assignment in assignments
        if not _shop_can_deliver_to_address(assignment.shop, address)
    }
    for key in list(cart):
        parsed = _parse_cart_line_key(key)
        if parsed and parsed[0] in unavailable_assignment_ids:
            cart[key] = 0

    carts[address_key] = cart
    request.session["carts_by_delivery_address"] = carts
    if request.session.get("cart_delivery_address_id") == address_key:
        request.session["cart"] = cart
    request.session.modified = True


def _parse_numeric_unit(unit):
    unit = (unit or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(\s*)(\D.*)", unit)
    if not match:
        return None, unit
    return Decimal(match.group(1)), match.group(3).strip()


def _quantity_unit_display(quantity, unit):
    unit = (unit or "").strip()
    amount, display_unit = _parse_numeric_unit(unit)
    if amount is None:
        return unit

    total = format((amount * quantity).normalize(), "f")
    separator = " " if re.search(r"\s", unit[len(str(amount)):]) else ""
    return f"{total}{separator}{display_unit}"


def _quantity_unit_total(quantity, unit):
    unit = (unit or "").strip()
    amount, _display_unit = _parse_numeric_unit(unit)
    if amount is not None:
        return _quantity_unit_display(quantity, unit)
    return f"{quantity} {unit}".strip()


def _estimated_weight_for_quantity(quantity, unit):
    unit_amount, _display_unit = _parse_numeric_unit(unit)
    estimated_weight = (
        unit_amount * quantity
        if unit_amount is not None
        else Decimal(quantity)
    )
    return estimated_weight.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _weight_display_unit(unit):
    _unit_amount, display_unit = _parse_numeric_unit(unit)
    return display_unit or unit or "weight"


def _weight_in_kilograms(weight, unit):
    _unit_amount, weight_unit = _parse_numeric_unit(unit)
    normalized_unit = re.sub(r"[\s.]", "", weight_unit.casefold())
    weight = Decimal(weight)
    if normalized_unit in {"g", "gm", "gms", "gram", "grams"}:
        return weight / Decimal("1000")
    if normalized_unit in {"mg", "milligram", "milligrams"}:
        return weight / Decimal("1000000")
    if normalized_unit in {"kg", "kgs", "kilogram", "kilograms", "kilo", "kilos"}:
        return weight
    if normalized_unit in {"lb", "lbs", "pound", "pounds"}:
        return weight * Decimal("0.45359237")
    if normalized_unit in {"oz", "ounce", "ounces"}:
        return weight * Decimal("0.028349523125")
    return weight


def _shop_order_estimated_load(shop_order):
    items = list(shop_order.order_items.all())
    estimated_weight = Decimal("0.00")
    estimated_volume = Decimal("0.00")
    item_count = 0
    for item in items:
        quantity = max(0, item.quantity - item.returned_quantity)
        if not quantity:
            continue
        item_count += quantity
        unit = item.unit or (item.product.unit if item.product_id else "")
        unit_amount, weight_unit = _parse_numeric_unit(unit)
        normalized_unit = re.sub(r"[\s.]", "", weight_unit.casefold())
        is_weight_unit = normalized_unit in {
            "g", "gm", "gms", "gram", "grams", "mg", "milligram",
            "milligrams", "kg", "kgs", "kilogram", "kilograms", "kilo",
            "kilos", "lb", "lbs", "pound", "pounds", "oz", "ounce",
            "ounces",
        }
        if item.is_approximate_weight and (
            item.final_weight is not None or item.estimated_weight is not None
        ):
            weight = item.final_weight or item.estimated_weight
            estimated_weight += _weight_in_kilograms(weight, unit)
        elif is_weight_unit and unit_amount is not None:
            weight = _estimated_weight_for_quantity(quantity, unit)
            estimated_weight += _weight_in_kilograms(weight, unit)
        else:
            estimated_weight += max(
                Decimal(quantity) * Decimal("0.50"),
                item.total / Decimal("1000"),
            )
        estimated_volume += max(
            Decimal(quantity) * Decimal("0.25"),
            item.total / Decimal("5000"),
        )

    if not items:
        item_count = shop_order.product_count
        estimated_weight = shop_order.total_weight_kg or max(
            Decimal(item_count) * Decimal("0.50"),
            shop_order.total_amount / Decimal("1000"),
        )
        estimated_volume = max(
            Decimal(item_count) * Decimal("0.25"),
            shop_order.total_amount / Decimal("5000"),
        )

    if not estimated_weight and shop_order.total_weight_kg:
        estimated_weight = shop_order.total_weight_kg
    return {
        "weight_kg": estimated_weight.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        ),
        "volume_cubic_ft": estimated_volume.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        ),
        "item_count": item_count or shop_order.product_count,
    }


def _rider_load_fits_capacity(rider, weight_kg, volume_cubic_ft):
    if (
        rider.max_load_weight_kg is None
        or rider.max_load_volume_cubic_ft is None
    ):
        return True
    return (
        weight_kg <= rider.max_load_weight_kg
        and volume_cubic_ft <= rider.max_load_volume_cubic_ft
    )


def _approximate_weight_total(price, final_weight, unit):
    unit_amount, _display_unit = _parse_numeric_unit(unit)
    billing_quantity = (
        Decimal(final_weight) / unit_amount
        if unit_amount is not None
        else Decimal(final_weight)
    )
    return (Decimal(price) * billing_quantity).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _quantity_unit_calculation(quantity, unit):
    unit = (unit or "").strip()
    amount, _display_unit = _parse_numeric_unit(unit)
    total_unit = _quantity_unit_total(quantity, unit)
    if amount is None:
        return total_unit
    return f"{quantity} × {unit} = {total_unit}"


def _json_stock_quantity(quantity):
    quantity = Decimal(quantity)
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return float(quantity)


def _inventory_unit_factor(unit_text):
    normalized = re.sub(r"[\s.]", "", (unit_text or "").casefold())
    factors = {
        "mg": ("mass", Decimal("0.001")),
        "milligram": ("mass", Decimal("0.001")),
        "milligrams": ("mass", Decimal("0.001")),
        "g": ("mass", Decimal("1")),
        "gm": ("mass", Decimal("1")),
        "gms": ("mass", Decimal("1")),
        "gram": ("mass", Decimal("1")),
        "grams": ("mass", Decimal("1")),
        "kg": ("mass", Decimal("1000")),
        "kgs": ("mass", Decimal("1000")),
        "kilo": ("mass", Decimal("1000")),
        "kilos": ("mass", Decimal("1000")),
        "kilogram": ("mass", Decimal("1000")),
        "kilograms": ("mass", Decimal("1000")),
        "lb": ("mass", Decimal("453.59237")),
        "lbs": ("mass", Decimal("453.59237")),
        "pound": ("mass", Decimal("453.59237")),
        "pounds": ("mass", Decimal("453.59237")),
        "oz": ("mass", Decimal("28.349523125")),
        "ounce": ("mass", Decimal("28.349523125")),
        "ounces": ("mass", Decimal("28.349523125")),
        "l": ("volume", Decimal("1000")),
        "lt": ("volume", Decimal("1000")),
        "liter": ("volume", Decimal("1000")),
        "litre": ("volume", Decimal("1000")),
        "liters": ("volume", Decimal("1000")),
        "litres": ("volume", Decimal("1000")),
        "ml": ("volume", Decimal("1")),
        "milliliter": ("volume", Decimal("1")),
        "millilitre": ("volume", Decimal("1")),
        "m": ("length", Decimal("1000")),
        "meter": ("length", Decimal("1000")),
        "metre": ("length", Decimal("1000")),
        "cm": ("length", Decimal("10")),
        "mm": ("length", Decimal("1")),
        "pc": ("count", Decimal("1")),
        "pcs": ("count", Decimal("1")),
        "piece": ("count", Decimal("1")),
        "pieces": ("count", Decimal("1")),
        "each": ("count", Decimal("1")),
        "ea": ("count", Decimal("1")),
        "unit": ("count", Decimal("1")),
        "units": ("count", Decimal("1")),
    }
    return factors.get(normalized, (f"custom:{normalized}", Decimal("1")))


def _converted_purchase_quantity(quantity, purchase_value, purchase_unit, inventory_value, inventory_unit):
    purchase_dimension, purchase_factor = _inventory_unit_factor(purchase_unit)
    inventory_dimension, inventory_factor = _inventory_unit_factor(inventory_unit)
    if purchase_dimension != inventory_dimension:
        raise ValueError(
            f"Purchase unit {purchase_unit!r} cannot be converted to inventory unit "
            f"{inventory_unit!r}."
        )
    converted = (
        Decimal(quantity)
        * purchase_value
        * purchase_factor
        / (inventory_value * inventory_factor)
    )
    return converted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _build_gst_breakdown(
    items,
    address,
    coupon_discount=Decimal("0.00"),
    *,
    require_state=False,
    delivery_fees=None,
    buyer_gstin_override=None,
):
    cent = Decimal("0.01")
    coupon_discount = Decimal(coupon_discount).quantize(cent, rounding=ROUND_HALF_UP)
    gross_total = sum((Decimal(item["total"]) for item in items), Decimal("0.00"))
    discount_remaining = min(coupon_discount, gross_total)
    rows_by_shop = {}
    total_gst = Decimal("0.00")
    delivery_total = Decimal("0.00")
    buyer_state = (getattr(address, "state_code", "") or "").strip()
    buyer_gstin = (
        getattr(address, "gstin", "")
        or buyer_gstin_override
        or ""
    ).strip().upper()

    for index, item in enumerate(items):
        assignment = item.get("assignment")
        shop = item.get("shop") or assignment.shop
        product = item.get("product") or (assignment.product if assignment else None)
        row = rows_by_shop.setdefault(shop.pk, {
            "shop": shop,
            "items": [],
            "taxable_amount": Decimal("0.00"),
            "cgst_amount": Decimal("0.00"),
            "sgst_amount": Decimal("0.00"),
            "igst_amount": Decimal("0.00"),
            "delivery_gst_amount": Decimal("0.00"),
            "delivery_hsn_code": getattr(shop, "delivery_hsn_code", "") or "",
            "delivery_gst_rate": Decimal(str(
                getattr(shop, "delivery_gst_rate", None)
                if getattr(shop, "delivery_gst_rate", None) is not None
                else settings.DELIVERY_SERVICE_GST_RATE
            )),
            "delivery_fee": Decimal(str(
                delivery_fees[shop.pk]
                if delivery_fees is not None and shop.pk in delivery_fees
                else getattr(shop, "delivery_fee", settings.DELIVERY_SERVICE_FEE)
            )).quantize(
                cent,
                rounding=ROUND_HALF_UP,
            ),
            "seller_state_code": shop.gst_state_code if shop.gstin else "",
            "buyer_state_code": buyer_state if shop.gstin else "",
            "seller_gstin": shop.gstin if shop.gstin else "",
            "buyer_gstin": buyer_gstin if shop.gstin else "",
            "seller_billing_address": shop.billing_address if shop.gstin else "",
        })
        item_gross = Decimal(item["total"]).quantize(cent, rounding=ROUND_HALF_UP)
        if gross_total and discount_remaining:
            if index == len(items) - 1:
                item_discount = min(item_gross, discount_remaining)
            else:
                item_discount = min(
                    item_gross,
                    (coupon_discount * item_gross / gross_total).quantize(
                        cent,
                        rounding=ROUND_HALF_UP,
                    ),
                    discount_remaining,
                )
        else:
            item_discount = Decimal("0.00")
        discount_remaining = max(Decimal("0.00"), discount_remaining - item_discount)
        taxable_amount = item_gross - item_discount
        gst_rate = Decimal(str(item.get("gst_rate", product.gst_rate if product else 0) or 0))
        hsn_code = item.get("hsn_code", product.hsn_code if product else "") or ""
        cgst = sgst = igst = Decimal("0.00")
        if require_state and shop.gstin and (not shop.gst_state_code or not buyer_state):
            raise ValueError(
                f"Select a delivery state and complete GST state details for {shop.name} before checkout."
            )
        if shop.gstin and gst_rate:
            tax = (taxable_amount * gst_rate / Decimal("100")).quantize(
                cent,
                rounding=ROUND_HALF_UP,
            )
            if shop.gst_state_code and buyer_state and shop.gst_state_code == buyer_state:
                cgst = (tax / 2).quantize(cent, rounding=ROUND_HALF_UP)
                sgst = tax - cgst
            else:
                igst = tax
        line = {
            "item": item,
            "hsn_code": hsn_code,
            "gst_rate": gst_rate if shop.gstin else Decimal("0.00"),
            "taxable_amount": taxable_amount,
            "cgst_amount": cgst,
            "sgst_amount": sgst,
            "igst_amount": igst,
        }
        row["items"].append(line)
        row["taxable_amount"] += taxable_amount
        row["cgst_amount"] += cgst
        row["sgst_amount"] += sgst
        row["igst_amount"] += igst

    for row in rows_by_shop.values():
        fee = row["delivery_fee"]
        delivery_total += fee
        delivery_rate = row["delivery_gst_rate"]
        if row["shop"].gstin and fee and delivery_rate:
            if require_state and (not row["seller_state_code"] or not buyer_state):
                raise ValueError(
                    f"Select a delivery state and complete GST state details for {row['shop'].name} before checkout."
                )
            delivery_tax = (fee * delivery_rate / Decimal("100")).quantize(
                cent,
                rounding=ROUND_HALF_UP,
            )
            if row["seller_state_code"] and buyer_state and row["seller_state_code"] == buyer_state:
                delivery_cgst = (delivery_tax / 2).quantize(cent, rounding=ROUND_HALF_UP)
                row["cgst_amount"] += delivery_cgst
                row["sgst_amount"] += delivery_tax - delivery_cgst
            else:
                row["igst_amount"] += delivery_tax
            row["delivery_gst_amount"] = delivery_tax
        row["taxable_amount"] += fee
        total_gst += row["cgst_amount"] + row["sgst_amount"] + row["igst_amount"]

    for row in rows_by_shop.values():
        for name in ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "delivery_gst_amount", "delivery_fee"):
            row[name] = row[name].quantize(cent, rounding=ROUND_HALF_UP)

    for line in (line for row in rows_by_shop.values() for line in row["items"]):
        for name in ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount"):
            line[name] = line[name].quantize(cent, rounding=ROUND_HALF_UP)
        line["item"].update({
            "hsn_code": line["hsn_code"],
            "gst_rate": line["gst_rate"],
            "taxable_amount": line["taxable_amount"],
            "cgst_amount": line["cgst_amount"],
            "sgst_amount": line["sgst_amount"],
            "igst_amount": line["igst_amount"],
        })

    return {
        "shops": rows_by_shop,
        "total_gst": total_gst.quantize(cent, rounding=ROUND_HALF_UP),
        "delivery_total": delivery_total.quantize(cent, rounding=ROUND_HALF_UP),
        "buyer_state_code": buyer_state,
        "buyer_gstin": buyer_gstin,
    }


def _checkout_shop_summary(items, gst_breakdown):
    shops = {}
    for item in items:
        shop = item["assignment"].shop
        shops[shop.pk] = shop

    summaries = []
    for shop_id, tax_row in gst_breakdown["shops"].items():
        shop = shops[shop_id]
        tax_total = (
            tax_row["cgst_amount"]
            + tax_row["sgst_amount"]
            + tax_row["igst_amount"]
        )
        delivery_fee = tax_row["delivery_fee"]
        product_total = max(
            Decimal("0.00"),
            tax_row["taxable_amount"] - delivery_fee,
        )
        summaries.append({
            "shop_name": shop.name,
            "product_total": str(product_total),
            "delivery_fee": str(delivery_fee),
            "delivery_free": delivery_fee == Decimal("0.00"),
            "delivery_label": "Free" if not delivery_fee else f"₹{delivery_fee:.2f}",
            "gst_total": str(tax_total),
            "shop_total": str(product_total + delivery_fee + tax_total),
        })
    return summaries


ORDER_INSTRUCTION_AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/3gpp": ".3gp",
}
ORDER_INSTRUCTION_AUDIO_MAX_BYTES = 10 * 1024 * 1024


def _normalize_checkout_instruction_draft_ids(request, shop_ids, raw_draft_ids):
    if not isinstance(raw_draft_ids, dict):
        return {}, True

    valid_recipients = {
        OrderVoiceInstruction.RECIPIENT_SHOP,
        OrderVoiceInstruction.RECIPIENT_RIDER,
    }
    candidates = []
    for shop_id, recipient_ids in raw_draft_ids.items():
        if isinstance(recipient_ids, dict):
            try:
                parsed_shop_id = int(shop_id)
            except (TypeError, ValueError):
                return {}, True
            for recipient, draft_id in recipient_ids.items():
                if recipient not in valid_recipients:
                    return {}, True
                candidates.append((parsed_shop_id, recipient, draft_id))

    if not candidates:
        legacy_ids = {
            recipient: draft_id
            for recipient, draft_id in raw_draft_ids.items()
            if recipient in valid_recipients
        }
        if legacy_ids:
            legacy_drafts = OrderVoiceInstruction.objects.filter(
                pk__in=legacy_ids.values(),
                customer=request.user,
                shop_order__isnull=True,
            ).values("pk", "shop_id", "recipient")
            candidate_ids = {
                draft["pk"]: (
                    draft["shop_id"],
                    draft["recipient"],
                    legacy_ids[draft["recipient"]],
                )
                for draft in legacy_drafts
            }
            candidates.extend(candidate_ids.values())
            if len(candidates) != len(legacy_ids):
                return {}, True

    parsed_candidates = []
    for shop_id, recipient, draft_id in candidates:
        try:
            parsed_draft_id = int(draft_id)
        except (TypeError, ValueError):
            return {}, True
        parsed_candidates.append((shop_id, recipient, parsed_draft_id))

    existing = {
        draft.pk: draft
        for draft in OrderVoiceInstruction.objects.filter(
            pk__in=[candidate[2] for candidate in parsed_candidates],
            customer=request.user,
            shop_order__isnull=True,
        )
    }
    normalized = {}
    invalid = False
    for shop_id, recipient, draft_id in parsed_candidates:
        draft = existing.get(draft_id)
        if (
            draft is None
            or draft.shop_id != shop_id
            or draft.recipient != recipient
        ):
            invalid = True
            continue
        normalized.setdefault(str(shop_id), {})[recipient] = draft_id
    return normalized, invalid


def _checkout_order_instruction_drafts(request, shop_ids):
    raw_draft_ids = request.session.get("order_voice_instruction_draft_ids", {})
    legacy_draft_id = request.session.pop(
        "order_voice_instruction_draft_id",
        None,
    )
    if legacy_draft_id and not raw_draft_ids:
        legacy_draft = OrderVoiceInstruction.objects.filter(
            pk=legacy_draft_id,
            customer=request.user,
            shop_order__isnull=True,
        ).first()
        if legacy_draft:
            raw_draft_ids = {legacy_draft.recipient: legacy_draft.pk}

    normalized_ids, _invalid = _normalize_checkout_instruction_draft_ids(
        request,
        shop_ids,
        raw_draft_ids,
    )
    drafts = {}
    for shop_id, recipient_ids in normalized_ids.items():
        parsed_shop_id = int(shop_id)
        if parsed_shop_id not in shop_ids:
            stale_drafts = OrderVoiceInstruction.objects.filter(
                pk__in=recipient_ids.values(),
                customer=request.user,
                shop_order__isnull=True,
            )
            for stale_draft in stale_drafts:
                if stale_draft.audio_file:
                    stale_draft.audio_file.delete(save=False)
                stale_draft.delete()
            continue
        for recipient, draft_id in recipient_ids.items():
            draft = OrderVoiceInstruction.objects.filter(
                pk=draft_id,
                customer=request.user,
                shop_id=parsed_shop_id,
                shop_order__isnull=True,
                recipient=recipient,
            ).first()
            if draft:
                drafts.setdefault(parsed_shop_id, {})[recipient] = draft

    normalized_ids = {
        str(shop_id): {
            recipient: draft.pk
            for recipient, draft in recipient_drafts.items()
        }
        for shop_id, recipient_drafts in drafts.items()
    }
    if raw_draft_ids != normalized_ids or legacy_draft_id:
        request.session["order_voice_instruction_draft_ids"] = normalized_ids
        request.session.modified = True
    return drafts


def _track_checkout_attempt(request, items):
    posted_checkout_key = request.POST.get("checkout_attempt_key", "")
    checkout_key = (
        posted_checkout_key
        if request.method == "POST"
        and isinstance(posted_checkout_key, str)
        and re.fullmatch(r"[0-9a-f]{32}", posted_checkout_key)
        else request.session.get("checkout_attempt_key")
    )
    if not isinstance(checkout_key, str) or not re.fullmatch(r"[0-9a-f]{32}", checkout_key):
        checkout_key = uuid.uuid4().hex
        request.session["checkout_attempt_key"] = checkout_key
    else:
        request.session["checkout_attempt_key"] = checkout_key

    grouped_items = defaultdict(list)
    totals_by_shop = defaultdict(lambda: Decimal("0.00"))
    shops_by_id = {}
    for item in items:
        assignment = item["assignment"]
        shop = assignment.shop
        shops_by_id[shop.pk] = shop
        grouped_items[shop.pk].append({
            "product_name": assignment.product.product_name,
            "quantity": item["quantity"],
            "unit": item["unit"],
            "line_total": str(item["total"]),
            "image_url": assignment.display_image_url,
            "image_urls": assignment.display_image_urls,
            "product_shop_id": assignment.pk,
        })
        totals_by_shop[shop.pk] += item["total"]

    CheckoutAttempt.objects.filter(
        customer=request.user,
        checkout_key=checkout_key,
    ).exclude(shop_id__in=shops_by_id).delete()
    for shop_id, shop in shops_by_id.items():
        CheckoutAttempt.objects.update_or_create(
            customer=request.user,
            checkout_key=checkout_key,
            shop=shop,
            defaults={
                "cart_items": grouped_items[shop_id],
                "total_amount": totals_by_shop[shop_id],
            },
        )
    request.session.modified = True


def _save_checkout_order_instruction(request, checkout_shops):
    drafts = _checkout_order_instruction_drafts(
        request,
        {shop.pk for shop in checkout_shops},
    )
    saved_drafts = {}
    legacy_shop_id = request.POST.get("order_instruction_shop", "").strip()
    shop_by_id = {str(shop.pk): shop for shop in checkout_shops}
    for shop in checkout_shops:
        for recipient in (
            OrderVoiceInstruction.RECIPIENT_SHOP,
            OrderVoiceInstruction.RECIPIENT_RIDER,
        ):
            field_suffix = recipient.lower()
            draft = drafts.get(shop.pk, {}).get(recipient)
            text_field = f"order_instruction_text_{field_suffix}_{shop.pk}"
            audio_field = f"order_instruction_audio_{field_suffix}_{shop.pk}"
            clear_field = f"clear_order_instruction_audio_{field_suffix}_{shop.pk}"
            if text_field not in request.POST and legacy_shop_id == str(shop.pk):
                legacy_generic_instruction = (
                    "order_instruction_text" in request.POST
                    or "order_instruction_audio" in request.FILES
                )
                legacy_recipient = request.POST.get(
                    "order_instruction_recipient",
                    OrderVoiceInstruction.RECIPIENT_SHOP,
                )
                if legacy_generic_instruction:
                    if recipient != legacy_recipient:
                        continue
                    text_field = "order_instruction_text"
                    audio_field = "order_instruction_audio"
                    clear_field = "clear_order_instruction_audio"
                else:
                    text_field = f"order_instruction_text_{field_suffix}"
                    audio_field = f"order_instruction_audio_{field_suffix}"
                    clear_field = f"clear_order_instruction_audio_{field_suffix}"

            instruction_text = request.POST.get(
                text_field,
                draft.instruction_text if draft else "",
            ).strip()
            if len(instruction_text) > 1000:
                raise ValueError("Each order instruction must be 1,000 characters or fewer.")
            audio = request.FILES.get(audio_field)
            clear_audio = request.POST.get(clear_field) == "1"
            has_existing_audio = bool(draft and draft.audio_file and not clear_audio)
            if not instruction_text and not audio and not has_existing_audio:
                if draft:
                    if draft.audio_file:
                        draft.audio_file.delete(save=False)
                    draft.delete()
                continue

            audio_content_type = ""
            if audio:
                audio_content_type = (audio.content_type or "").lower().split(";", 1)[0]
                extension = ORDER_INSTRUCTION_AUDIO_TYPES.get(audio_content_type)
                if not extension:
                    raise ValueError(
                        "Use a supported audio format such as WebM, MP3, MP4, OGG, or WAV."
                    )
                if audio.size > ORDER_INSTRUCTION_AUDIO_MAX_BYTES:
                    raise ValueError("Audio instructions must be 10 MB or smaller.")
                audio.name = f"{uuid.uuid4().hex}{extension}"

            if draft is None:
                draft = OrderVoiceInstruction(
                    customer=request.user,
                    shop=shop,
                    recipient=recipient,
                )
            draft.instruction_text = instruction_text
            if clear_audio and draft.audio_file:
                draft.audio_file.delete(save=False)
                draft.audio_content_type = ""
            if audio:
                if draft.audio_file:
                    draft.audio_file.delete(save=False)
                draft.audio_file = audio
                draft.audio_content_type = audio_content_type
            draft.save()
            saved_drafts.setdefault(shop.pk, {})[recipient] = draft

    request.session["order_voice_instruction_draft_ids"] = {
        str(shop_id): {
            recipient: draft.pk
            for recipient, draft in recipient_drafts.items()
        }
        for shop_id, recipient_drafts in saved_drafts.items()
    }
    request.session.pop("order_voice_instruction_draft_id", None)
    return saved_drafts


def _instruction_audio_is_available(instruction, shop_order):
    if not instruction or not instruction.audio_file:
        return False
    if (
        shop_order.delivery_completed_at
        and timezone.now() >= shop_order.delivery_completed_at + timedelta(hours=1)
    ):
        instruction.audio_file.delete(save=False)
        instruction.audio_content_type = ""
        instruction.save(update_fields=["audio_file", "audio_content_type"])
        return False
    return True


def _gst_shop_snapshot(shop_order):
    return SimpleNamespace(
        pk=shop_order.shop_id,
        name=shop_order.shop.name,
        gstin=shop_order.seller_gstin,
        gst_state_code=shop_order.seller_state_code,
        billing_address=shop_order.seller_billing_address,
        delivery_hsn_code=shop_order.delivery_hsn_code,
        delivery_gst_rate=shop_order.delivery_gst_rate,
    )


def _refresh_order_gst_snapshots(order, address, buyer_gstin_override=None):
    shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
    )
    if not shop_orders:
        return
    delivery_fees = {row.shop_id: row.delivery_fee for row in shop_orders}
    shop_by_id = {row.shop_id: _gst_shop_snapshot(row) for row in shop_orders}
    order_items = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order__order=order)
        .select_related("shop_order__shop", "product")
        .order_by("id")
    )
    items = [
        {
            "total": line.total,
            "shop": shop_by_id[line.shop_order.shop_id],
            "product": line.product,
            "gst_rate": line.gst_rate,
            "hsn_code": line.hsn_code,
        }
        for line in order_items
    ]
    has_gst_seller = any(row.seller_gstin for row in shop_orders)
    breakdown = _build_gst_breakdown(
        items,
        address,
        order.coupon_discount_amount,
        require_state=has_gst_seller,
        delivery_fees=delivery_fees,
        buyer_gstin_override=buyer_gstin_override,
    )
    for line, item in zip(order_items, items):
        line.taxable_amount = item["taxable_amount"]
        line.cgst_amount = item["cgst_amount"]
        line.sgst_amount = item["sgst_amount"]
        line.igst_amount = item["igst_amount"]
        line.save(update_fields=[
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
        ])
    for shop_order in shop_orders:
        row = breakdown["shops"][shop_order.shop_id]
        shop_order.buyer_state_code = row["buyer_state_code"]
        shop_order.buyer_gstin = row["buyer_gstin"]
        shop_order.taxable_amount = row["taxable_amount"]
        shop_order.cgst_amount = row["cgst_amount"]
        shop_order.sgst_amount = row["sgst_amount"]
        shop_order.igst_amount = row["igst_amount"]
        shop_order.delivery_gst_amount = row["delivery_gst_amount"]
        shop_order.save(update_fields=[
            "buyer_state_code",
            "buyer_gstin",
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "delivery_gst_amount",
        ])
    order.gst_amount = breakdown["total_gst"]
    order.save(update_fields=["gst_amount", "updated_at"])
    return breakdown


class PortalLoginView(LoginView):
    template_name = "registration/login.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_portal = (
            self.request.POST.get("login_as")
            or self.request.session.get("active_portal")
            or ("ADMIN" if self.request.user.is_superuser else "CUSTOMER")
        ).upper()
        if selected_portal not in PORTAL_LABELS:
            selected_portal = "CUSTOMER"
        context["selected_portal"] = selected_portal
        context["selected_portal_label"] = PORTAL_LABELS[selected_portal]
        return context

    def form_valid(self, form):
        _complete_due_staff_resignations()
        user = form.get_user()
        portal = (self.request.POST.get("login_as") or "CUSTOMER").upper()
        if portal not in {"CUSTOMER", "B2B_CUSTOMER", "SHOP_OWNER", "SHOP_STAFF", "RIDER", "ADMIN"}:
            form.add_error(None, "Choose one of the available workspaces.")
            return self.form_invalid(form)
        if portal == "RIDER":
            application = PublicRiderApplication.objects.filter(
                user=user,
                status=PublicRiderApplication.STATUS_APPROVED,
            ).first()
            if not application:
                if ShopStaff.objects.filter(
                    user=user,
                    is_active=True,
                    role="RIDER",
                    is_public_rider=False,
                ).exists():
                    form.add_error(
                        None,
                        "Shop riders must sign in using the Shop staff workspace.",
                    )
                else:
                    form.add_error(
                        None,
                        "Your public rider application must be approved before you can sign in here.",
                    )
                return self.form_invalid(form)
            if not user.is_active:
                form.add_error(None, "This account is inactive.")
                return self.form_invalid(form)
            self.request.session["active_portal"] = "RIDER"
            return super().form_valid(form)
        if portal == "ADMIN" and not user.is_superuser:
            form.add_error(None, "Administrator access is only available to administrators.")
            return self.form_invalid(form)
        if portal == "SHOP_STAFF" and not ShopStaff.objects.filter(
            user=user,
            is_active=True,
            is_public_rider=False,
        ).exists():
            form.add_error(None, "This account has no active shop staff assignment.")
            return self.form_invalid(form)
        self.request.session["active_portal"] = portal
        return super().form_valid(form)

    def get_success_url(self):
        redirect_url = super().get_success_url()
        if (
            self.request.user.is_superuser
            and self.request.session.get("active_portal") == "ADMIN"
        ):
            return redirect("admin_catalog_dashboard").url
        if self.request.session.get("active_portal") == "RIDER":
            return redirect("rider_dashboard").url
        if self.request.GET.get("next") or self.request.POST.get("next"):
            return redirect_url
        portal = self.request.session.get("active_portal", "CUSTOMER")
        staff_membership = ShopStaff.objects.filter(
            user=self.request.user,
            is_active=True,
        ).first()
        if portal == "SHOP_STAFF" and staff_membership:
            if staff_membership.role == "RIDER":
                return redirect("rider_dashboard").url
            return redirect("staff_dashboard").url
        if portal == "SHOP_OWNER" and Shop.objects.filter(owner=self.request.user).exists():
            return redirect("dashboard").url
        if portal == "SHOP_OWNER":
            return redirect("shop_add").url
        if ClientProfile.objects.filter(user=self.request.user).exists():
            return redirect("marketplace").url
        return redirect("signup").url


def logout_view(request):
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('login')


def _storefront_slug_from_host(request):
    host = request.get_host().partition(":")[0].lower().rstrip(".")
    base_domain = _storefront_base_domain(request)
    if not base_domain or host == base_domain or not host.endswith(f".{base_domain}"):
        return None
    slug = host[: -(len(base_domain) + 1)]
    if not slug or "." in slug:
        return None
    return slug if Shop.objects.filter(storefront_slug=slug).exists() else None


def _storefront_base_domain(request):
    configured_domain = settings.SHOP_STOREFRONT_BASE_DOMAIN
    if configured_domain:
        return configured_domain

    host = request.get_host().partition(":")[0].lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) > 2:
        possible_shop_slug, remaining_host = labels[0], ".".join(labels[1:])
        if Shop.objects.filter(storefront_slug=possible_shop_slug).exists():
            return remaining_host
    return host


def _storefront_uses_subdomain(request):
    domain = _storefront_base_domain(request)
    if not domain or domain == "localhost":
        return False
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return "." in domain
    return False


def _storefront_form_url_parts(request):
    scheme = (
        request.scheme
        if settings.DEBUG or not _storefront_uses_subdomain(request)
        else "https"
    )
    if _storefront_uses_subdomain(request):
        return {
            "storefront_url_prefix": f"{scheme}://",
            "storefront_url_suffix": f".{_storefront_base_domain(request)}/",
        }
    return {
        "storefront_url_prefix": f"{scheme}://{request.get_host()}/storefronts/",
        "storefront_url_suffix": "",
    }


def _storefront_main_site_url(request, path):
    scheme = (
        request.scheme
        if settings.DEBUG or not _storefront_uses_subdomain(request)
        else "https"
    )
    host = (
        _storefront_base_domain(request)
        if _storefront_uses_subdomain(request)
        else request.get_host()
    )
    return f"{scheme}://{host}{path}"


def _shop_storefront_url(request, shop):
    uses_subdomain = _storefront_uses_subdomain(request)
    scheme = "https" if uses_subdomain and not settings.DEBUG else request.scheme
    if uses_subdomain:
        return f"{scheme}://{shop.storefront_slug}.{_storefront_base_domain(request)}/"
    return f"{scheme}://{request.get_host()}/storefronts/{shop.storefront_slug}/"


def _storefront_is_active(shop):
    expires_at = shop.storefront_subscription_expires_at
    return shop.storefront_subscription_active and (
        expires_at is None or expires_at >= timezone.localdate()
    )


def _shop_storefront_context(request, shop):
    assignments = (
        ProductShop.objects.filter(
            shop=shop,
            is_active=True,
            product__is_active=True,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
        .select_related("product", "shop")
        .order_by("product__product_category", "product__product_name")
    )
    is_b2b = _is_b2b_customer(request) if request.user.is_authenticated else False
    assignments = [
        item for item in assignments
        if item.stock_for_customer(is_b2b=is_b2b) > 0
    ]
    cart = _address_scoped_cart(request)[0] if request.user.is_authenticated else {}
    for item in assignments:
        item.storefront_stock = item.stock_for_customer(is_b2b=is_b2b)
        item.storefront_retail_stock = item.stock
        item.storefront_b2b_stock = item.b2b_stock
        item.storefront_total_stock = item.stock + item.b2b_stock
        item.storefront_price = item.effective_price_for_customer(is_b2b=is_b2b)
        item.storefront_unit = item.unit_for_customer(is_b2b=is_b2b)
        item.storefront_bulk_price = item.effective_bulk_price_for_customer(is_b2b=is_b2b)
        item.storefront_bulk_unit = item.bulk_unit_for_customer(is_b2b=is_b2b)
        item.storefront_cart_quantity = _cart_quantity(cart, item.pk)
        item.storefront_bulk_cart_quantity = _cart_quantity(
            cart,
            item.pk,
            is_bulk_order=True,
        )
        item.storefront_has_bulk_offer = (
            item.storefront_bulk_price is not None and bool(item.storefront_bulk_unit)
        )

    settings_obj = ShopStorefrontSettings.objects.first()
    return {
        "shop": shop,
        "products": assignments,
        "cart_count": sum(
            max(0, int(quantity))
            for quantity in cart.values()
            if str(quantity).lstrip("-").isdigit()
        ),
        "app_deep_link": (
            f"sellerhelper://shop/{shop.storefront_slug}?url="
            f"{quote(_shop_storefront_url(request, shop), safe='')}"
        ),
        "android_app_download_url": (
            settings_obj.android_app_download_url if settings_obj else ""
        ),
        "ios_app_download_url": settings_obj.ios_app_download_url if settings_obj else "",
        "is_b2b_customer": is_b2b,
        "can_order": request.user.is_authenticated,
        "storefront_is_open": is_shop_currently_open(shop),
    }


def shop_storefront_slug_available(request):
    if request.method != "GET":
        return JsonResponse(
            {"available": False, "message": "Method not allowed."},
            status=405,
        )
    slug = request.GET.get("slug", "").strip().lower()
    owner_shop_id = request.GET.get("shop_id")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 63:
        return JsonResponse({"available": False, "message": "Enter a valid subdomain."})
    if slug in SHOP_STOREFRONT_RESERVED_SLUGS:
        return JsonResponse({"available": False, "message": "This subdomain is reserved."})
    matches = Shop.objects.filter(storefront_slug=slug)
    if owner_shop_id and request.user.is_authenticated:
        matches = matches.exclude(pk=owner_shop_id, owner=request.user)
    if matches.exists():
        return JsonResponse({"available": False, "message": "This subdomain is already in use."})
    return JsonResponse({"available": True})


def public_shop_storefront(request, slug):
    shop = get_object_or_404(Shop, storefront_slug=slug)
    if not _storefront_is_active(shop):
        return render(
            request,
            "seller/shop_storefront_unavailable.html",
            {"shop": shop},
            status=503,
        )
    context = _shop_storefront_context(request, shop)
    return render(request, "shop/storefront.html", context)


def home(request):
    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        return public_shop_storefront(request, storefront_slug)
    if request.user.is_authenticated:
        if request.session.get("active_portal") == "RIDER":
            return redirect("rider_dashboard")
        active_staff = ShopStaff.objects.filter(
            user=request.user,
            is_active=True,
            is_public_rider=False,
        ).select_related("shop").first()
        active_shops = Shop.objects.filter(owner=request.user)
    else:
        active_staff = None
        active_shops = Shop.objects.none()
    is_theme_preview = (
        request.GET.get("theme_preview") == "1"
        and request.user.is_authenticated
        and request.user.is_superuser
    )
    response = render(request, 'seller/home.html', {
        "active_staff": active_staff,
        "active_shops": active_shops,
        "active_portal": request.session.get("active_portal", "CUSTOMER"),
        "theme_preview": is_theme_preview,
    })
    if is_theme_preview:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def public_information_page(request, page_slug):
    branding = SiteBrandingSettings.objects.first()
    if page_slug == "contact":
        page = {
            "title": "Contact Us",
            "intro": "Reach the SellerHelper team or find the right place to get help with an order.",
            "slug": "contact",
            "sections": [],
        }
    else:
        page = LEGAL_PAGES.get(page_slug)
        if page is None:
            raise Http404("Information page not found.")
        page = {**page, "slug": page_slug}
    return render(
        request,
        "seller/public_information.html",
        {
            "page": page,
            "support_email": (
                branding.support_email if branding and branding.support_email
                else settings.SUPPORT_EMAIL
            ),
            "support_phone": (
                branding.support_phone if branding and branding.support_phone
                else settings.SUPPORT_PHONE
            ),
            "support_postal_address": (
                branding.support_address if branding and branding.support_address
                else settings.SUPPORT_POSTAL_ADDRESS
            ),
            "grievance_officer_name": settings.GRIEVANCE_OFFICER_NAME,
            "grievance_officer_email": settings.GRIEVANCE_OFFICER_EMAIL,
            "grievance_officer_phone": settings.GRIEVANCE_OFFICER_PHONE,
        },
    )


def _require_superuser(request):
    if not request.user.is_superuser:
        raise PermissionDenied


@login_required
def admin_panel_dashboard(request):
    _require_superuser(request)
    settings_obj = _delivery_sharing_settings()
    rider_settings_form = PublicRiderSettingsForm(
        request.POST or None,
        instance=settings_obj,
    )
    if request.method == "POST":
        if rider_settings_form.is_valid():
            rider_settings_form.save()
            messages.success(request, "Public rider settings saved.")
            return redirect("admin_panel_dashboard")
        messages.error(
            request,
            "Review the highlighted public rider settings and try again.",
        )
    return render(
        request,
        "seller/admin_panel.html",
        {"public_rider_settings_form": rider_settings_form},
    )


@login_required
def user_presence_heartbeat(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    _complete_due_staff_resignations()
    portal = request.session.get("active_portal", "CUSTOMER")
    if portal not in PORTAL_LABELS:
        portal = "CUSTOMER"
    UserPresence.objects.update_or_create(
        user=request.user,
        defaults={
            "last_seen": timezone.now(),
            "active_portal": portal,
        },
    )
    return JsonResponse({"success": True})


@login_required
def admin_user_presence_dashboard(request):
    _require_superuser(request)
    user_model = get_user_model()
    now = timezone.now()
    today = timezone.localdate()
    online_cutoff = now - timedelta(minutes=5)
    online_user_ids = set(
        UserPresence.objects.filter(
            user__is_active=True,
            last_seen__gte=online_cutoff,
        ).values_list("user_id", flat=True)
    )
    active_user_ids = set(
        user_model.objects.filter(is_active=True).values_list("pk", flat=True)
    )
    logged_in_today_ids = set(
        user_model.objects.filter(
            is_active=True,
            last_login__date=today,
        ).values_list("pk", flat=True)
    )
    login_events_today = UserActivity.objects.filter(
        event_type="AUTH_LOGIN",
        created_at__date=today,
        user__is_active=True,
    )

    role_user_ids = {
        "Administrator": set(
            user_model.objects.filter(is_active=True, is_superuser=True)
            .values_list("pk", flat=True)
        ),
        "Customer": set(
            ClientProfile.objects.filter(user__is_active=True)
            .values_list("user_id", flat=True)
        ),
        "Shop owner": set(
            Shop.objects.filter(owner__is_active=True)
            .values_list("owner_id", flat=True)
        ),
        "Public rider": set(
            PublicRiderApplication.objects.filter(
                user__is_active=True,
                status=PublicRiderApplication.STATUS_APPROVED,
            ).values_list("user_id", flat=True)
        ),
    }
    for role, label in ShopStaff.ROLE_CHOICES:
        role_user_ids[label] = set(
            ShopStaff.objects.filter(
                user__is_active=True,
                is_active=True,
                role=role,
            ).values_list("user_id", flat=True)
        )

    role_rows = [
        {
            "role": role,
            "total": len(user_ids & active_user_ids),
            "online": len(user_ids & online_user_ids),
            "logged_in_today": len(user_ids & logged_in_today_ids),
        }
        for role, user_ids in role_user_ids.items()
    ]

    search_term = request.GET.get("q", "").strip()[:100]
    users = user_model.objects.filter(is_active=True).filter(
        Q(presence__last_seen__gte=now - timedelta(days=1))
        | Q(last_login__date=today)
    )
    if search_term:
        users = users.filter(
            Q(username__icontains=search_term)
            | Q(first_name__icontains=search_term)
            | Q(last_name__icontains=search_term)
            | Q(email__icontains=search_term)
        )
    users = users.select_related(
        "presence",
        "public_rider_application",
    ).prefetch_related(
        "shop_staff_memberships__shop",
    ).order_by(
        "-presence__last_seen",
        "-last_login",
        "username",
    )
    page = Paginator(users, 50).get_page(request.GET.get("page"))
    page_user_ids = [user.pk for user in page.object_list]
    customer_user_ids = set(
        ClientProfile.objects.filter(user_id__in=page_user_ids)
        .values_list("user_id", flat=True)
    )
    shop_owner_user_ids = set(
        Shop.objects.filter(owner_id__in=page_user_ids)
        .values_list("owner_id", flat=True)
    )
    presence_rows = []
    for user in page.object_list:
        memberships = list(
            user.shop_staff_memberships.filter(is_active=True)
            .values_list("role", "shop__name")
        )
        role_labels = []
        if user.is_superuser:
            role_labels.append("Administrator")
        if user.pk in customer_user_ids:
            role_labels.append("Customer")
        if user.pk in shop_owner_user_ids:
            role_labels.append("Shop owner")
        role_labels.extend(
            f"{dict(ShopStaff.ROLE_CHOICES).get(role, role)} · {shop_name}"
            for role, shop_name in memberships
        )
        application = getattr(user, "public_rider_application", None)
        if (
            application
            and application.status == PublicRiderApplication.STATUS_APPROVED
        ):
            availability = "available" if application.is_online else "offline"
            role_labels.append(f"Public rider · {availability}")
        if not role_labels:
            role_labels.append("User")
        presence = getattr(user, "presence", None)
        is_online = bool(presence and presence.last_seen >= online_cutoff)
        presence_rows.append({
            "user": user,
            "roles": role_labels,
            "is_online": is_online,
            "last_seen": presence.last_seen if presence else None,
            "active_portal": (
                PORTAL_LABELS.get(presence.active_portal, presence.active_portal)
                if presence
                else ""
            ),
        })

    return render(
        request,
        "seller/admin_user_presence.html",
        {
            "role_rows": role_rows,
            "presence_rows": presence_rows,
            "page_obj": page,
            "search_term": search_term,
            "online_count": len(online_user_ids & active_user_ids),
            "active_account_count": len(active_user_ids),
            "logged_in_today_count": login_events_today.count(),
            "last_login_today_count": len(logged_in_today_ids),
            "online_window_minutes": 5,
            "refreshed_at": now,
        },
    )


@login_required
def admin_catalog_dashboard(request):
    _require_superuser(request)
    category_form = ProductCategoryForm()
    site_branding_settings, _ = SiteBrandingSettings.objects.get_or_create(pk=1)
    site_branding_form = SiteBrandingSettingsForm(instance=site_branding_settings)
    marketplace_layout_settings, _ = MarketplaceLayoutSettings.objects.get_or_create(pk=1)
    marketplace_layout_form = MarketplaceLayoutSettingsForm(
        instance=marketplace_layout_settings,
    )
    product_clone_settings, _ = ProductCloneSettings.objects.get_or_create(pk=1)
    product_clone_settings_form = ProductCloneSettingsForm(
        instance=product_clone_settings,
    )
    if request.method == "POST":
        if "save_site_branding" in request.POST:
            site_branding_form = SiteBrandingSettingsForm(
                request.POST,
                request.FILES,
                instance=site_branding_settings,
            )
            if site_branding_form.is_valid():
                site_branding_form.save()
                messages.success(request, "Site branding and contact details saved.")
                return redirect("admin_catalog_dashboard")
        elif "save_marketplace_layout" in request.POST:
            marketplace_layout_form = MarketplaceLayoutSettingsForm(
                request.POST,
                instance=marketplace_layout_settings,
            )
            if marketplace_layout_form.is_valid():
                marketplace_layout_form.save()
                messages.success(request, "Marketplace mobile layout settings saved.")
                return redirect("admin_catalog_dashboard")
        elif "save_product_clone_settings" in request.POST:
            product_clone_settings_form = ProductCloneSettingsForm(
                request.POST,
                instance=product_clone_settings,
            )
            if product_clone_settings_form.is_valid():
                product_clone_settings_form.save()
                messages.success(
                    request,
                    "Shop product copy settings saved.",
                )
                return redirect("admin_catalog_dashboard")
        else:
            category_form = ProductCategoryForm(request.POST)
            if category_form.is_valid():
                category_form.save()
                messages.success(request, "Product category added.")
                return redirect("admin_catalog_dashboard")

    search = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()
    selected_status = request.GET.get("status", "all").strip()
    products = AdminProductCatalog.objects.all()
    if selected_category:
        products = products.filter(product_category=selected_category)
    if selected_status == "active":
        products = products.filter(is_active=True)
    elif selected_status == "inactive":
        products = products.filter(is_active=False)
    else:
        selected_status = "all"
    if search:
        products = products.filter(
            Q(product_name__icontains=search)
            | Q(hsn_code__icontains=search)
        )
    products = products.order_by("product_category", "product_name")
    product_page = Paginator(products, 25).get_page(request.GET.get("page"))
    categories = ProductCategory.objects.all()
    for category in categories:
        category.product_count = AdminProductCatalog.objects.filter(
            product_category=category.name,
        ).count()
    shop_search = request.GET.get("shop_q", "").strip()
    shops = Shop.objects.select_related("owner").order_by("name")
    if shop_search:
        shops = shops.filter(
            Q(name__icontains=shop_search)
            | Q(owner__username__icontains=shop_search)
            | Q(owner__first_name__icontains=shop_search)
            | Q(owner__last_name__icontains=shop_search)
        )
    for shop in shops:
        shop.storefront_public_url = _shop_storefront_url(request, shop)
        shop.storefront_is_currently_active = _storefront_is_active(shop)

    return render(request, "seller/admin_catalog.html", {
        "category_form": category_form,
        "marketplace_layout_form": marketplace_layout_form,
        "product_clone_settings_form": product_clone_settings_form,
        "site_branding_form": site_branding_form,
        "offline_product_dictionary_ready": dictionary_is_ready(),
        "categories": categories,
        "products": product_page,
        "product_page": product_page,
        "product_count": AdminProductCatalog.objects.count(),
        "hsn_reference_count": HSNCodeDescription.objects.count(),
        "hsn_reference_preview": HSNCodeDescription.objects.all()[:12],
        "sac_reference_count": SACCodeDescription.objects.count(),
        "sac_reference_preview": SACCodeDescription.objects.all()[:12],
        "category_market_map": dict(
            ProductCategory.objects.values_list("name", "market_category")
        ),
        "active_product_count": AdminProductCatalog.objects.filter(is_active=True).count(),
        "active_category_count": ProductCategory.objects.filter(is_active=True).count(),
        "category_choices": get_product_category_choices(include_inactive=True),
        "selected_category": selected_category,
        "selected_status": selected_status,
        "search": search,
        "shops": shops,
        "shop_search": shop_search,
    })


@login_required
def admin_catalog_product_export(request):
    _require_superuser(request)
    category = request.GET.get("category", "").strip()
    products = AdminProductCatalog.objects.all()
    if category:
        products = products.filter(product_category=category)

    headers = (
        "Catalog ID",
        "Category",
        "Product name",
        "Image URL 1",
        "Image URL 2",
        "Image URL 3",
        "Image URL 4",
        "Image URL 5",
        "HSN code",
        "GST rate (%)",
        "Active",
        "SAC code",
    )
    rows = [headers]
    rows.extend(
        (
            product.pk,
            product.product_category,
            product.product_name,
            product.image_url,
            product.image_url_2,
            product.image_url_3,
            product.image_url_4,
            product.image_url_5,
            product.hsn_code,
            product.gst_rate,
            "Yes" if product.is_active else "No",
            product.sac_code,
        )
        for product in products.order_by("product_category", "product_name")
    )

    def column_name(index):
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    worksheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(row, start=1):
            cell_ref = f"{column_name(column_number)}{row_number}"
            if isinstance(value, Decimal):
                cells.append(
                    f'<c r="{cell_ref}"><v>{value:f}</v></c>'
                )
            else:
                text = escape(str(value))
                cells.append(
                    f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">'
                    f"{text}</t></is></c>"
                )
        worksheet_rows.append(
            f'<row r="{row_number}">{"".join(cells)}</row>'
        )

    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w", zipfile.ZIP_DEFLATED) as excel_file:
        excel_file.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        excel_file.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        excel_file.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Products" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        excel_file.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        excel_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(worksheet_rows)}</sheetData>'
            "</worksheet>",
        )

    filename = (
        f"product-catalog-{slugify(category) or 'category'}.xlsx"
        if category
        else "product-catalog-all.xlsx"
    )
    response = HttpResponse(
        workbook.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _catalog_excel_text(element, shared_strings, number_formats):
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cell_type = element.get("t")
    if cell_type == "inlineStr":
        inline = element.find(f"{namespace}is")
        return "".join(
            text.text or ""
            for text in inline.iter(f"{namespace}t")
        ) if inline is not None else ""
    value = element.findtext(f"{namespace}v", default="")
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError):
            raise ValueError("Workbook contains an invalid shared-string reference.")
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    try:
        style_index = int(element.get("s", "0"))
    except ValueError:
        style_index = 0
    if cell_type in {None, "n"} and value and style_index < len(number_formats):
        number_format = number_formats[style_index]
        if re.fullmatch(r"0{2,8}", number_format):
            try:
                numeric_value = Decimal(value)
                if numeric_value == numeric_value.to_integral_value():
                    return str(int(numeric_value)).zfill(len(number_format))
            except InvalidOperation:
                pass
    return value


def _read_catalog_excel(
    upload,
    *,
    required_fields=("product_category", "product_name"),
    max_rows=5_000,
    ignore_formulas=False,
):
    extension = upload.name.lower().rsplit(".", 1)[-1]
    if extension not in {"xlsx", "csv"}:
        raise ValueError("Choose an Excel .xlsx or .csv file.")
    if upload.size > 10 * 1024 * 1024:
        raise ValueError("The upload must be 10 MB or smaller.")

    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows = []
    if extension == "csv":
        try:
            content = upload.read().decode("utf-8-sig")
            first_line, separator, remaining_content = content.partition("\n")
            if first_line.strip().casefold() in {
                "sep=,",
                "sep=;",
                "sep=\t",
                "sep=|",
            }:
                content = remaining_content
            try:
                dialect = csv.Sniffer().sniff(
                    content[:8192],
                    delimiters=",;\t|",
                )
            except csv.Error:
                dialect = csv.excel
            for row_number, values in enumerate(
                csv.reader(io.StringIO(content, newline=""), dialect),
                start=1,
            ):
                if any(value.strip() for value in values):
                    rows.append((
                        row_number,
                        {
                            column_number: value
                            for column_number, value in enumerate(values, start=1)
                        },
                    ))
                if len(rows) > max_rows + 1:
                    raise ValueError(
                        f"Upload at most {max_rows:,} data rows at a time."
                    )
        except UnicodeDecodeError as error:
            raise ValueError("The CSV file must use UTF-8 encoding.") from error
        except csv.Error as error:
            raise ValueError("The CSV file could not be read.") from error
    else:
        try:
            with zipfile.ZipFile(upload) as excel_file:
                entries = excel_file.infolist()
                if sum(entry.file_size for entry in entries) > 50 * 1024 * 1024:
                    raise ValueError("The uncompressed Excel file is too large.")
                names = {entry.filename for entry in entries}
                workbook_path = "xl/workbook.xml"
                relations_path = "xl/_rels/workbook.xml.rels"
                if workbook_path not in names or relations_path not in names:
                    raise ValueError("This file is not a valid Excel workbook.")

                workbook_xml = excel_file.read(workbook_path)
                relations_xml = excel_file.read(relations_path)
                if b"<!DOCTYPE" in workbook_xml.upper() or b"<!ENTITY" in workbook_xml.upper():
                    raise ValueError("The workbook contains unsupported XML declarations.")
                if b"<!DOCTYPE" in relations_xml.upper() or b"<!ENTITY" in relations_xml.upper():
                    raise ValueError("The workbook contains unsupported XML declarations.")
                workbook_root = ElementTree.fromstring(workbook_xml)
                relations_root = ElementTree.fromstring(relations_xml)
                office_rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
                package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
                first_sheet = workbook_root.find(f"{main_ns}sheets/{main_ns}sheet")
                if first_sheet is None:
                    raise ValueError("The workbook has no worksheets.")
                relation_id = first_sheet.get(f"{office_rel_ns}id")
                relation = next(
                    (
                        item for item in relations_root.findall(
                            f"{package_rel_ns}Relationship",
                        )
                        if item.get("Id") == relation_id
                    ),
                    None,
                )
                if relation is None:
                    raise ValueError("The workbook's first worksheet could not be found.")
                target = relation.get("Target", "")
                sheet_path = (
                    posixpath.normpath(target.lstrip("/"))
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join("xl", target))
                )
                if (
                    not sheet_path.startswith("xl/worksheets/")
                    or sheet_path not in names
                ):
                    raise ValueError("The workbook's first worksheet could not be read.")

                shared_strings = []
                shared_path = "xl/sharedStrings.xml"
                if shared_path in names:
                    shared_xml = excel_file.read(shared_path)
                    if b"<!DOCTYPE" in shared_xml.upper() or b"<!ENTITY" in shared_xml.upper():
                        raise ValueError("The workbook contains unsupported XML declarations.")
                    shared_root = ElementTree.fromstring(shared_xml)
                    shared_strings = [
                        "".join(
                            text.text or ""
                            for text in item.iter(f"{main_ns}t")
                        )
                        for item in shared_root.findall(f"{main_ns}si")
                    ]

                number_formats = []
                styles_path = "xl/styles.xml"
                if styles_path in names:
                    styles_xml = excel_file.read(styles_path)
                    if b"<!DOCTYPE" in styles_xml.upper() or b"<!ENTITY" in styles_xml.upper():
                        raise ValueError("The workbook contains unsupported XML declarations.")
                    styles_root = ElementTree.fromstring(styles_xml)
                    custom_formats = {
                        int(item.get("numFmtId")): item.get("formatCode", "")
                        for item in styles_root.findall(
                            f"{main_ns}numFmts/{main_ns}numFmt",
                        )
                    }
                    builtin_formats = {
                        1: "0",
                        2: "0.00",
                        3: "#,##0",
                        4: "#,##0.00",
                    }
                    cell_formats = styles_root.find(f"{main_ns}cellXfs")
                    if cell_formats is not None:
                        for style in cell_formats.findall(f"{main_ns}xf"):
                            number_format_id = int(style.get("numFmtId", "0"))
                            number_formats.append(
                                custom_formats.get(
                                    number_format_id,
                                    builtin_formats.get(number_format_id, ""),
                                ),
                            )

                sheet_xml = excel_file.read(sheet_path)
                if b"<!DOCTYPE" in sheet_xml.upper() or b"<!ENTITY" in sheet_xml.upper():
                    raise ValueError("The workbook contains unsupported XML declarations.")
                sheet_root = ElementTree.fromstring(sheet_xml)

                for row_element in sheet_root.findall(f".//{main_ns}sheetData/{main_ns}row"):
                    row_number = int(row_element.get("r", len(rows) + 1))
                    values = {}
                    for cell in row_element.findall(f"{main_ns}c"):
                        if (
                            cell.find(f"{main_ns}f") is not None
                            and not ignore_formulas
                        ):
                            raise ValueError(
                                f"Formulas are not supported (row {row_number}). "
                                "Paste formula results as values before importing."
                            )
                        reference = cell.get("r", "")
                        column_letters = re.match(r"[A-Z]+", reference.upper())
                        if not column_letters:
                            continue
                        column_number = 0
                        for letter in column_letters.group(0):
                            column_number = column_number * 26 + ord(letter) - ord("A") + 1
                        if cell.find(f"{main_ns}f") is not None:
                            continue
                        values[column_number] = _catalog_excel_text(
                            cell,
                            shared_strings,
                            number_formats,
                        )
                    if values:
                        rows.append((row_number, values))
                        if len(rows) > max_rows + 1:
                            raise ValueError(
                                f"Upload at most {max_rows:,} data rows at a time."
                            )
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, OSError) as error:
            raise ValueError("The uploaded file is not a readable .xlsx workbook.") from error

    if not rows:
        raise ValueError("The workbook has no product rows.")

    headers = {
        "catalog id": "catalog_id",
        "category": "product_category",
        "product name": "product_name",
        "image url 1": "image_url",
        "image url 2": "image_url_2",
        "image url 3": "image_url_3",
        "image url 4": "image_url_4",
        "image url 5": "image_url_5",
        "hsn code": "hsn_code",
        "sac code": "sac_code",
        "gst rate (%)": "gst_rate",
        "active": "is_active",
        "hsn cd": "hsn_cd",
        "hsn description": "hsn_description",
        "sac cd": "sac_cd",
        "sac description": "sac_description",
    }
    if "hsn_cd" in required_fields:
        headers["hsn code"] = "hsn_cd"
        headers["hsn"] = "hsn_cd"
        headers["description"] = "hsn_description"
    if "sac_cd" in required_fields:
        headers["sac code"] = "sac_cd"
        headers["sac"] = "sac_cd"
        headers["description"] = "sac_description"
    header_row_number, header_values = rows[0]
    columns = {}
    unknown_headers = []
    for column_number, value in header_values.items():
        normalized = " ".join(
            value.strip().casefold().replace("_", " ").split()
        )
        mapped_field = headers.get(normalized)
        if mapped_field:
            columns[column_number] = mapped_field
        elif normalized:
            unknown_headers.append(value.strip())
    if unknown_headers:
        raise ValueError(
            "Unsupported column(s): " + ", ".join(unknown_headers) + "."
        )
    if not set(required_fields).issubset(columns.values()):
        required_labels = {
            "product_category": "Category",
            "product_name": "Product name",
            "hsn_cd": "HSN_CD",
            "hsn_description": "HSN_Description",
            "sac_cd": "SAC_CD",
            "sac_description": "SAC_Description",
        }
        required_columns = " and ".join(
            required_labels[field_name]
            for field_name in required_fields
        )
        raise ValueError(
            f"Row {header_row_number} must include {required_columns} columns."
        )
    return columns, rows[1:]


@login_required
def admin_catalog_product_import(request):
    _require_superuser(request)
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    upload = request.FILES.get("catalog_excel")
    if not upload:
        messages.error(request, "Choose an Excel .xlsx file to import.")
        return redirect("admin_catalog_dashboard")

    try:
        columns, rows = _read_catalog_excel(upload)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("admin_catalog_dashboard")

    allowed_fields = {
        "product_category",
        "product_name",
        "image_url",
        "image_url_2",
        "image_url_3",
        "image_url_4",
        "image_url_5",
        "hsn_code",
        "sac_code",
        "gst_rate",
        "is_active",
    }
    provided_fields = set(columns.values()) & allowed_fields
    validated = []
    errors = []
    seen_ids = set()
    seen_keys = set()
    for row_number, row_values in rows:
        if not any(value.strip() for value in row_values.values()):
            continue
        row = {
            field_name: row_values.get(column_number, "").strip()
            for column_number, field_name in columns.items()
        }
        raw_id = row.pop("catalog_id", "")
        instance = None
        if raw_id:
            try:
                product_id = int(raw_id)
            except ValueError:
                errors.append(f"Row {row_number}: Catalog ID must be a whole number.")
                continue
            if product_id in seen_ids:
                errors.append(f"Row {row_number}: Catalog ID {product_id} appears more than once.")
                continue
            seen_ids.add(product_id)
            instance = AdminProductCatalog.objects.filter(pk=product_id).first()
            if instance is None:
                errors.append(f"Row {row_number}: Catalog ID {product_id} does not exist.")
                continue
        category = row.get("product_category", "")
        product_name = row.get("product_name", "")
        if not category or not product_name:
            errors.append(
                f"Row {row_number}: Category and Product name are required."
            )
            continue
        product_key = (category.casefold(), product_name.casefold())
        if product_key in seen_keys:
            errors.append(
                f"Row {row_number}: This category and product name are duplicated in the file."
            )
            continue
        seen_keys.add(product_key)

        if instance is None:
            instance = AdminProductCatalog.objects.filter(
                product_category=category,
                product_name=product_name,
            ).first()
        form_data = {}
        for field_name in allowed_fields:
            if field_name in row:
                form_data[field_name] = row[field_name]
            elif instance is not None:
                form_data[field_name] = getattr(instance, field_name)
            elif field_name == "is_active":
                form_data[field_name] = "on"
        if "is_active" in provided_fields:
            active_value = row.get("is_active", "").strip().casefold()
            if active_value in {"yes", "true", "1", "active"}:
                form_data["is_active"] = "on"
            elif active_value in {"no", "false", "0", "inactive"}:
                form_data["is_active"] = ""
            else:
                errors.append(
                    f"Row {row_number}: Active must be Yes or No."
                )
                continue
        form = AdminProductCatalogForm(data=form_data, instance=instance)
        if not form.is_valid():
            detail = "; ".join(
                f"{field}: {', '.join(messages_list)}"
                for field, messages_list in form.errors.items()
            )
            errors.append(f"Row {row_number}: {detail}")
            continue
        validated.append((form, instance is None))

    if not validated and not errors:
        errors.append("The workbook contains no product rows to import.")
    if errors:
        shown_errors = errors[:10]
        if len(errors) > len(shown_errors):
            shown_errors.append(
                f"...and {len(errors) - len(shown_errors)} more row errors."
            )
        messages.error(
            request,
            "Import canceled; no products were changed. " + " ".join(shown_errors),
        )
        return redirect("admin_catalog_dashboard")

    created_count = 0
    updated_count = 0
    unchanged_count = 0
    with transaction.atomic():
        for form, is_new in validated:
            if not is_new and not form.has_changed():
                unchanged_count += 1
                continue
            _save_admin_catalog_product(form)
            if is_new:
                created_count += 1
            else:
                updated_count += 1
    messages.success(
        request,
        f"Excel import complete: {created_count} product(s) added and "
        f"{updated_count} product(s) updated; "
        f"{unchanged_count} unchanged.",
    )
    return redirect("admin_catalog_dashboard")


@login_required
def admin_catalog_hsn_reference_import(request):
    _require_superuser(request)
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    upload = request.FILES.get("hsn_reference_excel")
    if not upload:
        messages.error(
            request,
            "Choose an .xlsx or .csv file with HSN code and description columns.",
        )
        return redirect("admin_catalog_dashboard")

    try:
        columns, rows = _read_catalog_excel(
            upload,
            required_fields=("hsn_cd", "hsn_description"),
            max_rows=50_000,
            ignore_formulas=True,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("admin_catalog_dashboard")

    imported_rows = []
    errors = []
    seen_entries = set()
    for row_number, row_values in rows:
        values = {
            field_name: row_values.get(column_number, "").strip()
            for column_number, field_name in columns.items()
        }
        hsn_code = values.get("hsn_cd", "")
        description = values.get("hsn_description", "")
        if not hsn_code and not description:
            continue
        hsn_code = re.sub(r"\s+", "", hsn_code)
        if not re.fullmatch(r"\d{2}(?:\d{2}){0,3}", hsn_code):
            errors.append(
                f"Row {row_number}: HSN_CD must have 2, 4, 6, or 8 digits. "
                "Format the Excel column as Text to preserve leading zeroes."
            )
            continue
        if not description:
            errors.append(f"Row {row_number}: HSN_Description is required.")
            continue
        if len(description) > 500:
            errors.append(f"Row {row_number}: HSN_Description must be 500 characters or fewer.")
            continue
        entry = (hsn_code, description.casefold())
        if entry in seen_entries:
            continue
        seen_entries.add(entry)
        imported_rows.append(
            HSNCodeDescription(hsn_code=hsn_code, description=description),
        )

    if not imported_rows and not errors:
        errors.append("The workbook contains no HSN description rows.")
    if errors and not imported_rows:
        shown_errors = errors[:10]
        if len(errors) > len(shown_errors):
            shown_errors.append(
                f"...and {len(errors) - len(shown_errors)} more row errors."
            )
        messages.error(
            request,
            "HSN upload canceled; existing suggestions were not changed. "
            + " ".join(shown_errors),
        )
        return redirect("admin_catalog_dashboard")

    with transaction.atomic():
        HSNCodeDescription.objects.all().delete()
        HSNCodeDescription.objects.bulk_create(imported_rows, batch_size=500)
    messages.success(
        request,
        f"Imported {len(imported_rows)} HSN code description(s). "
        "The previous HSN suggestion list was replaced.",
    )
    if errors:
        shown_errors = errors[:10]
        if len(errors) > len(shown_errors):
            shown_errors.append(
                f"...and {len(errors) - len(shown_errors)} more rows were skipped."
            )
        messages.warning(
            request,
            f"Skipped {len(errors)} invalid HSN row(s): " + " ".join(shown_errors),
        )
    return redirect("admin_catalog_dashboard")


@login_required
def admin_catalog_hsn_suggestions(request):
    _require_superuser(request)
    query = " ".join(request.GET.get("q", "").split())
    if len(query) < 2:
        return JsonResponse({"results": []})

    terms = list(dict.fromkeys(
        token.casefold()
        for token in re.findall(r"\w+", query)
        if len(token) >= 2
    ))
    search_terms = set(terms)
    for term in terms:
        if len(term) > 3 and term.endswith("ies"):
            search_terms.add(term[:-3] + "y")
        if len(term) > 3 and term.endswith("es"):
            search_terms.add(term[:-2])
        elif len(term) > 3 and term.endswith("s"):
            search_terms.add(term[:-1])
        else:
            search_terms.add(term + "s")
    if not terms:
        return JsonResponse({"results": []})

    matching_descriptions = HSNCodeDescription.objects.none()
    for term in search_terms:
        matching_descriptions = matching_descriptions | (
            HSNCodeDescription.objects.filter(
                description__icontains=term,
            )
        )
    matches = list(matching_descriptions.distinct()[:200])
    matches.sort(
        key=lambda item: (
            -ratio(query.casefold(), item.description.casefold()),
            len(item.hsn_code),
            item.hsn_code,
        ),
    )
    return JsonResponse({
        "results": [
            {
                "code": item.hsn_code,
                "description": item.description,
            }
            for item in matches[:12]
        ],
    })


@login_required
def admin_site_theme_builder(request):
    _require_superuser(request)
    theme_settings, _ = SiteThemeSettings.objects.get_or_create(pk=1)
    form = SiteThemeSettingsForm(instance=theme_settings)
    if theme_settings.theme_revision_retention_days:
        SiteThemeRevision.objects.filter(
            theme_settings=theme_settings,
            created_at__lt=timezone.now() - timedelta(
                days=theme_settings.theme_revision_retention_days,
            ),
        ).delete()
    if request.method == "POST":
        restoring_revision = None
        if "delete_theme_revision" in request.POST:
            deleted_count, _ = SiteThemeRevision.objects.filter(
                theme_settings=theme_settings,
                pk=request.POST.get("delete_theme_revision"),
            ).delete()
            if deleted_count:
                messages.success(request, "Theme history record deleted.")
            else:
                messages.error(request, "That theme history record no longer exists.")
            return redirect("admin_site_theme_builder")
        if "restore_theme_revision" in request.POST:
            restoring_revision = get_object_or_404(
                SiteThemeRevision,
                pk=request.POST.get("restore_theme_revision"),
                theme_settings=theme_settings,
            )
            form_data = restoring_revision.snapshot
            if not isinstance(form_data, dict):
                form_data = {}
            defaults = SiteThemeSettings()
            form_data = {
                field_name: form_data.get(
                    field_name,
                    getattr(defaults, field_name),
                )
                for field_name in SiteThemeSettingsForm.Meta.fields
            }
            form = SiteThemeSettingsForm(
                form_data,
                instance=theme_settings,
            )
        else:
            form_data = request.POST.copy()
            if "theme_revision_retention_days" not in form_data:
                form_data["theme_revision_retention_days"] = str(
                    theme_settings.theme_revision_retention_days,
                )
            if "mobile_navigation_fields_present" not in form_data:
                form_data["mobile_topbar_scroll_hide_enabled"] = (
                    "on"
                    if theme_settings.mobile_topbar_scroll_hide_enabled
                    else ""
                )
                form_data["mobile_bottombar_scroll_hide_enabled"] = (
                    "on"
                    if theme_settings.mobile_bottombar_scroll_hide_enabled
                    else ""
                )
            if "mobile_app_desktop_view_fields_present" not in form_data:
                form_data["mobile_app_desktop_view_enabled"] = (
                    "on" if theme_settings.mobile_app_desktop_view_enabled else ""
                )
            homepage_fields = (
                "homepage_hero_badge",
                "homepage_hero_title",
                "homepage_hero_text",
                "homepage_primary_cta",
                "homepage_secondary_cta",
                "homepage_feature_1_title",
                "homepage_feature_1_text",
                "homepage_feature_2_title",
                "homepage_feature_2_text",
                "homepage_feature_3_title",
                "homepage_feature_3_text",
                "homepage_section_order",
            )
            for field_name in homepage_fields:
                if field_name not in form_data:
                    form_data[field_name] = getattr(theme_settings, field_name)
            if "homepage_content_fields_present" not in form_data:
                form_data["homepage_hero_enabled"] = (
                    "on" if theme_settings.homepage_hero_enabled else ""
                )
                form_data["homepage_features_enabled"] = (
                    "on" if theme_settings.homepage_features_enabled else ""
                )
            marketplace_fields = [
                field_name
                for field_name in SiteThemeSettingsForm.Meta.fields
                if field_name.startswith("marketplace_")
            ]
            if "marketplace_content_fields_present" not in form_data:
                for field_name in marketplace_fields:
                    if field_name not in form_data:
                        value = getattr(theme_settings, field_name)
                        if field_name == "marketplace_featured_product_ids":
                            form_data.setlist(field_name, [
                                str(product_id) for product_id in (value or [])
                            ])
                        else:
                            form_data[field_name] = value
                for field_name in (
                    "marketplace_hero_enabled",
                    "marketplace_nearby_shops_enabled",
                    "marketplace_category_nav_enabled",
                    "marketplace_products_enabled",
                ):
                    form_data[field_name] = (
                        "on" if getattr(theme_settings, field_name) else ""
                    )
            form = SiteThemeSettingsForm(
                form_data,
                instance=theme_settings,
            )

        if form.is_valid():
            with transaction.atomic():
                saved_theme = form.save()
                snapshot = {
                    field_name: getattr(saved_theme, field_name)
                    for field_name in SiteThemeSettingsForm.Meta.fields
                }
                SiteThemeRevision.objects.create(
                    theme_settings=saved_theme,
                    snapshot=snapshot,
                    action="RESTORE" if restoring_revision else "SAVE",
                    created_by=request.user,
                    restored_from=restoring_revision,
                )
                if saved_theme.theme_revision_retention_days:
                    SiteThemeRevision.objects.filter(
                        theme_settings=saved_theme,
                        created_at__lt=timezone.now() - timedelta(
                            days=saved_theme.theme_revision_retention_days,
                        ),
                    ).delete()
            if restoring_revision:
                messages.success(request, "Selected theme revision restored.")
            else:
                messages.success(request, "Site-wide theme settings saved.")
            return redirect("admin_site_theme_builder")
    return render(request, "seller/admin_site_theme_builder.html", {
        "form": form,
        "marketplace_category_choices": form.marketplace_category_choices,
        "theme_revisions": SiteThemeRevision.objects.filter(
            theme_settings=theme_settings,
        ).select_related("created_by", "restored_from")[:50],
    })


@login_required
def admin_catalog_sac_reference_import(request):
    _require_superuser(request)
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    upload = request.FILES.get("sac_reference_excel")
    if not upload:
        messages.error(
            request,
            "Choose an .xlsx or .csv file with SAC_CD and SAC_Description columns.",
        )
        return redirect("admin_catalog_dashboard")

    try:
        columns, rows = _read_catalog_excel(
            upload,
            required_fields=("sac_cd", "sac_description"),
            max_rows=50_000,
            ignore_formulas=True,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("admin_catalog_dashboard")

    imported_rows = []
    errors = []
    seen_entries = set()
    for row_number, row_values in rows:
        values = {
            field_name: row_values.get(column_number, "").strip()
            for column_number, field_name in columns.items()
        }
        sac_code = re.sub(r"\s+", "", values.get("sac_cd", ""))
        description = values.get("sac_description", "")
        if not sac_code and not description:
            continue
        if not re.fullmatch(r"\d{2}(?:\d{2}){0,2}", sac_code):
            errors.append(
                f"Row {row_number}: SAC_CD must have 2, 4, or 6 digits."
            )
            continue
        if not description:
            errors.append(f"Row {row_number}: SAC_Description is required.")
            continue
        if len(description) > 500:
            errors.append(
                f"Row {row_number}: SAC_Description must be 500 characters or fewer."
            )
            continue
        entry = (sac_code, description.casefold())
        if entry in seen_entries:
            continue
        seen_entries.add(entry)
        imported_rows.append(
            SACCodeDescription(sac_code=sac_code, description=description),
        )

    if not imported_rows and not errors:
        errors.append("The workbook contains no SAC description rows.")
    if errors and not imported_rows:
        shown_errors = errors[:10]
        if len(errors) > len(shown_errors):
            shown_errors.append(
                f"...and {len(errors) - len(shown_errors)} more row errors."
            )
        messages.error(
            request,
            "SAC upload canceled; existing suggestions were not changed. "
            + " ".join(shown_errors),
        )
        return redirect("admin_catalog_dashboard")

    with transaction.atomic():
        SACCodeDescription.objects.all().delete()
        SACCodeDescription.objects.bulk_create(imported_rows, batch_size=500)
    messages.success(
        request,
        f"Imported {len(imported_rows)} SAC code description(s). "
        "The previous SAC suggestion list was replaced.",
    )
    if errors:
        shown_errors = errors[:10]
        if len(errors) > len(shown_errors):
            shown_errors.append(
                f"...and {len(errors) - len(shown_errors)} more rows were skipped."
            )
        messages.warning(
            request,
            f"Skipped {len(errors)} invalid SAC row(s): " + " ".join(shown_errors),
        )
    return redirect("admin_catalog_dashboard")


@login_required
def admin_catalog_sac_suggestions(request):
    _require_superuser(request)
    category = ProductCategory.objects.filter(
        name=request.GET.get("category", ""),
        market_category="SERVICE MARKET",
        is_active=True,
    ).first()
    if category is None:
        return JsonResponse({"results": []})

    query = " ".join(request.GET.get("q", "").split())
    if len(query) < 2:
        return JsonResponse({"results": []})

    terms = list(dict.fromkeys(
        token.casefold()
        for token in re.findall(r"\w+", query)
        if len(token) >= 2
    ))
    search_terms = set(terms)
    for term in terms:
        if len(term) > 3 and term.endswith("ies"):
            search_terms.add(term[:-3] + "y")
        if len(term) > 3 and term.endswith("es"):
            search_terms.add(term[:-2])
        elif len(term) > 3 and term.endswith("s"):
            search_terms.add(term[:-1])
        else:
            search_terms.add(term + "s")
    if not terms:
        return JsonResponse({"results": []})

    matching_descriptions = SACCodeDescription.objects.none()
    for term in search_terms:
        matching_descriptions = matching_descriptions | (
            SACCodeDescription.objects.filter(description__icontains=term)
        )
    matches = list(matching_descriptions.distinct()[:200])
    matches.sort(
        key=lambda item: (
            -ratio(query.casefold(), item.description.casefold()),
            len(item.sac_code),
            item.sac_code,
        ),
    )
    return JsonResponse({
        "results": [
            {
                "code": item.sac_code,
                "description": item.description,
            }
            for item in matches[:12]
        ],
    })


@login_required
def admin_catalog_shop_storefront_toggle(request, pk):
    _require_superuser(request)
    if request.method != "POST":
        return redirect("admin_catalog_dashboard")
    shop = get_object_or_404(Shop, pk=pk)
    enable = not _storefront_is_active(shop)
    shop.storefront_subscription_active = enable
    if enable:
        shop.storefront_subscription_expires_at = None
    shop.save(update_fields=[
        "storefront_subscription_active",
        "storefront_subscription_expires_at",
        "updated_at",
    ])
    state = "activated" if enable else "deactivated"
    messages.success(request, f"Storefront subscription {state} for {shop.name}.")
    return redirect("admin_catalog_dashboard")


@login_required
def admin_catalog_product_create(request):
    _require_superuser(request)
    form = AdminProductCatalogForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Shared catalog product saved.")
        return redirect(_admin_catalog_return_url(request))
    return render(request, "seller/admin_catalog_product_form.html", {
        "form": form,
        "form_title": "Add catalog product",
        "return_to": _admin_catalog_return_url(request),
        "category_market_map": dict(
            ProductCategory.objects.values_list("name", "market_category")
        ),
    })


def _admin_catalog_return_url(request):
    target = request.POST.get("return_to", "")
    if not target or not url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return reverse("admin_catalog_dashboard")
    return urlsplit(target)._replace(fragment="catalog-products").geturl()


def _save_admin_catalog_product(form):
    previous_category = form.initial.get(
        "product_category",
        form.instance.product_category,
    )
    previous_name = form.initial.get(
        "product_name",
        form.instance.product_name,
    )
    product_name_changed = (
        form.instance.pk is not None
        and "product_name" in form.changed_data
    )
    catalog_product = form.save()
    if product_name_changed:
        shop_products = Product.objects.filter(
            product_category=previous_category,
            product_name=previous_name,
        )
        for shop_product in shop_products.iterator():
            shop_product.product_name = catalog_product.product_name
            shop_product.save(update_fields=["product_name", "updated_at"])
    return catalog_product


@login_required
def admin_catalog_product_edit(request, pk):
    _require_superuser(request)
    product = get_object_or_404(AdminProductCatalog, pk=pk)
    form = AdminProductCatalogForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            _save_admin_catalog_product(form)
        messages.success(request, "Shared catalog product updated.")
        return redirect(_admin_catalog_return_url(request))
    return render(request, "seller/admin_catalog_product_form.html", {
        "form": form,
        "form_title": f"Edit {product.product_name}",
        "product": product,
        "return_to": _admin_catalog_return_url(request),
        "category_market_map": dict(
            ProductCategory.objects.values_list("name", "market_category")
        ),
    })


@login_required
def admin_catalog_products_save_all(request):
    _require_superuser(request)
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    selected_product_id = request.POST.get("save_product")
    if selected_product_id is not None:
        if not selected_product_id.isdigit():
            messages.error(request, "Invalid catalog product selected.")
            return redirect(_admin_catalog_return_url(request))
        product_ids = [int(selected_product_id)]
    else:
        product_ids = sorted({
            int(match.group(1))
            for key in request.POST
            if (match := re.fullmatch(r"product-(\d+)-product_name", key))
        })
    if not product_ids:
        messages.info(request, "No catalog products were submitted.")
        return redirect(_admin_catalog_return_url(request))

    products_by_id = AdminProductCatalog.objects.in_bulk(product_ids)
    if len(products_by_id) != len(product_ids):
        messages.error(request, "A catalog product no longer exists. No changes were saved.")
        return redirect(_admin_catalog_return_url(request))

    forms = [
        AdminProductCatalogForm(
            request.POST,
            instance=products_by_id[product_id],
            prefix=f"product-{product_id}",
        )
        for product_id in product_ids
    ]
    invalid_products = [
        form.instance.product_name
        for form in forms
        if not form.is_valid()
    ]
    if invalid_products:
        messages.error(
            request,
            "No products were saved. Correct invalid fields for: "
            + ", ".join(invalid_products[:10])
            + (" ..." if len(invalid_products) > 10 else ""),
        )
        return redirect(_admin_catalog_return_url(request))

    updated_count = 0
    with transaction.atomic():
        for form in forms:
            if form.has_changed():
                _save_admin_catalog_product(form)
                updated_count += 1
    messages.success(
        request,
        (
            "Catalog product saved."
            if selected_product_id is not None and updated_count
            else (
                "Catalog product has no changes."
                if selected_product_id is not None
                else f"Saved changes to {updated_count} catalog product(s)."
            )
        ),
    )
    return redirect(_admin_catalog_return_url(request))


@login_required
def admin_catalog_category_toggle(request, pk):
    _require_superuser(request)
    if request.method != "POST":
        return redirect("admin_catalog_dashboard")
    category = get_object_or_404(ProductCategory, pk=pk)
    category.is_active = not category.is_active
    category.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"Category {category.name} {'enabled' if category.is_active else 'disabled'}.",
    )
    return redirect("admin_catalog_dashboard")


@login_required
@transaction.atomic
def admin_catalog_category_edit(request, pk):
    _require_superuser(request)
    category = get_object_or_404(ProductCategory.objects.select_for_update(), pk=pk)
    old_name = category.name
    form = ProductCategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        new_name = form.cleaned_data["name"]
        duplicate_names = AdminProductCatalog.objects.filter(
            product_category=new_name,
        ).values_list("product_name", flat=True)
        if old_name != new_name and AdminProductCatalog.objects.filter(
            product_category=old_name,
            product_name__in=duplicate_names,
        ).exists():
            form.add_error(
                "name",
                "This category already has catalog products with matching names. "
                "Resolve those duplicates before renaming.",
            )
        else:
            updated_category = form.save()
            if old_name != updated_category.name:
                AdminProductCatalog.objects.filter(
                    product_category=old_name,
                ).update(product_category=updated_category.name)
                Product.objects.filter(
                    product_category=old_name,
                ).update(product_category=updated_category.name)
            messages.success(request, "Product category updated.")
            return redirect("admin_catalog_dashboard")
    return render(request, "seller/admin_catalog_category_form.html", {
        "form": form,
        "category": category,
    })


@login_required
def admin_catalog_shop_image_permission(request, pk):
    _require_superuser(request)
    if request.method != "POST":
        return redirect("admin_catalog_dashboard")
    shop = get_object_or_404(Shop, pk=pk)
    shop.allow_product_image_overrides = request.POST.get("enabled") == "1"
    shop.save(update_fields=["allow_product_image_overrides"])
    state = "enabled" if shop.allow_product_image_overrides else "disabled"
    messages.success(
        request,
        f"Shop-specific catalog image overrides {state} for {shop.name}.",
    )
    return redirect("admin_catalog_dashboard")


@login_required
def admin_catalog_shop_group_permission(request, pk):
    _require_superuser(request)
    if request.method != "POST":
        return redirect("admin_catalog_dashboard")
    shop = get_object_or_404(Shop, pk=pk)
    shop.allow_owner_create_shared_rider_groups = request.POST.get("enabled") == "1"
    shop.save(update_fields=["allow_owner_create_shared_rider_groups"])
    state = "enabled" if shop.allow_owner_create_shared_rider_groups else "disabled"
    messages.success(
        request,
        f"Shared-rider group creation {state} for {shop.name}.",
    )
    return redirect("admin_catalog_dashboard")


@login_required
@transaction.atomic
def shared_rider_groups(request):
    owned_groups = SharedRiderGroup.objects.filter(
        owner=request.user,
    ).select_related("created_from_shop").prefetch_related(
        "shop_memberships__shop",
    )
    pending_requests = SharedRiderGroupShop.objects.filter(
        shop__owner=request.user,
        status=SharedRiderGroupShop.STATUS_PENDING,
    ).select_related("group", "shop", "requested_by").order_by(
        "requested_at",
    )
    shop_group_memberships = SharedRiderGroupShop.objects.filter(
        shop__owner=request.user,
    ).select_related("group", "shop").order_by("group__name", "shop__name")
    authorized_shops = Shop.objects.filter(
        owner=request.user,
        allow_owner_create_shared_rider_groups=True,
    ).order_by("name")
    can_create_group = authorized_shops.exists()
    has_group_access = (
        can_create_group
        or owned_groups.exists()
        or pending_requests.exists()
        or shop_group_memberships.exists()
    )
    if not has_group_access:
        raise PermissionDenied

    post_action = request.POST.get("action", "")
    create_form = SharedRiderGroupCreateForm(
        request.POST if post_action == "create" else None,
        owner=request.user,
    )
    if request.method == "POST" and post_action == "create":
        if not can_create_group:
            raise PermissionDenied
        if create_form.is_valid():
            group = create_form.save(commit=False)
            group.owner = request.user
            group.save()
            SharedRiderGroupShop.objects.create(
                group=group,
                shop=group.created_from_shop,
                status=SharedRiderGroupShop.STATUS_APPROVED,
                requested_by=request.user,
                reviewed_by=request.user,
                reviewed_at=timezone.now(),
            )
            messages.success(request, f"Rider-sharing group “{group.name}” created.")
            return redirect("shared_rider_groups")

    add_shop_form = SharedRiderShopRequestForm(
        request.POST if post_action == "add-shop" else None,
    )
    if request.method == "POST" and post_action == "add-shop":
        if add_shop_form.is_valid():
            try:
                group_id = int(request.POST.get("group_id", ""))
            except (TypeError, ValueError):
                messages.error(request, "Select a valid rider-sharing group.")
                return redirect("shared_rider_groups")
            group = get_object_or_404(
                SharedRiderGroup.objects.select_for_update(),
                pk=group_id,
                owner=request.user,
            )
            shop_id = add_shop_form.cleaned_data["shop_id"]
            target_shop = get_object_or_404(Shop, pk=shop_id)
            membership, created = SharedRiderGroupShop.objects.get_or_create(
                group=group,
                shop=target_shop,
                defaults={
                    "status": (
                        SharedRiderGroupShop.STATUS_APPROVED
                        if target_shop.owner_id == request.user.pk
                        else SharedRiderGroupShop.STATUS_PENDING
                    ),
                    "requested_by": request.user,
                    "reviewed_by": (
                        request.user
                        if target_shop.owner_id == request.user.pk
                        else None
                    ),
                    "reviewed_at": (
                        timezone.now()
                        if target_shop.owner_id == request.user.pk
                        else None
                    ),
                },
            )
            if not created and membership.status == SharedRiderGroupShop.STATUS_REJECTED:
                membership.status = (
                    SharedRiderGroupShop.STATUS_APPROVED
                    if target_shop.owner_id == request.user.pk
                    else SharedRiderGroupShop.STATUS_PENDING
                )
                membership.requested_by = request.user
                membership.requested_at = timezone.now()
                membership.reviewed_by = (
                    request.user
                    if target_shop.owner_id == request.user.pk
                    else None
                )
                membership.reviewed_at = (
                    timezone.now()
                    if target_shop.owner_id == request.user.pk
                    else None
                )
                membership.save(update_fields=[
                    "status",
                    "requested_by",
                    "requested_at",
                    "reviewed_by",
                    "reviewed_at",
                ])
                created = True
            if created:
                if target_shop.owner_id == request.user.pk:
                    messages.success(request, f"{target_shop.name} was added to the group.")
                else:
                    messages.success(
                        request,
                        f"An approval request was sent to {target_shop.name}'s owner.",
                    )
            else:
                messages.info(request, "This shop is already in the group or awaiting approval.")
            return redirect("shared_rider_groups")

    if request.method == "POST" and post_action == "remove-shop":
        membership = get_object_or_404(
            SharedRiderGroupShop.objects.select_for_update().select_related(
                "shop",
                "group",
            ),
            pk=request.POST.get("membership_id"),
            group__owner=request.user,
        )
        group_name = membership.group.name
        shop_name = membership.shop.name
        membership.delete()
        messages.success(request, f"{shop_name} was removed from {group_name}.")
        return redirect("shared_rider_groups")

    return render(request, "seller/shared_rider_groups.html", {
        "groups": owned_groups,
        "pending_requests": pending_requests,
        "shop_group_memberships": shop_group_memberships,
        "can_create_group": can_create_group,
        "create_form": create_form,
        "add_shop_form": add_shop_form,
    })


@login_required
@transaction.atomic
def exit_shared_rider_group(request, membership_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    membership = get_object_or_404(
        SharedRiderGroupShop.objects.select_for_update().select_related(
            "shop",
            "group",
        ),
        pk=membership_id,
        shop__owner=request.user,
    )
    group_name = membership.group.name
    shop_name = membership.shop.name
    membership.delete()
    messages.success(request, f"{shop_name} has exited {group_name}.")
    return redirect("dashboard")


@login_required
@transaction.atomic
def review_shared_rider_group_shop(request, membership_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    membership = get_object_or_404(
        SharedRiderGroupShop.objects.select_for_update().select_related(
            "shop",
            "group",
        ),
        pk=membership_id,
    )
    if membership.shop.owner_id != request.user.pk:
        raise PermissionDenied
    if membership.status != SharedRiderGroupShop.STATUS_PENDING:
        messages.info(request, "This group request has already been reviewed.")
        return redirect("shared_rider_groups")
    action = request.POST.get("action")
    if action not in {"approve", "reject"}:
        messages.error(request, "Choose approve or reject for this request.")
        return redirect("shared_rider_groups")
    membership.status = (
        SharedRiderGroupShop.STATUS_APPROVED
        if action == "approve"
        else SharedRiderGroupShop.STATUS_REJECTED
    )
    membership.reviewed_by = request.user
    membership.reviewed_at = timezone.now()
    membership.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    messages.success(
        request,
        f"{membership.shop.name} {membership.status.lower()} the rider-sharing request.",
    )
    return redirect("shared_rider_groups")


@login_required
def admin_catalog_product_toggle(request, pk):
    _require_superuser(request)
    if request.method != "POST":
        return redirect("admin_catalog_dashboard")
    product = get_object_or_404(AdminProductCatalog, pk=pk)
    product.is_active = not product.is_active
    product.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"Catalog product {product.product_name} {'enabled' if product.is_active else 'disabled'}.",
    )
    return redirect("admin_catalog_dashboard")


@login_required
def dashboard(request):
    if request.session.get("active_portal") == "B2B_CUSTOMER":
        return redirect("marketplace")
    if request.session.get("active_portal") == "RIDER":
        return redirect("rider_dashboard")

    staff_membership = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        is_public_rider=False,
    ).select_related("shop").first()

    active_portal = request.session.get("active_portal")
    if active_portal == "SHOP_STAFF" or (
        active_portal is None and staff_membership
    ):
        if not staff_membership:
            messages.error(request, "You are not assigned to an active shop staff role.")
            return redirect("home")
        if staff_membership.role == "RIDER":
            return redirect("rider_dashboard")
        return redirect("staff_dashboard")

    shops = Shop.objects.filter(
        owner=request.user
    )

    products = (
        Product.objects
        .filter(
            shop_assignments__shop__owner=request.user
        )
        .distinct()
        .order_by("-created_at")
    )

    shop_orders = (
        ShopOrder.objects
        .filter(shop__in=shops)
        .select_related("shop", "order")
        .order_by("-created_at")
    )
    orders_count = shop_orders.count()
    orders_pending = shop_orders.filter(
        status__in=["PLACED", "CONFIRMED", "PACKING", "READY"],
    ).count()
    delivered_orders = shop_orders.filter(status="DELIVERED").count()
    low_stock_products = (
        ProductShop.objects
        .filter(
            shop__in=shops,
            is_active=True,
            product__is_active=True,
            stock__lte=5,
        )
        .select_related("shop", "product")
        .order_by("stock", "product__product_name")[:8]
    )
    wallet_balance = ShopWallet.objects.filter(
        shop__in=shops,
    ).aggregate(total=Sum("balance"))["total"] or Decimal("0.00")
    context = {

        "shops": shops,

        "products": products,

        "orders_count": orders_count,

        "orders_pending": orders_pending,
        "orders_delivered": delivered_orders,
        "recent_shop_orders": shop_orders[:8],
        "low_stock_products": low_stock_products,
        "low_stock_count": ProductShop.objects.filter(
            shop__in=shops,
            is_active=True,
            product__is_active=True,
            stock__lte=5,
        ).count(),
        "wallet_balance": wallet_balance,
        "can_manage_shared_rider_groups": (
            Shop.objects.filter(
                owner=request.user,
                allow_owner_create_shared_rider_groups=True,
            ).exists()
            or SharedRiderGroup.objects.filter(owner=request.user).exists()
            or SharedRiderGroupShop.objects.filter(
                shop__owner=request.user,
            ).exists()
        ),
    }

    return render(
        request,
        "seller/dashboard.html",
        context
    )


def _shop_customer_rows(owner):
    owned_shop_ids = list(
        Shop.objects.filter(owner=owner).values_list("id", flat=True)
    )
    shop_orders = (
        ShopOrder.objects.filter(
            shop_id__in=owned_shop_ids,
            order__is_valid_order=True,
        )
        .exclude(status__iexact="CANCELLED")
        .exclude(status__iexact="CANCELED")
        .select_related("shop", "order")
        .prefetch_related("order_items")
        .order_by("-created_at", "-id")
    )
    customer_references = {
        shop_order.order.user.strip()
        for shop_order in shop_orders
        if shop_order.order.user.strip()
    }
    numeric_references = {
        int(reference)
        for reference in customer_references
        if reference.isdecimal()
    }
    profiles = ClientProfile.objects.filter(
        Q(user__username__in=customer_references)
        | Q(user_id__in=numeric_references)
    ).select_related("user")
    profiles_by_reference = {}
    for profile in profiles:
        profiles_by_reference[profile.user.username.casefold()] = profile
        profiles_by_reference[str(profile.user_id)] = profile

    customers = {}
    today = timezone.localdate()
    for shop_order in shop_orders:
        order = shop_order.order
        reference = order.user.strip()
        profile = profiles_by_reference.get(reference.casefold())
        if profile is None:
            profile = profiles_by_reference.get(reference)

        mobile = (
            profile.contact_no.strip()
            if profile and profile.contact_no.strip()
            else order.delivery_contact_no.strip()
        )
        identity = (
            f"user:{profile.user_id}"
            if profile
            else f"mobile:{''.join(character for character in mobile if character.isalnum()).casefold()}"
            if mobile
            else f"pos-order:{order.pk}"
            if order.order_source == Order.SOURCE_POS
            else f"account:{reference.casefold()}"
        )
        customer = customers.get(identity)
        if customer is None:
            date_of_birth = profile.date_of_birth if profile else None
            age = (
                today.year
                - date_of_birth.year
                - ((today.month, today.day) < (date_of_birth.month, date_of_birth.day))
                if date_of_birth
                else ""
            )
            customer = {
                "name": (
                    order.customer_name
                    or (
                        profile.name.strip()
                        if profile and profile.name.strip()
                        else reference
                    )
                ),
                "mobile": mobile,
                "email": (
                    profile.email.strip()
                    if profile and profile.email.strip()
                    else profile.user.email.strip()
                    if profile
                    else ""
                ),
                "age": age,
                "gender": profile.gender if profile else "",
                "shops": set(),
                "products": set(),
                "order_count": 0,
                "total_spent": Decimal("0.00"),
                "last_purchase": None,
            }
            customers[identity] = customer

        customer["shops"].add(shop_order.shop.name)
        customer["order_count"] += 1
        customer["total_spent"] += shop_order.total_amount
        purchase_time = order.order_time or shop_order.created_at
        if customer["last_purchase"] is None or purchase_time > customer["last_purchase"]:
            customer["last_purchase"] = purchase_time
        order_products = {
            item.product_name
            for item in shop_order.order_items.all()
            if item.product_name
        }
        if not order_products and isinstance(shop_order.items, list):
            order_products.update(
                item.get("name", "")
                for item in shop_order.items
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and item["name"]
                )
            )
        customer["products"].update(order_products)

    return [
        {
            **customer,
            "shops": sorted(customer["shops"], key=str.casefold),
            "products": sorted(customer["products"], key=str.casefold),
            "shop_names": ", ".join(sorted(customer["shops"], key=str.casefold)),
            "product_names": ", ".join(
                sorted(customer["products"], key=str.casefold)
            ),
        }
        for customer in sorted(
            customers.values(),
            key=lambda row: row["name"].casefold(),
        )
    ]


@login_required
def shop_customers(request):
    if not Shop.objects.filter(owner=request.user).exists():
        raise Http404("No shop customer data is available for this account.")

    customer_rows = _shop_customer_rows(request.user)
    page = Paginator(customer_rows, 50).get_page(request.GET.get("page"))
    total_orders = sum(customer["order_count"] for customer in customer_rows)
    total_spent = sum(
        (customer["total_spent"] for customer in customer_rows),
        Decimal("0.00"),
    )
    return render(
        request,
        "seller/shop_customers.html",
        {
            "page": page,
            "customer_count": len(customer_rows),
            "total_orders": total_orders,
            "total_spent": total_spent,
        },
    )


@login_required
def shop_customers_export(request):
    if not Shop.objects.filter(owner=request.user).exists():
        raise Http404("No shop customer data is available for this account.")

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="shop-customers-{timezone.localdate().isoformat()}.csv"'
    )
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([
        "Customer name",
        "Mobile",
        "Email",
        "Age",
        "Gender",
        "Shops purchased from",
        "Products purchased",
        "Orders",
        "Total spent",
        "Last purchase",
    ])
    for customer in _shop_customer_rows(request.user):
        values = [
            customer["name"],
            customer["mobile"],
            customer["email"],
            customer["age"],
            customer["gender"],
            customer["shop_names"],
            customer["product_names"],
            customer["order_count"],
            customer["total_spent"],
            customer["last_purchase"].isoformat()
            if customer["last_purchase"]
            else "",
        ]
        writer.writerow([
            f"'{value}" if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            for value in values
        ])
    return response


@login_required
def owner_statement(request):
    shops = list(
        Shop.objects.filter(owner=request.user).order_by("name", "id")
    )
    shop_ids = [shop.pk for shop in shops]
    product_assignments = (
        ProductShop.objects.filter(shop_id__in=shop_ids)
        .select_related("shop", "product")
        .order_by("shop__name", "product__product_name")
    )
    staff_members = (
        ShopStaff.objects.filter(shop_id__in=shop_ids)
        .select_related("shop", "user")
        .order_by("shop__name", "role", "user__username")
    )
    shop_orders = (
        ShopOrder.objects.filter(shop_id__in=shop_ids)
        .select_related("shop", "order")
        .order_by("-created_at")
    )
    wallet_transactions = (
        ShopWalletTransaction.objects.filter(wallet__shop_id__in=shop_ids)
        .select_related("wallet__shop", "shop_order__order", "payout")
        .order_by("-created_at", "-id")[:100]
    )
    wallet_balances = {
        wallet.shop_id: wallet.balance
        for wallet in ShopWallet.objects.filter(shop_id__in=shop_ids)
    }

    shop_rows = []
    for shop in shops:
        shop_products = ProductShop.objects.filter(shop=shop)
        shop_orders_for_shop = ShopOrder.objects.filter(shop=shop)
        shop_rows.append({
            "shop": shop,
            "product_count": shop_products.count(),
            "active_product_count": shop_products.filter(
                is_active=True,
                product__is_active=True,
            ).count(),
            "staff_count": ShopStaff.objects.filter(shop=shop).count(),
            "order_count": shop_orders_for_shop.count(),
            "delivered_order_count": shop_orders_for_shop.filter(
                status="DELIVERED",
            ).count(),
            "wallet_balance": wallet_balances.get(shop.pk, Decimal("0.00")),
        })

    return render(request, "seller/owner_statement.html", {
        "shops": shops,
        "shop_rows": shop_rows,
        "product_assignments": product_assignments,
        "staff_members": staff_members,
        "shop_orders": shop_orders[:100],
        "wallet_transactions": wallet_transactions,
        "total_products": product_assignments.count(),
        "total_staff": staff_members.count(),
        "total_orders": shop_orders.count(),
        "total_delivered_orders": shop_orders.filter(status="DELIVERED").count(),
        "wallet_balance": sum(wallet_balances.values(), Decimal("0.00")),
    })


def signup(request):
    if request.method == 'POST':
        form = CustomerSignupForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                ClientProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        "name": user.get_full_name() or user.username,
                        "contact_no": "",
                        "email": user.email or "",
                        "is_active": True,
                        "verification_otp": generate_otp_code(),
                    },
                )
                _record_required_legal_acceptances(user)
            messages.success(request, 'Account created successfully. You can now log in.')
            return redirect('login')
    else:
        form = CustomerSignupForm()
    return render(request, 'registration/signup.html', {
        'form': form,
        'required_legal_documents': [
            (slug, LEGAL_PAGES[slug]["title"])
            for slug in REQUIRED_LEGAL_DOCUMENTS
        ],
    })


@login_required
def public_rider_apply(request):
    if not ClientProfile.objects.filter(user=request.user).exists():
        messages.info(request, "Create a customer account before applying to become a public rider.")
        return redirect("signup")

    application = PublicRiderApplication.objects.filter(user=request.user).first()
    if application and application.status == PublicRiderApplication.STATUS_APPROVED:
        return render(
            request,
            "seller/public_rider_application.html",
            {"application": application, "form": None},
        )
    if application and application.status == PublicRiderApplication.STATUS_PENDING:
        return render(
            request,
            "seller/public_rider_application.html",
            {"application": application, "form": None},
        )

    form = PublicRiderApplicationForm(
        request.POST or None,
        request.FILES or None,
        instance=application,
    )
    if request.method == "POST" and form.is_valid():
        application = form.save(commit=False)
        application.user = request.user
        application.status = PublicRiderApplication.STATUS_PENDING
        application.reviewed_by = None
        application.reviewed_at = None
        application.review_notes = ""
        application.save()
        messages.success(
            request,
            "Your public rider application was submitted for admin review.",
        )
        return redirect("public_rider_apply")

    return render(
        request,
        "seller/public_rider_application.html",
        {"application": application, "form": form},
    )


@login_required
def order_list(request):
    orders = (
        Order.objects
        .filter(user=request.user.username)
        .select_related("shop")
        .prefetch_related("shop_orders__shop", "shop_orders__order_items__product_shop__product")
        .order_by("-created_at")
    )
    order_groups = {
        "active": [],
        "delivered": [],
        "cancelled_customer": [],
        "cancelled_shop": [],
    }
    for order in orders:
        child_orders = list(order.shop_orders.all())
        statuses = {child.status.upper() for child in child_orders}
        if order.cancellation_source == "CUSTOMER" or (
            statuses == {"CANCELLED"}
            and child_orders
            and all(child.cancellation_source == "CUSTOMER" for child in child_orders)
        ):
            order_groups["cancelled_customer"].append(order)
        elif order.cancellation_source == "SHOP" or any(
            child.status.upper() == "CANCELLED" and child.cancellation_source == "SHOP"
            for child in child_orders
        ):
            order_groups["cancelled_shop"].append(order)
        elif child_orders and statuses == {"DELIVERED"}:
            order_groups["delivered"].append(order)
        else:
            order_groups["active"].append(order)
        order.shop_orders_list = child_orders
        order.can_customer_cancel = bool(child_orders) and all(
            child.status.upper() == "PLACED" for child in child_orders
        )
        order.can_change_address = bool(child_orders) and all(
            child.status.upper() in {"PLACED", "CONFIRMED", "PACKING", "READY"}
            for child in child_orders
        )
        order.can_edit_items = bool(child_orders) and all(
            child.status.upper() in {"PLACED", "CONFIRMED"}
            for child in child_orders
        )
    return render(request, "seller/order_list.html", {
        "order_groups": order_groups,
        "active_tab": request.GET.get("tab", "active"),
        "delivery_addresses": ClientDeliveryAddress.objects.filter(
            client__user=request.user,
            is_active=True,
        ).order_by("-is_default", "id"),
    })


@login_required
@transaction.atomic
def customer_reorder(request, order_id):
    if request.method != "POST":
        return redirect("order_list")

    order = get_object_or_404(
        Order.objects.filter(user=request.user.username),
        order_id=order_id,
    )
    cart = _address_scoped_cart(request)[0]

    is_b2b = _is_b2b_customer(request)
    unavailable = []
    added_count = 0
    historical_items = (
        OrderItem.objects.filter(shop_order__order=order)
        .select_related("product", "product_shop", "shop_order__shop")
        .order_by("id")
    )
    for historical_item in historical_items:
        assignment_id = historical_item.product_shop_id
        assignment = (
            ProductShop.objects.select_for_update()
            .select_related("product", "shop")
            .filter(
                pk=assignment_id,
                is_active=True,
                product__is_active=True,
            )
            .filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .first()
            if assignment_id
            else None
        )
        if not assignment or assignment.stock_for_customer(is_b2b=is_b2b) <= 0:
            unavailable.append(historical_item.product_name)
            continue

        is_bulk_order = historical_item.is_bulk_order
        if is_bulk_order and (
            assignment.effective_bulk_price_for_customer(
                is_b2b=_is_b2b_customer(request),
            ) is None
            or historical_item.quantity < assignment.bulk_min_quantity
        ):
            unavailable.append(historical_item.product_name)
            continue
        key = _cart_line_key(assignment.pk, is_bulk_order=is_bulk_order)
        try:
            current_quantity = max(0, int(cart.get(key, 0)))
        except (TypeError, ValueError):
            current_quantity = 0
        quantity_available = max(
            0,
            int(assignment.stock_for_customer(is_b2b=is_b2b))
            - current_quantity
            - _cart_quantity(cart, assignment.pk, is_bulk_order=not is_bulk_order),
        )
        quantity_to_add = min(int(historical_item.quantity), quantity_available)
        if quantity_to_add <= 0:
            unavailable.append(
                f"{historical_item.product_name} (no additional stock available)"
            )
            continue
        if is_bulk_order and quantity_to_add < assignment.bulk_min_quantity:
            unavailable.append(
                f"{historical_item.product_name} (available stock is below the bulk minimum)"
            )
            continue
        cart[key] = current_quantity + quantity_to_add
        added_count += quantity_to_add
        if quantity_to_add < historical_item.quantity:
            unavailable.append(
                f"{historical_item.product_name} (only {quantity_to_add} available)"
            )

    if added_count:
        _save_address_scoped_cart(request, cart)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        message = (
            f"Added {added_count} available item(s) to your cart."
            if added_count
            else "This order has no items available to reorder."
        )
        return JsonResponse({
            "success": bool(added_count),
            "message": message,
            "redirect_url": reverse("cart" if added_count else "order_list"),
            "unavailable": unavailable,
        })

    if added_count:
        messages.success(
            request,
            f"Added {added_count} available item(s) to your cart. Review the cart before checkout.",
        )
    if unavailable:
        messages.warning(
            request,
            "Some items could not be fully reordered: " + ", ".join(unavailable),
        )
    if not added_count and not unavailable:
        messages.warning(request, "This order has no items available to reorder.")
    return redirect("cart" if added_count else "order_list")


class OrderEditRejected(Exception):
    pass


def _current_coupon_discount(order, subtotal):
    if not order.coupon_code:
        return "", Decimal("0.00")
    coupon = Coupon.objects.filter(
        code__iexact=order.coupon_code,
        is_active=True,
        valid_from__lte=timezone.now(),
        valid_until__gte=timezone.now(),
    ).first()
    if not coupon or subtotal < coupon.minimum_order_amount:
        return "", Decimal("0.00")
    if coupon.discount_type == "PERCENT":
        discount = subtotal * coupon.discount_value / Decimal("100")
        if coupon.maximum_discount is not None:
            discount = min(discount, coupon.maximum_discount)
    else:
        discount = coupon.discount_value
    return coupon.code, min(subtotal, max(Decimal("0.00"), discount)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _apply_order_item_quantities(order, shop_orders, order_items, changes, actor, source):
    if not any(row.status.upper() in {"PLACED", "CONFIRMED"} for row in shop_orders):
        raise OrderEditRejected("This order can no longer be edited.")
    if any(row.status.upper() not in {"PLACED", "CONFIRMED"} for row in shop_orders):
        raise OrderEditRejected("Order items can only be edited before any shop order enters PACKING.")

    changed_items = []
    normalized_changes = {}
    for line in order_items:
        if line.id not in changes:
            continue
        raw_quantity = changes[line.id]
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            raise OrderEditRejected(f"Enter a valid quantity for {line.product_name}.")
        if quantity < 0 or quantity > 9999999:
            raise OrderEditRejected(f"Quantity for {line.product_name} is outside the allowed range.")
        normalized_changes[line.id] = quantity
        changed_items.append((line, quantity))
    if not changed_items:
        raise OrderEditRejected("No order item quantities were submitted.")

    bulk_minimums = dict(
        ProductShop.objects.filter(
            pk__in={
                line.product_shop_id
                for line in order_items
                if line.is_bulk_order and line.product_shop_id
            },
        ).values_list("pk", "bulk_min_quantity")
    )
    resulting_lines = []
    for line in order_items:
        quantity = normalized_changes.get(line.id, line.quantity)
        if quantity <= 0:
            continue
        bulk_minimum = bulk_minimums.get(line.product_shop_id)
        if line.is_bulk_order and bulk_minimum and quantity < bulk_minimum:
            raise OrderEditRejected(
                f"Bulk orders for {line.product_name} require at least "
                f"{bulk_minimum} bulk units, or remove the line."
            )
        resulting_lines.append({
            "line": line,
            "shop_order": line.shop_order,
            "quantity": quantity,
            "total": (line.price * quantity).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            ),
        })
    if not resulting_lines:
        raise OrderEditRejected("An order must contain at least one item.")

    assignment_ids = sorted({
        line.product_shop_id
        for line, _quantity in changed_items
        if line.product_shop_id
    })
    assignments = {
        assignment.pk: assignment
        for assignment in ProductShop.objects.select_for_update()
        .filter(pk__in=assignment_ids)
        .select_related("product", "shop")
        .order_by("pk")
    }
    increased_by_assignment = {}
    for line, quantity in changed_items:
        difference = quantity - line.quantity
        if difference <= 0:
            continue
        assignment = assignments.get(line.product_shop_id)
        if not assignment or not assignment.is_active or not assignment.product.is_active:
            raise OrderEditRejected(
                f"There is not enough available stock to increase {line.product_name}."
            )
        increased_by_assignment[line.product_shop_id] = (
            increased_by_assignment.get(line.product_shop_id, 0) + difference
        )
    for assignment_id, increase in increased_by_assignment.items():
        stock_field = "b2b_stock" if order.is_b2b_order else "stock"
        if getattr(assignments[assignment_id], stock_field) < increase:
            raise OrderEditRejected(
                f"There is not enough available stock to increase "
                f"{assignments[assignment_id].product.product_name}."
            )

    subtotal = sum((row["total"] for row in resulting_lines), Decimal("0.00"))
    coupon_code, coupon_discount = _current_coupon_discount(order, subtotal)
    buyer_state_code = next(
        (row.buyer_state_code for row in shop_orders if row.buyer_state_code),
        "",
    )
    buyer_gstin = next(
        (row.buyer_gstin for row in shop_orders if row.buyer_gstin),
        "",
    )
    shop_snapshots = {
        row.shop_id: _gst_shop_snapshot(row)
        for row in shop_orders
    }
    delivery_fees = {
        row.shop_id: row.delivery_fee
        for row in shop_orders
        if any(entry["shop_order"].id == row.id for entry in resulting_lines)
    }
    gst_items = [
        {
            "item": {"total": row["total"]},
            "total": row["total"],
            "shop": shop_snapshots[row["shop_order"].shop_id],
            "product": row["line"].product,
            "gst_rate": row["line"].gst_rate,
            "hsn_code": row["line"].hsn_code,
        }
        for row in resulting_lines
    ]
    has_gst_seller = any(row.seller_gstin for row in shop_orders)
    try:
        gst_breakdown = _build_gst_breakdown(
            gst_items,
            SimpleNamespace(state_code=buyer_state_code, gstin=buyer_gstin),
            coupon_discount,
            require_state=has_gst_seller,
            delivery_fees=delivery_fees,
        )
    except ValueError as error:
        raise OrderEditRejected(str(error))

    discounted_subtotal = max(Decimal("0.00"), subtotal - coupon_discount)
    payment_type = order.payment_type.upper()
    wallet_used = (
        Decimal("0.00")
        if payment_type == "WALLET"
        else min(order.wallet_amount_used, discounted_subtotal)
    )
    charges = _checkout_charge_totals(
        subtotal,
        coupon_discount,
        wallet_used if payment_type != "WALLET" else Decimal("0.00"),
        "RAZORPAY" if payment_type == "RAZORPAY" else "COD",
        gst_breakdown["total_gst"] + gst_breakdown["delivery_total"],
    )
    revised_total = (
        discounted_subtotal
        + charges["platform_charge"]
        + charges["razorpay_fee"]
        + gst_breakdown["total_gst"]
        + gst_breakdown["delivery_total"]
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if payment_type == "WALLET":
        wallet_used = revised_total

    client = ClientProfile.objects.select_for_update().filter(
        user__username=order.user
    ).first()
    if not client:
        raise OrderEditRejected("The customer's wallet account could not be found.")
    wallet_change = wallet_used - order.wallet_amount_used
    if payment_type == "WALLET" and wallet_change > client.wallet_balance:
        raise OrderEditRejected("The customer's wallet balance is insufficient to collect the revised total.")

    received = order.finally_received_amount
    revised_payable = max(Decimal("0.00"), revised_total - wallet_used)
    refund_amount = Decimal("0.00")
    if payment_type == "RAZORPAY":
        revised_payable = max(Decimal("0.00"), revised_total - wallet_used)
        if revised_payable > received:
            raise OrderEditRejected(
                "The revised Razorpay amount is higher, but this app cannot safely collect it: checkout verification only creates a new cart order, and this order stores only its original payment ID. No order or stock changes were made."
            )
        refund_amount = received - revised_payable
        if refund_amount and (
            not order.payment_id
            or not settings.RAZORPAY_KEY_ID
            or not settings.RAZORPAY_KEY_SECRET
        ):
            raise OrderEditRejected(
                "The revised order requires a Razorpay refund, but no refundable payment ID and configured Razorpay credentials are available."
            )

    if refund_amount:
        amount_paise = int(
            (refund_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        try:
            response = requests.post(
                f"https://api.razorpay.com/v1/payments/{order.payment_id}/refund",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                json={"amount": amount_paise},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Could not refund revised order %s.", order.order_id)
            raise OrderEditRejected(
                "Razorpay did not confirm the refund. The order and stock were not changed."
            )

    if wallet_change:
        if wallet_change > 0:
            client.wallet_balance -= wallet_change
            transaction_type = "DEBIT"
            description = "Additional wallet payment after order edit."
        else:
            client.wallet_balance += -wallet_change
            transaction_type = "CREDIT"
            description = "Wallet balance returned after order edit."
        client.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client,
            transaction_type=transaction_type,
            amount=abs(wallet_change),
            balance_after=client.wallet_balance,
            reference=order.order_id,
            description=description,
        )

    now_reference = f"ORDER-EDIT-{order.pk}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
    for line, quantity in changed_items:
        difference = quantity - line.quantity
        assignment = assignments.get(line.product_shop_id)
        if assignment and difference:
            stock_field = "b2b_stock" if order.is_b2b_order else "stock"
            current_stock = assignment.stock_for_customer(
                is_b2b=order.is_b2b_order,
            )
            setattr(assignment, stock_field, current_stock - difference)
            assignment.save(update_fields=[stock_field])
            StockMovement.objects.create(
                product_shop=assignment,
                movement_type="ADJUSTMENT",
                quantity=-difference,
                reference=f"{now_reference}-{line.pk}",
                created_by=actor,
            )
        if quantity == 0:
            line.delete()
            continue
        line.quantity = quantity
        line.total = (line.price * quantity).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if line.is_approximate_weight:
            line.estimated_weight = _estimated_weight_for_quantity(
                quantity,
                line.unit,
            )
            line.final_weight = None
        line.save(update_fields=[
            "quantity",
            "total",
            "estimated_weight",
            "final_weight",
        ])

    for shop_order in shop_orders:
        shop_lines = [
            entry for entry in resulting_lines
            if entry["shop_order"].id == shop_order.id
        ]
        if not shop_lines:
            shop_order.status = "CANCELLED"
            shop_order.cancellation_source = source
            shop_order.total_amount = Decimal("0.00")
            shop_order.product_count = 0
            shop_order.total_weight_kg = Decimal("0.00")
            shop_order.delivery_fee = Decimal("0.00")
            shop_order.taxable_amount = Decimal("0.00")
            shop_order.cgst_amount = Decimal("0.00")
            shop_order.sgst_amount = Decimal("0.00")
            shop_order.igst_amount = Decimal("0.00")
            shop_order.delivery_gst_amount = Decimal("0.00")
            shop_order.save(update_fields=[
                "status",
                "cancellation_source",
                "total_amount",
                "product_count",
                "total_weight_kg",
                "delivery_fee",
                "taxable_amount",
                "cgst_amount",
                "sgst_amount",
                "igst_amount",
                "delivery_gst_amount",
                "updated_at",
            ])
            OrderStatusHistory.objects.create(
                shop_order=shop_order,
                status="CANCELLED",
                remark=f"All items removed by {source.lower()} during order edit.",
            )
            continue

        shop_order.total_amount = sum(
            (entry["total"] for entry in shop_lines),
            Decimal("0.00"),
        )
        shop_order.product_count = sum(entry["quantity"] for entry in shop_lines)
        shop_order.total_weight_kg = sum(
            (
                (
                    _weight_in_kilograms(
                        _estimated_weight_for_quantity(
                            entry["quantity"],
                            entry["line"].unit,
                        ),
                        entry["line"].unit,
                    )
                    if entry["line"].is_approximate_weight
                    else Decimal("0.000")
                )
                for entry in shop_lines
            ),
            Decimal("0.000"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        shop_order.items = [
            {
                "product_id": entry["line"].product_id,
                "product_shop_id": entry["line"].product_shop_id,
                "product_name": entry["line"].product_name,
                "product_title": (
                    entry["line"].product.product_title
                    if entry["line"].product_id
                    else ""
                ),
                "sku": entry["line"].sku,
                "quantity": entry["quantity"],
                "price": str(entry["line"].price),
                "total": str(entry["total"]),
            }
            for entry in shop_lines
        ]
        shop_order.save(update_fields=[
            "total_amount",
            "product_count",
            "total_weight_kg",
            "items",
            "updated_at",
        ])

    # Rebuild every surviving shop's tax snapshot using the checkout GST rules.
    order_items_now = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order__order=order)
        .select_related("shop_order__shop", "product")
        .order_by("id")
    )
    current_gst_items = [
        {
            "item": {"total": line.total},
            "total": line.total,
            "shop": _gst_shop_snapshot(line.shop_order),
            "product": line.product,
            "gst_rate": line.gst_rate,
            "hsn_code": line.hsn_code,
        }
        for line in order_items_now
    ]
    final_gst = _build_gst_breakdown(
        current_gst_items,
        SimpleNamespace(state_code=buyer_state_code, gstin=buyer_gstin),
        coupon_discount,
        require_state=has_gst_seller,
        delivery_fees=delivery_fees,
    )
    for line, gst_item in zip(order_items_now, current_gst_items):
        line.taxable_amount = gst_item["taxable_amount"]
        line.cgst_amount = gst_item["cgst_amount"]
        line.sgst_amount = gst_item["sgst_amount"]
        line.igst_amount = gst_item["igst_amount"]
        line.save(update_fields=[
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
        ])
    for shop_order in shop_orders:
        breakdown = final_gst["shops"].get(shop_order.shop_id)
        if breakdown is None:
            continue
        shop_order.buyer_state_code = breakdown["buyer_state_code"]
        shop_order.buyer_gstin = breakdown["buyer_gstin"]
        shop_order.taxable_amount = breakdown["taxable_amount"]
        shop_order.cgst_amount = breakdown["cgst_amount"]
        shop_order.sgst_amount = breakdown["sgst_amount"]
        shop_order.igst_amount = breakdown["igst_amount"]
        shop_order.delivery_gst_amount = breakdown["delivery_gst_amount"]
        shop_order.save(update_fields=[
            "buyer_state_code",
            "buyer_gstin",
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "delivery_gst_amount",
            "updated_at",
        ])

    if payment_type == "RAZORPAY":
        received -= refund_amount
        payment_status = (
            "REFUNDED" if received == 0
            else "PARTIALLY_REFUNDED" if refund_amount
            else "PAID"
        )
        payment_done = True
    elif payment_type == "WALLET":
        received = Decimal("0.00")
        payment_status = "PAID"
        payment_done = True
    else:
        received = Decimal("0.00")
        payment_status = "COD" if revised_total > wallet_used else "PAID"
        payment_done = revised_total <= wallet_used

    order.coupon_code = coupon_code
    order.coupon_discount_amount = coupon_discount
    order.platform_charge_amount = charges["platform_charge"]
    order.razorpay_fee_amount = charges["razorpay_fee"]
    order.final_billing_amount = revised_total
    order.wallet_amount_used = wallet_used
    order.gst_amount = final_gst["total_gst"]
    order.finally_received_amount = received
    order.payment_amount = (
        max(Decimal("0.00"), revised_total - wallet_used - received)
        if payment_type != "WALLET"
        else Decimal("0.00")
    )
    order.is_payment_done = payment_done
    order.payment_status = payment_status
    order.save(update_fields=[
        "coupon_code",
        "coupon_discount_amount",
        "platform_charge_amount",
        "razorpay_fee_amount",
        "final_billing_amount",
        "wallet_amount_used",
        "gst_amount",
        "finally_received_amount",
        "payment_amount",
        "is_payment_done",
        "payment_status",
        "updated_at",
    ])

    if source == "SHOP":
        customer_user = get_user_model().objects.filter(username=order.user).first()
        if customer_user:
            Notification.objects.create(
                user=customer_user,
                title="Your order was updated by the shop",
                message=(
                    f"The shop updated items in order {order.order_id}. "
                    f"Your revised total is ₹{revised_total:.2f}."
                ),
                notification_type="ORDER",
                related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
            )
    return revised_total


def _order_edit_page(request, order, shop_order=None):
    items = OrderItem.objects.filter(shop_order__order=order)
    if shop_order:
        items = items.filter(shop_order=shop_order)
    items = items.select_related("product", "product_shop", "shop_order__shop").order_by("id")
    return render(request, "seller/order_edit.html", {
        "order": order,
        "shop_order": shop_order,
        "order_items": items,
    })


def _customer_order_action_response(
    request,
    level,
    message,
    redirect_name="order_list",
    redirect_kwargs=None,
    **data,
):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": level == "success",
            "message": message,
            "redirect_url": reverse(redirect_name, kwargs=redirect_kwargs),
            **data,
        })
    getattr(messages, level)(request, message)
    return redirect(redirect_name, **(redirect_kwargs or {}))


@login_required
def customer_edit_placed_order(request, order_id):
    if request.method not in {"GET", "POST"}:
        return redirect("order_list")
    try:
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().filter(user=request.user.username),
                order_id=order_id,
            )
            shop_orders = list(
                ShopOrder.objects.select_for_update()
                .filter(order=order)
                .select_related("shop")
                .order_by("id")
            )
            if not shop_orders or any(
                row.status.upper() not in {"PLACED", "CONFIRMED"}
                for row in shop_orders
            ):
                raise OrderEditRejected(
                    "Order items can only be edited before any shop order enters PACKING."
                )
            order_items = list(
                OrderItem.objects.select_for_update()
                .filter(shop_order__order=order)
                .select_related("product", "product_shop", "shop_order__shop")
                .order_by("id")
            )
            if request.method == "GET":
                return _order_edit_page(request, order, None)
            changes = {
                line.id: request.POST.get(f"quantity_{line.id}")
                for line in order_items
            }
            revised_total = _apply_order_item_quantities(
                order, shop_orders, order_items, changes, request.user, "CUSTOMER"
            )
    except OrderEditRejected as error:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return _customer_order_action_response(request, "error", str(error))
        messages.error(request, str(error))
        return redirect("order_list")
    return _customer_order_action_response(
        request,
        "success",
        f"Order updated. Revised total: ₹{revised_total:.2f}.",
        revised_total=str(revised_total),
    )


@login_required
def shop_edit_order_items(request, shop_order_id):
    if request.method not in {"GET", "POST"}:
        return redirect("shop_order_list")
    try:
        with transaction.atomic():
            shop_order = get_object_or_404(
                ShopOrder.objects.select_for_update()
                .select_related("shop", "order"),
                pk=shop_order_id,
            )
            staff = ShopStaff.objects.filter(
                shop=shop_order.shop,
                user=request.user,
                is_active=True,
            ).first()
            authorized = (
                shop_order.shop.owner_id == request.user.id
                or bool(staff and (
                    staff.role == "MANAGER"
                    or (staff.role == "ORDER" and staff.can_view_orders)
                ))
            )
            if not authorized:
                raise PermissionDenied
            order = Order.objects.select_for_update().get(pk=shop_order.order_id)
            shop_orders = list(
                ShopOrder.objects.select_for_update()
                .filter(order=order)
                .select_related("shop")
                .order_by("id")
            )
            if any(
                row.status.upper() not in {"PLACED", "CONFIRMED"}
                for row in shop_orders
            ):
                raise OrderEditRejected(
                    "Order items can only be edited before any shop order enters PACKING."
                )
            order_items = list(
                OrderItem.objects.select_for_update()
                .filter(shop_order__order=order)
                .select_related("product", "product_shop", "shop_order__shop")
                .order_by("id")
            )
            if request.method == "GET":
                return _order_edit_page(request, order, shop_order)
            target_lines = [line for line in order_items if line.shop_order_id == shop_order.id]
            changes = {
                line.id: request.POST.get(f"quantity_{line.id}")
                for line in target_lines
            }
            revised_total = _apply_order_item_quantities(
                order, shop_orders, order_items, changes, request.user, "SHOP"
            )
    except OrderEditRejected as error:
        messages.error(request, str(error))
        return redirect("shop_order_detail", shop_order_id=shop_order_id)
    messages.success(request, f"Order items updated. Revised total: ₹{revised_total:.2f}.")
    return redirect("shop_order_detail", shop_order_id=shop_order_id)


def _restock_cancelled_shop_order(shop_order, actor):
    is_b2b = shop_order.order.is_b2b_order
    reference = f"CANCEL-RESTOCK-{shop_order.pk}"
    order_items = (
        OrderItem.objects.select_for_update()
        .filter(shop_order=shop_order, product_shop__isnull=False)
        .order_by("product_shop_id")
    )
    for item in order_items:
        assignment = ProductShop.objects.select_for_update().filter(
            pk=item.product_shop_id
        ).first()
        if not assignment or StockMovement.objects.filter(
            product_shop=assignment,
            movement_type="ADJUSTMENT",
            reference=reference,
        ).exists():
            continue
        stock_field = "b2b_stock" if is_b2b else "stock"
        current_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        setattr(assignment, stock_field, current_stock + item.quantity)
        assignment.save(update_fields=[stock_field])
        StockMovement.objects.create(
            product_shop=assignment,
            movement_type="ADJUSTMENT",
            quantity=item.quantity,
            reference=reference,
            created_by=actor,
        )


def _refund_cancelled_shop_order(shop_order, actor, *, cancellation_source="SHOP"):
    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    tax_amount = (
        shop_order.cgst_amount
        + shop_order.sgst_amount
        + shop_order.igst_amount
    )
    refundable_amount = shop_order.taxable_amount + tax_amount
    if refundable_amount <= 0 and shop_order.total_amount > 0:
        refundable_amount = shop_order.total_amount
    available_order_value = max(
        Decimal("0.00"),
        order.final_billing_amount
        - order.platform_charge_amount
        - order.razorpay_fee_amount,
    )
    if available_order_value:
        refundable_amount = min(refundable_amount, available_order_value)
    if refundable_amount <= 0:
        return True, ""

    cent = Decimal("0.01")
    wallet_available = max(Decimal("0.00"), order.wallet_amount_used)
    wallet_refund = (
        (wallet_available * refundable_amount / available_order_value).quantize(
            cent,
            rounding=ROUND_HALF_UP,
        )
        if available_order_value
        else min(wallet_available, refundable_amount)
    )
    wallet_refund = min(wallet_refund, wallet_available, refundable_amount)
    online_refund = refundable_amount - wallet_refund
    if (
        order.payment_type == "RAZORPAY"
        and online_refund > order.finally_received_amount
    ):
        wallet_refund += online_refund - order.finally_received_amount
        online_refund = order.finally_received_amount
    if wallet_refund > wallet_available:
        return False, "The order balance is insufficient to refund this shop cancellation."
    cash_refund = (
        min(online_refund, order.finally_received_amount)
        if order.payment_type == "COD"
        else Decimal("0.00")
    )
    wallet_credit = wallet_refund + cash_refund
    client = None
    if wallet_credit:
        client = ClientProfile.objects.select_for_update().filter(
            user__username=order.user
        ).first()
        if not client:
            return False, "The customer wallet could not be found. The order was not cancelled."

    if online_refund and order.payment_type == "RAZORPAY":
        if not order.payment_id or not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            return False, "Razorpay refund is not configured. The order was not cancelled."
        amount_paise = int(
            (online_refund * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        try:
            response = requests.post(
                f"https://api.razorpay.com/v1/payments/{order.payment_id}/refund",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                json={
                    "amount": amount_paise,
                    "notes": {
                        "shop_order_id": str(shop_order.pk),
                        "reason": "Shop cancellation",
                    },
                },
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Could not refund Razorpay payment for cancelled shop order %s.", shop_order.pk)
            return False, "The Razorpay refund could not be confirmed. The order was not cancelled."
    elif online_refund and order.payment_type != "COD" and (
        order.is_payment_done or order.finally_received_amount > 0
    ):
        return False, "This payment cannot be refunded automatically. The order was not cancelled."

    if wallet_credit:
        client.wallet_balance += wallet_credit
        client.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client,
            transaction_type="CREDIT",
            amount=wallet_credit,
            balance_after=client.wallet_balance,
            reference=f"{order.order_id}:{shop_order.pk}",
            description=f"Wallet returned for shop-cancelled order {order.order_id}.",
        )

    shop_order.razorpay_refund_amount = (
        online_refund if order.payment_type == "RAZORPAY" else Decimal("0.00")
    )
    shop_order.wallet_refund_amount = wallet_credit
    shop_order.save(update_fields=["razorpay_refund_amount", "wallet_refund_amount"])
    order.final_billing_amount = max(
        Decimal("0.00"),
        order.final_billing_amount - refundable_amount,
    )
    order.wallet_amount_used = max(
        Decimal("0.00"),
        order.wallet_amount_used - wallet_refund,
    )
    order.finally_received_amount = max(
        Decimal("0.00"),
        order.finally_received_amount
        - (online_refund if order.payment_type == "RAZORPAY" else cash_refund),
    )
    order.gst_amount = max(Decimal("0.00"), order.gst_amount - tax_amount)
    order.payment_amount = max(
        Decimal("0.00"),
        order.final_billing_amount
        - order.wallet_amount_used
        - order.finally_received_amount,
    )
    order.is_payment_done = (
        order.payment_amount == 0
        and (
            order.is_payment_done
            or order.payment_type in {"RAZORPAY", "WALLET"}
        )
    )
    if order.payment_type == "RAZORPAY" and online_refund:
        order.payment_status = (
            "REFUNDED"
            if order.finally_received_amount == 0
            else "PARTIALLY_REFUNDED"
        )
    order.save(update_fields=[
        "final_billing_amount",
        "wallet_amount_used",
        "finally_received_amount",
        "gst_amount",
        "payment_amount",
        "is_payment_done",
        "payment_status",
        "updated_at",
    ])
    customer_user = get_user_model().objects.filter(username=order.user).first()
    if customer_user:
        refund_methods = []
        if shop_order.razorpay_refund_amount:
            refund_methods.append("Razorpay")
        if shop_order.wallet_refund_amount:
            refund_methods.append("wallet balance")
        refund_message = (
            f"₹{refundable_amount:.2f} was refunded via {' and '.join(refund_methods)}."
            if refund_methods
            else f"Your amount due was reduced by ₹{refundable_amount:.2f}."
        )
        cancellation_label = (
            "Shop cancellation"
            if cancellation_source == "SHOP"
            else "Order cancellation"
        )
        Notification.objects.create(
            user=customer_user,
            title=f"{cancellation_label} refund processed",
            message=f"{cancellation_label} for order {order.order_id} was processed. {refund_message}",
            notification_type="ORDER",
            related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
        )
    return True, ""


@login_required
@transaction.atomic
def customer_cancel_order(request, order_id):
    if request.method != "POST":
        return redirect("order_list")
    order = get_object_or_404(
        Order.objects.select_for_update().filter(user=request.user.username),
        order_id=order_id,
    )
    shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
    )
    if not shop_orders or order.status.upper() == "CANCELLED":
        return _customer_order_action_response(
            request,
            "error",
            "This order is already cancelled or has no cancellable shop orders.",
        )
    if any(shop_order.status.upper() != "PLACED" for shop_order in shop_orders):
        return _customer_order_action_response(
            request,
            "error",
            "Orders can only be cancelled before a shop confirms them.",
        )

    if order.payment_type == "RAZORPAY" and order.finally_received_amount > 0:
        try:
            response = requests.post(
                f"https://api.razorpay.com/v1/payments/{order.payment_id}/refund",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                json={"amount": int(order.finally_received_amount * 100)},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Could not refund Razorpay payment for cancelled order %s.", order.order_id)
            return _customer_order_action_response(
                request,
                "error",
                "The Razorpay refund could not be confirmed. The order was not cancelled; please contact support.",
            )

    client = ClientProfile.objects.select_for_update().filter(user=request.user).first()
    if client and order.wallet_amount_used:
        client.wallet_balance += order.wallet_amount_used
        client.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client,
            transaction_type="CREDIT",
            amount=order.wallet_amount_used,
            balance_after=client.wallet_balance,
            reference=order.order_id,
            description="Wallet balance returned after customer order cancellation.",
        )
    now = timezone.now()
    for shop_order in shop_orders:
        _restock_cancelled_shop_order(shop_order, request.user)
        shop_order.status = "CANCELLED"
        shop_order.cancellation_source = "CUSTOMER"
        shop_order.save(update_fields=["status", "cancellation_source", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="CANCELLED",
            remark="Cancelled by customer before shop confirmation.",
        )
    order.status = "CANCELLED"
    order.cancellation_source = "CUSTOMER"
    if order.payment_type == "RAZORPAY" and order.finally_received_amount > 0:
        order.payment_status = "REFUNDED"
    order.save(update_fields=["status", "cancellation_source", "payment_status", "updated_at"])
    return _customer_order_action_response(
        request,
        "success",
        "Order cancelled. Any wallet amount was returned; Razorpay refund has been requested.",
    )


@login_required
@transaction.atomic
def customer_cancel_shop_order(request, order_id, shop_order_id):
    tracking_order_id = order_id
    if request.method != "POST":
        return redirect("order_tracking", order_id=tracking_order_id)

    order = get_object_or_404(
        Order.objects.select_for_update().filter(user=request.user.username),
        Q(tracking_code=order_id) | Q(order_id=order_id),
    )
    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        pk=shop_order_id,
        order=order,
    )
    if shop_order.status.upper() != "PLACED":
        return _customer_order_action_response(
            request,
            "error",
            "This shop order can only be cancelled before the shop confirms it.",
            redirect_name="order_tracking",
            redirect_kwargs={"order_id": tracking_order_id},
            shop_order_id=shop_order.pk,
        )

    refund_ok, refund_message = _refund_cancelled_shop_order(
        shop_order,
        request.user,
        cancellation_source="CUSTOMER",
    )
    if not refund_ok:
        return _customer_order_action_response(
            request,
            "error",
            refund_message,
            redirect_name="order_tracking",
            redirect_kwargs={"order_id": tracking_order_id},
            shop_order_id=shop_order.pk,
        )

    _restock_cancelled_shop_order(shop_order, request.user)
    shop_order.status = "CANCELLED"
    shop_order.cancellation_source = "CUSTOMER"
    shop_order.save(update_fields=["status", "cancellation_source", "updated_at"])
    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="CANCELLED",
        remark="Cancelled by customer before shop confirmation.",
    )

    other_shop_orders = ShopOrder.objects.filter(order=order).exclude(
        pk=shop_order.pk,
    )
    if not other_shop_orders.exclude(status__iexact="CANCELLED").exists():
        order.status = "CANCELLED"
        order.cancellation_source = (
            "SHOP"
            if other_shop_orders.filter(cancellation_source="SHOP").exists()
            else "CUSTOMER"
        )
        order.save(update_fields=["status", "cancellation_source", "updated_at"])

    return _customer_order_action_response(
        request,
        "success",
        f"The order from {shop_order.shop.name} was cancelled.",
        redirect_name="order_tracking",
        redirect_kwargs={"order_id": tracking_order_id},
        shop_order_id=shop_order.pk,
        status="CANCELLED",
    )


@login_required
@transaction.atomic
def customer_change_order_address(request, order_id):
    if request.method != "POST":
        return redirect("order_list")
    order = get_object_or_404(
        Order.objects.select_for_update().filter(user=request.user.username),
        order_id=order_id,
    )
    shop_orders = list(ShopOrder.objects.select_for_update().filter(order=order))
    if not shop_orders or any(
        child.status.upper() not in {"PLACED", "CONFIRMED", "PACKING", "READY"}
        for child in shop_orders
    ):
        return _customer_order_action_response(
            request,
            "error",
            "Delivery address is locked after rider pickup.",
        )
    address = get_object_or_404(
        ClientDeliveryAddress,
        pk=request.POST.get("address_id"),
        client__user=request.user,
        is_active=True,
    )
    client = ClientProfile.objects.filter(user=request.user).first()
    buyer_gstin = (
        address.gstin
        or         (
            client.gstin
            if client and _is_b2b_customer(request, client)
            else ""
        )
    )
    try:
        _refresh_order_gst_snapshots(order, address, buyer_gstin)
    except ValueError as error:
        return _customer_order_action_response(request, "error", str(error))
    location = address.location or {}
    order.delivery_address = address.address
    order.delivery_location = (
        f"{location['lat']},{location['lng']}"
        if isinstance(location, dict) and location.get("lat") is not None and location.get("lng") is not None
        else str(location)
    )
    order.delivery_contact_no = address.client.contact_no
    order.save(update_fields=["delivery_address", "delivery_location", "delivery_contact_no", "updated_at"])
    return _customer_order_action_response(
        request,
        "success",
        "Delivery address updated for this order.",
        address=address.address,
        address_label=address.label,
    )


@login_required
def customer_order_feedback(request, order_id):
    if request.method != "POST":
        return redirect("order_list")
    shop_order = get_object_or_404(
        ShopOrder.objects.select_related("order").filter(
            order__order_id=order_id,
            order__user=request.user.username,
            status="DELIVERED",
        ),
        pk=request.POST.get("shop_order_id"),
    )
    feedback_type = request.POST.get("feedback_type", "").upper()
    if feedback_type not in {CustomerProductFeedback.COMPLAINT, CustomerProductFeedback.REJECTED}:
        return _customer_order_action_response(request, "error", "Select a valid feedback type.")
    item = None
    item_id = (request.POST.get("order_item_id") or "").strip()
    if item_id:
        item = get_object_or_404(OrderItem, pk=item_id, shop_order=shop_order)
    if feedback_type == CustomerProductFeedback.REJECTED and item is None:
        return _customer_order_action_response(request, "error", "Select the product you rejected.")
    CustomerProductFeedback.objects.get_or_create(
        user=request.user,
        shop_order=shop_order,
        order_item=item,
        feedback_type=feedback_type,
        defaults={"details": (request.POST.get("details") or "").strip()[:2000]},
    )
    return _customer_order_action_response(
        request,
        "success",
        "Thank you. Your feedback has been recorded.",
    )


@login_required
def toggle_favorite_shop(request, shop_id):
    if request.method != "POST":
        return redirect("customer_favorites")
    get_object_or_404(Shop, pk=shop_id)
    favorite, created = FavoriteShop.objects.get_or_create(user=request.user, shop_id=shop_id)
    if not created:
        favorite.delete()
    return redirect(_safe_favorites_return(request))


@login_required
def toggle_favorite_product(request, product_shop_id):
    if request.method != "POST":
        return redirect("customer_favorites")
    get_object_or_404(ProductShop, pk=product_shop_id)
    favorite, created = FavoriteProduct.objects.get_or_create(
        user=request.user,
        product_shop_id=product_shop_id,
    )
    if not created:
        favorite.delete()
    return redirect(_safe_favorites_return(request))


def _safe_favorites_return(request):
    target = request.POST.get("return_to") or request.GET.get("return_to", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return redirect("customer_favorites").url


@login_required
def customer_favorites(request):
    client = ClientProfile.objects.filter(user=request.user).first()
    if client is None:
        raise PermissionDenied
    is_b2b = _is_b2b_customer(request, client)
    favorite_shop_ids = list(
        FavoriteShop.objects.filter(user=request.user).values_list(
            "shop_id",
            flat=True,
        )
    )
    favorite_product_ids = set(
        FavoriteProduct.objects.filter(user=request.user).values_list(
            "product_shop_id",
            flat=True,
        )
    )
    available_shop_products = (
        ProductShop.objects.select_related("product")
        .filter(
            is_active=True,
            product__is_active=True,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
        .order_by("product__product_name")
    )
    products = list(
        available_shop_products.filter(
            Q(shop_id__in=favorite_shop_ids)
            | Q(pk__in=favorite_product_ids)
        ).order_by("product__product_name", "shop__name")
    )
    for product in products:
        product.is_favorite_product = product.pk in favorite_product_ids
        product.is_favorite_shop = product.shop_id in favorite_shop_ids
        product.marketplace_stock = product.stock_for_customer(is_b2b=is_b2b)
        product.marketplace_price = product.effective_price_for_customer(is_b2b=is_b2b)
        product.marketplace_unit = product.unit_for_customer(is_b2b=is_b2b)
        product.bulk_marketplace_price = product.effective_bulk_price_for_customer(is_b2b=is_b2b)
        product.bulk_marketplace_unit = product.bulk_unit_for_customer(is_b2b=is_b2b)
        product.has_bulk_offer = (
            product.bulk_marketplace_price is not None
            and bool(product.bulk_marketplace_unit)
        )
    return render(request, "market/favorites.html", {
        "favorite_products": products,
        "is_b2b_customer": is_b2b,
    })


def _reputation_metrics(shop, product_shop=None):
    now = timezone.now()
    cutoff = now - timedelta(days=90)
    delivered_orders = list(
        ShopOrder.objects.filter(
            shop=shop,
            status="DELIVERED",
            delivery_completed_at__gte=cutoff,
        ).order_by("-delivery_completed_at")[:200]
    )
    order_ids = [row.pk for row in delivered_orders]
    history = {}
    for shop_order_id, status, created_at in OrderStatusHistory.objects.filter(
        shop_order_id__in=order_ids,
        status__in=["PICKED_UP", "OUT_FOR_DELIVERY"],
    ).order_by("created_at").values_list("shop_order_id", "status", "created_at"):
        history.setdefault(shop_order_id, {}).setdefault(status, created_at)

    timely_count = 0
    for delivered in delivered_orders:
        started = history.get(delivered.pk, {}).get("OUT_FOR_DELIVERY") or history.get(
            delivered.pk, {}
        ).get("PICKED_UP")
        if started and delivered.delivery_completed_at <= started + timedelta(
            minutes=delivered.rider_eta_minutes
        ):
            timely_count += 1
    on_time_score = (
        timely_count * 100 / len(delivered_orders)
        if delivered_orders
        else 50.0
    )

    feedback = CustomerProductFeedback.objects.filter(shop_order__shop=shop)
    if product_shop:
        feedback = feedback.filter(order_item__product_shop=product_shop)
        delivered_item_count = OrderItem.objects.filter(
            shop_order__shop=shop,
            shop_order__status="DELIVERED",
            product_shop=product_shop,
        ).count()
        product_order_count = OrderItem.objects.filter(
            shop_order__shop=shop,
            shop_order__status="DELIVERED",
            product_shop=product_shop,
        ).values("shop_order_id").distinct().count()
        complaint_denominator = max(1, product_order_count)
        rejection_denominator = max(1, delivered_item_count)
        last_activity = max(
            shop.updated_at,
            product_shop.product.updated_at,
        )
    else:
        complaint_denominator = max(1, len(delivered_orders))
        delivered_item_count = OrderItem.objects.filter(
            shop_order__shop=shop,
            shop_order__status="DELIVERED",
        ).count()
        rejection_denominator = max(1, delivered_item_count)
        last_order = ShopOrder.objects.filter(shop=shop).order_by("-created_at").values_list(
            "created_at", flat=True
        ).first()
        last_activity = max(shop.updated_at, last_order or shop.updated_at)

    complaints = feedback.filter(feedback_type=CustomerProductFeedback.COMPLAINT).count()
    rejections = feedback.filter(feedback_type=CustomerProductFeedback.REJECTED).count()
    complaint_rate = complaints * 100 / complaint_denominator
    rejection_rate = rejections * 100 / rejection_denominator
    complaint_score = max(0.0, 100.0 - complaint_rate * 10)
    rejection_score = max(0.0, 100.0 - rejection_rate * 10)
    days_since_activity = max(0.0, (now - last_activity).total_seconds() / 86400)
    activity_score = max(0.0, 100.0 * (1 - days_since_activity / 30))
    rank = round(
        on_time_score * 0.4
        + activity_score * 0.2
        + complaint_score * 0.2
        + rejection_score * 0.2
    )
    return {
        "rank_score": rank,
        "on_time_score": round(on_time_score),
        "activity_score": round(activity_score),
        "complaint_score": round(complaint_score),
        "rejection_score": round(rejection_score),
        "complaints": complaints,
        "rejections": rejections,
    }


def order_create(request):
    if request.method == 'POST':
        form = OrderForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Order created successfully.')
            return redirect('order_list')
    else:
        form = OrderForm()
    return render(request, 'seller/order_form.html', {
        'form': form,
        'form_title': 'New Order',
        'form_action': 'create'
    })

def order_edit(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        form = OrderForm(request.POST, instance=order)
        if form.is_valid():
            form.save()
            messages.success(request, 'Order updated successfully.')
            return redirect('order_list')
    else:
        form = OrderForm(instance=order)
    return render(request, 'seller/order_form.html', {
        'form': form,
        'form_title': 'Edit Order',
        'form_action': 'update'
    })

def order_delete(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        order.delete()
        messages.success(request, 'Order deleted successfully.')
        return redirect('order_list')
    return render(request, 'seller/order_delete_confirm.html', {'order': order})

import json

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ShopForm
from .models import Shop

@login_required
def shop_list(request):

    if request.user.is_superuser:
        shops = Shop.objects.all().order_by("-id")
    else:
        shops = Shop.objects.filter(
            owner=request.user
        ).order_by("-id")

    for shop in shops:
        shop.storefront_public_url = _shop_storefront_url(request, shop)
        shop.storefront_qr_data_uri = _qr_code_data_uri(
            shop.storefront_public_url,
            box_size=3,
        )
        shop.storefront_is_active = _storefront_is_active(shop)

    storefront_settings = ShopStorefrontSettings.objects.first()
    return render(
        request,
        "shop/shop_list.html",
        {
            "shops": shops,
            **_storefront_form_url_parts(request),
            "storefront_monthly_price": (
                storefront_settings.monthly_subscription_price
                if storefront_settings
                else Decimal("0.00")
            ),
        }
    )

@login_required
def shop_add(request):
    storefront_settings = ShopStorefrontSettings.objects.first()

    if request.method == "POST":

        form = ShopForm(
            request.POST,
            request.FILES,
            require_legal_acceptance=not Shop.objects.filter(owner=request.user).exists(),
        )

        pickup_locations = None
        if form.is_valid():
            try:
                pickup_locations = _parse_shop_pickup_locations(
                    request.POST.get("pickup_locations_json", "[]"),
                    max_locations=_delivery_sharing_settings().max_additional_pickup_locations,
                )
            except ValueError as error:
                form.add_error(None, str(error))

        if form.is_valid() and pickup_locations is not None:

            shop = form.save(
                commit=False
            )

            shop.owner = request.user
            shop.pickup_locations = pickup_locations

            try:

                shop.location = json.loads(
                    request.POST.get(
                        "location_json",
                        "null"
                    )
                )

            except json.JSONDecodeError:

                shop.location = None
            if isinstance(shop.location, dict):
                shop.location["address"] = request.POST.get(
                    "primary_pickup_address",
                    "",
                ).strip()[:500]

            try:

                shop.delivery_area = json.loads(
                    request.POST.get(
                        "delivery_area_json",
                        "[]"
                    )
                )

            except json.JSONDecodeError:

                shop.delivery_area = []

            try:

                shop.delivery_pincode = json.loads(
                    request.POST.get(
                        "delivery_pincode_json",
                        "[]"
                    )
                )

            except json.JSONDecodeError:

                shop.delivery_pincode = []

            try:

                shop.shop_break_time_list = json.loads(
                    request.POST.get(
                        "break_time_json",
                        "[]"
                    )
                )

            except json.JSONDecodeError:

                shop.shop_break_time_list = []

            with transaction.atomic():
                shop.save()
                if form.cleaned_data.get("accept_legal_documents"):
                    _record_required_legal_acceptances(request.user)

            request.session["active_portal"] = "SHOP_OWNER"
            return redirect(
                "shop_list"
            )

    else:

        form = ShopForm(
            initial={
                "is_shop_open": True
            },
            require_legal_acceptance=not Shop.objects.filter(owner=request.user).exists(),
        )

    context = {

        "form": form,

        "title": "Add Shop",

        "shop": None,
        "requires_legal_acceptance": not Shop.objects.filter(owner=request.user).exists(),
        "required_legal_documents": [
            (slug, LEGAL_PAGES[slug]["title"])
            for slug in REQUIRED_LEGAL_DOCUMENTS
        ],

        "location_json": "null",
        "primary_pickup_address": "",
        "pickup_locations_data": [],
        "max_additional_pickup_locations": (
            _delivery_sharing_settings().max_additional_pickup_locations
        ),

        "delivery_area_json": "[]",

        "delivery_pincode_json": "[]",

        "break_time_json": "[]",
        **_storefront_form_url_parts(request),
        "storefront_monthly_price": (
            storefront_settings.monthly_subscription_price
            if storefront_settings
            else Decimal("0.00")
        ),
    }

    return render(
        request,
        "shop/shop_form.html",
        context
    )

@login_required
def shop_edit(request, pk):

    shop = get_object_or_404(
        Shop,
        pk=pk
    )

    if not request.user.is_superuser:

        if shop.owner != request.user:

            return redirect("shop_list")

    if request.method == "POST":

        form = ShopForm(
            request.POST,
            request.FILES,
            instance=shop
        )

        if form.is_valid():

            try:
                pickup_locations = _parse_shop_pickup_locations(
                    request.POST.get("pickup_locations_json", "[]"),
                    max_locations=_delivery_sharing_settings().max_additional_pickup_locations,
                )
            except ValueError as error:
                form.add_error(None, str(error))
                pickup_locations = None

            if pickup_locations is not None:
                shop = form.save(commit=False)
                shop.pickup_locations = pickup_locations

                location_json = request.POST.get(
                    "location_json",
                    ""
                )

                if location_json:

                    try:
                        shop.location = json.loads(
                            location_json
                        )

                    except json.JSONDecodeError:

                        shop.location = None

                else:

                    shop.location = None

                if isinstance(shop.location, dict):
                    shop.location["address"] = request.POST.get(
                        "primary_pickup_address",
                        "",
                    ).strip()[:500]

                area_json = request.POST.get(
                    "delivery_area_json",
                    ""
                )

                if area_json:

                    try:
                        shop.delivery_area = json.loads(
                            area_json
                        )

                    except json.JSONDecodeError:

                        shop.delivery_area = []

                else:

                    shop.delivery_area = []

                pincode_json = request.POST.get(
                    "delivery_pincode_json",
                    ""
                )

                if pincode_json:

                    try:
                        shop.delivery_pincode = json.loads(
                            pincode_json
                        )

                    except json.JSONDecodeError:

                        shop.delivery_pincode = []

                else:

                    shop.delivery_pincode = []

                break_json = request.POST.get(
                    "break_time_json",
                    ""
                )

                if break_json:

                    try:
                        shop.shop_break_time_list = json.loads(
                            break_json
                        )

                    except json.JSONDecodeError:

                        shop.shop_break_time_list = []

                else:

                    shop.shop_break_time_list = []

                shop.save()

                return redirect(
                    "shop_list"
                )

    else:

        form = ShopForm(
            instance=shop
        )

    context = {

        "form": form,

        "title": "Edit Shop",

        "shop": shop,

        "location_json":
            json.dumps(
                shop.location or None
            ),
        "primary_pickup_address": (
            (shop.location or {}).get("address", "")
            if isinstance(shop.location, dict)
            else ""
        ),
        "pickup_locations_data": shop.pickup_locations or [],
        "max_additional_pickup_locations": (
            _delivery_sharing_settings().max_additional_pickup_locations
        ),

        "delivery_area_json":
            json.dumps(
                shop.delivery_area or []
            ),

        "delivery_pincode_json":
            json.dumps(
                shop.delivery_pincode or []
            ),

        "break_time_json":
            json.dumps(
                shop.shop_break_time_list or []
            ),
        **_storefront_form_url_parts(request),

    }

    return render(
        request,
        "shop/shop_form.html",
        context
    )
@login_required
def shop_delete(request, pk):

    shop = get_object_or_404(
        Shop,
        pk=pk
    )

    if not request.user.is_superuser:
        messages.error(
            request,
            "You do not have permission to delete this shop."
        )
        return redirect("shop_list")

    if request.method == "POST":

        shop_name = shop.name

        shop.delete()

        messages.success(
            request,
            f"Shop '{shop_name}' deleted successfully."
        )

        return redirect("shop_list")

    return redirect("shop_list")

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.core.exceptions import PermissionDenied

from .models import (
    Shop,
    ProductShop,
    ShopOrder,
)
@login_required
def shop_detail(request, pk):

    shop = get_object_or_404(
        Shop,
        pk=pk
    )

    is_owner = (
        shop.owner_id == request.user.id
    )

    is_staff = shop.staff_members.filter(
        user=request.user,
        is_active=True
    ).exists()

    if not is_owner and not is_staff:
        raise PermissionDenied(
            "You are not authorized to access this shop."
        )

    can_manage_staff = (
        is_owner
        or shop.staff_members.filter(
            user=request.user,
            is_active=True,
            can_manage_staff=True,
        ).exists()
    )
    staff_record = shop.staff_members.filter(
        user=request.user,
        is_active=True,
    ).first()
    can_manage_stock = is_owner or bool(
        staff_record and staff_record.can_manage_stock
    )
    can_manage_orders = is_owner or user_can_manage_orders(
        request.user,
        shop,
    )
    can_view_rider_map = is_owner or bool(
        staff_record and staff_record.role == "MANAGER"
    )
    can_view_financials = is_owner or bool(
        staff_record
        and staff_record.role == "MANAGER"
        and staff_record.can_view_reports
    )

    product_assignments = (
        ProductShop.objects
        .filter(shop=shop)
        .select_related("product")
        .order_by(
            "product__product_name"
        )
    )

    shop_orders = (
        ShopOrder.objects
        .filter(shop=shop)
        .select_related("order")
        .order_by("-created_at")
    )

    job_openings = (
        shop.job_openings
        .all()
        .order_by("-created_at")[:5]
    )

    job_applications = (
        shop.job_applications
        .select_related("job_opening")
        .order_by("-created_at")[:5]
    )

    total_products = product_assignments.count()

    active_products = product_assignments.filter(
        is_active=True,
        product__is_active=True
    ).count()

    total_stock = (
        product_assignments.aggregate(
            total=Sum("stock")
        )["total"]
        or 0
    )

    total_orders = shop_orders.count()

    pending_orders = shop_orders.filter(
        status__in=[
            "PLACED",
            "CONFIRMED",
            "PACKING",
            "READY",
        ]
    ).count()

    delivered_orders = shop_orders.filter(
        status="DELIVERED"
    ).count()

    return render(
        request,
        "seller/shop_detail.html",
        {
            "shop": shop,
            "product_assignments": product_assignments,
            "shop_orders": shop_orders[:10],
            "job_openings": job_openings,
            "job_applications": job_applications,
            "is_shop_owner": is_owner,
            "can_manage_staff": can_manage_staff,
            "can_manage_stock": can_manage_stock,
            "can_manage_orders": can_manage_orders,
            "can_view_rider_map": can_view_rider_map,
            "can_view_financials": can_view_financials,
            "total_products": total_products,
            "active_products": active_products,
            "total_stock": total_stock,
            "total_orders": total_orders,
            "pending_orders": pending_orders,
            "delivered_orders": delivered_orders,
            "shared_rider_payment_report": (
                _shared_rider_payment_report(shop)
                if can_view_financials
                else []
            ),
            "shared_rider_fee_policy": (
                _delivery_sharing_settings()
                if can_view_financials
                else None
            ),
        }
    )

from .forms import ShopStaffForm

from django.core.exceptions import PermissionDenied
from .models import ShopStaff
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db import transaction

from .models import (
    Shop,
    ShopOrder,
    OrderItem,
    OrderStatusHistory,
    ShopStaff,
)

from .utils import (
    user_can_access_shop,
    user_can_manage_orders,
    user_can_change_status,
)


def _shared_rider_payment_report(shop, now=None):
    now = now or timezone.now()
    today_start = timezone.localtime(now).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)
    payments = list(
        SharedRiderDeliveryPayment.objects.filter(
            Q(paying_shop=shop) | Q(rider_home_shop=shop),
            delivered_at__gte=month_start,
        )
        .select_related(
            "rider__user",
            "paying_shop",
            "rider_home_shop",
            "shop_order__order",
        )
        .order_by("-delivered_at", "-id")
    )
    by_rider = {}
    for payment in payments:
        row = by_rider.setdefault(
            payment.rider_id,
            {
                "rider": payment.rider,
                "paid_today": Decimal("0.00"),
                "paid_7_days": Decimal("0.00"),
                "paid_30_days": Decimal("0.00"),
                "received_today": Decimal("0.00"),
                "received_7_days": Decimal("0.00"),
                "received_30_days": Decimal("0.00"),
                "payments": [],
            },
        )
        is_paying_shop = payment.paying_shop_id == shop.pk
        direction = "paid" if is_paying_shop else "received"
        row[f"{direction}_30_days"] += payment.amount
        if payment.delivered_at >= week_start:
            row[f"{direction}_7_days"] += payment.amount
        if payment.delivered_at >= today_start:
            row[f"{direction}_today"] += payment.amount
        row["payments"].append(payment)
    return sorted(
        by_rider.values(),
        key=lambda row: (
            -row["paid_30_days"],
            -row["received_30_days"],
            row["rider"].user.username.casefold(),
        ),
    )


def _staff_efficiency_card(shop, role, current_staff, now=None):
    now = now or timezone.now()
    period_start = now - timedelta(days=30)
    team = list(
        ShopStaff.objects.filter(
            shop=shop,
            role=role,
            is_active=True,
        ).select_related("user").order_by("user__username")
    )
    team_by_id = {member.pk: member for member in team}
    metrics = {
        member.pk: {
            "staff": member,
            "completed_order_count": 0,
            "total_order_value": Decimal("0.00"),
            "total_quantity": 0,
            "elapsed_seconds": 0,
            "timed_order_count": 0,
        }
        for member in team
    }
    assignment_field = "assigned_packer_id" if role == "PACKER" else "assigned_rider_id"
    assignments = list(
        ShopOrder.objects.filter(
            shop=shop,
            **{f"{assignment_field}__in": list(team_by_id)},
        )
        .select_related("order")
        .prefetch_related("order_items", "status_history")
    )

    for shop_order in assignments:
        history = list(shop_order.status_history.all())
        if role == "PACKER":
            completed_at = max(
                (
                    event.created_at
                    for event in history
                    if event.status == "READY"
                ),
                default=None,
            )
            started_at = min(
                (
                    event.created_at
                    for event in history
                    if event.status == "PACKING"
                ),
                default=None,
            )
            staff_id = shop_order.assigned_packer_id
        else:
            completed_at = shop_order.delivery_completed_at
            started_at = shop_order.rider_accepted_at or min(
                (
                    event.created_at
                    for event in history
                    if event.status == "RIDER_ASSIGNED"
                ),
                default=None,
            )
            staff_id = shop_order.assigned_rider_id
        if (
            staff_id not in metrics
            or completed_at is None
            or completed_at < period_start
        ):
            continue

        staff_metrics = metrics[staff_id]
        staff_metrics["completed_order_count"] += 1
        staff_metrics["total_order_value"] += shop_order.total_amount
        staff_metrics["total_quantity"] += sum(
            (
                item.quantity
                if role == "PACKER"
                else max(0, item.quantity - item.returned_quantity)
                for item in shop_order.order_items.all()
            ),
            0,
        )
        if started_at and started_at < completed_at:
            staff_metrics["elapsed_seconds"] += (
                completed_at - started_at
            ).total_seconds()
            staff_metrics["timed_order_count"] += 1

    ranked_metrics = []
    for staff_metrics in metrics.values():
        elapsed_seconds = staff_metrics["elapsed_seconds"]
        elapsed_hours = Decimal(str(elapsed_seconds)) / Decimal("3600") if elapsed_seconds else Decimal("0")
        staff_metrics["value_per_hour"] = (
            staff_metrics["total_order_value"] / elapsed_hours
            if elapsed_hours
            else Decimal("0.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        staff_metrics["quantity_per_hour"] = (
            Decimal(staff_metrics["total_quantity"]) / elapsed_hours
            if elapsed_hours
            else Decimal("0.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        staff_metrics["average_minutes"] = (
            Decimal(elapsed_seconds)
            / Decimal(staff_metrics["timed_order_count"])
            / Decimal("60")
            if staff_metrics["timed_order_count"]
            else None
        )
        ranked_metrics.append(staff_metrics)

    ranked_metrics.sort(
        key=lambda row: (
            -row["value_per_hour"],
            -row["quantity_per_hour"],
            -row["completed_order_count"],
            row["average_minutes"]
            if row["average_minutes"] is not None
            else Decimal("Infinity"),
            row["staff"].user.username.casefold(),
        )
    )
    current_rank = next(
        (
            index
            for index, row in enumerate(ranked_metrics, start=1)
            if row["staff"].pk == current_staff.pk
        ),
        None,
    )
    current_metrics = metrics.get(current_staff.pk, {
        "completed_order_count": 0,
        "total_order_value": Decimal("0.00"),
        "total_quantity": 0,
        "value_per_hour": Decimal("0.00"),
        "quantity_per_hour": Decimal("0.00"),
        "average_minutes": None,
        "timed_order_count": 0,
    })
    salary_settings = _delivery_sharing_settings()
    if role == "PACKER":
        base_salary = salary_settings.packer_monthly_base_salary
        order_bonus = salary_settings.packer_completed_order_bonus
    else:
        base_salary = salary_settings.rider_monthly_base_salary
        order_bonus = salary_settings.rider_completed_order_bonus
    current_metrics.update({
        "rank": current_rank,
        "team_size": len(ranked_metrics),
        "salary_suggestion": (
            base_salary
            + order_bonus * current_metrics["completed_order_count"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "salary_policy_configured": base_salary > 0 or order_bonus > 0,
        "base_salary": base_salary,
        "order_bonus": order_bonus,
        "shared_rider_income_30_days": (
            SharedRiderDeliveryPayment.objects.filter(
                rider=current_staff,
                delivered_at__gte=period_start,
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        ) if role == "RIDER" else Decimal("0.00"),
    })
    return current_metrics


def get_shop_staff(user, shop):

    return ShopStaff.objects.filter(
        user=user,
        shop=shop,
        is_active=True
    ).first()

def get_owned_shop(request, shop_id):

    return get_object_or_404(
        Shop,
        id=shop_id,
        owner=request.user
    )
from django.db import transaction

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password

User = get_user_model()
@login_required
def shop_staff_create(request, shop_id=None):

    if shop_id is None:
        shops = Shop.objects.filter(owner=request.user).order_by("name")

        if not shops.exists():
            messages.warning(request, "Create a shop first before adding staff.")
            return redirect("shop_add")

        if shops.count() == 1:
            shop = shops.first()
        else:
            messages.info(request, "Select a shop to add staff.")
            return redirect("shop_staff_list")

    else:
        shop = get_object_or_404(
            Shop,
            id=shop_id,
            owner=request.user
        )

    if request.method == "POST":

        form = ShopStaffForm(request.POST, shop=shop)
        form.fields["existing_staff_user"].queryset = User.objects.filter(
            shop_staff_memberships__shop__owner=request.user,
            shop_staff_memberships__is_active=True,
            shop_staff_memberships__is_public_rider=False,
        ).exclude(
            pk__in=ShopStaff.objects.filter(
                shop=shop,
            ).values("user_id"),
        ).distinct().order_by("username")

        if form.is_valid():

            staff = form.save(commit=False)

            staff.shop = shop
            staff.is_active = True

            staff.save()

            messages.success(
                request,
                f"{staff.user.username} added to {shop.name}."
            )

            return redirect(
                "shop_staff_list_shop",
                shop_id=shop.id
            )

    else:

        form = ShopStaffForm(shop=shop)
        form.fields["existing_staff_user"].queryset = User.objects.filter(
            shop_staff_memberships__shop__owner=request.user,
            shop_staff_memberships__is_active=True,
            shop_staff_memberships__is_public_rider=False,
        ).exclude(
            pk__in=ShopStaff.objects.filter(
                shop=shop,
            ).values("user_id"),
        ).distinct().order_by("username")

    return render(
        request,
        "seller/shop_staff_form.html",
        {
            "form": form,
            "shop": shop,
        }
    )

@login_required
def shop_staff_list(request):

    shops = Shop.objects.filter(
        owner=request.user
    )

    staff = ShopStaff.objects.filter(
        shop__in=shops
    ).select_related(
        "shop",
        "user",
    )

    return render(
        request,
        "shop/staff_list.html",
        {
            "staff": staff,
            "shops": shops,
        }
    )

@login_required
@transaction.atomic
def shop_staff_edit(request, shop_id, staff_id):

    shop = get_object_or_404(
        Shop,
        id=shop_id,
        owner=request.user,
    )

    staff = get_object_or_404(
        ShopStaff.objects.select_for_update().select_related("user"),
        id=staff_id,
        shop=shop
    )
    owner_shops = list(
        Shop.objects.filter(owner=request.user).order_by("name")
    )
    owner_shop_ids = {owner_shop.pk for owner_shop in owner_shops}
    memberships_by_shop = {
        membership.shop_id: membership
        for membership in ShopStaff.objects.filter(
            user=staff.user,
            shop_id__in=owner_shop_ids,
            is_public_rider=False,
        )
    }

    if request.method == "POST":
        selected_shop_values = request.POST.getlist("accessible_shop_ids")
        try:
            selected_shop_ids = {int(value) for value in selected_shop_values}
        except (TypeError, ValueError):
            messages.error(request, "Select valid shops for this staff member.")
            return redirect(
                "shop_staff_edit",
                shop_id=shop.id,
                staff_id=staff.id,
            )
        if not selected_shop_ids.issubset(owner_shop_ids):
            messages.error(request, "You can only assign staff to shops you own.")
            return redirect(
                "shop_staff_edit",
                shop_id=shop.id,
                staff_id=staff.id,
            )
        role_choices = {
            role_value
            for role_value, _label in ShopStaff.ROLE_CHOICES
        }
        selected_shop_roles = {}
        for selected_shop_id in selected_shop_ids:
            if selected_shop_id == shop.pk:
                continue
            role_value = (
                request.POST.get(f"shop_role_{selected_shop_id}") or ""
            ).strip()
            existing_membership = memberships_by_shop.get(selected_shop_id)
            default_role = (
                existing_membership.role
                if existing_membership
                else staff.role
            )
            role_value = role_value or default_role
            if role_value not in role_choices:
                messages.error(request, "Select a valid role for each shop.")
                return redirect(
                    "shop_staff_edit",
                    shop_id=shop.id,
                    staff_id=staff.id,
                )
            selected_shop_roles[selected_shop_id] = role_value

        role_map = {
            "STAFF": "MANAGER",
            "PACKER": "PACKER",
            "MANAGER": "MANAGER",
            "PURCHASE_MANAGER": "PURCHASE_MANAGER",
            "SUPERVISOR": "MANAGER",
            "DELIVERY": "DELIVERY",
            "RIDER": "RIDER",
            "ORDER": "ORDER",
            "ORDER MANAGER": "ORDER",
        }

        role_value = (request.POST.get("role") or "MANAGER").strip().upper()
        staff.role = role_map.get(role_value, role_value)

        staff.can_confirm_orders = (
            "can_confirm_orders" in request.POST
        )

        staff.can_pack_orders = (
            "can_pack_orders" in request.POST
        )

        staff.can_change_order_status = (
            "can_change_order_status" in request.POST
        )

        staff.can_cancel_orders = (
            "can_cancel_orders" in request.POST
        )

        staff.can_manage_stock = (
            "can_manage_stock" in request.POST
        )
        staff.can_confirm_rider_payments = (
            "can_confirm_rider_payments" in request.POST
        )

        staff.can_manage_staff = (
            "can_manage_staff" in request.POST
        )

        staff.can_assign_staff = (
            "can_assign_staff" in request.POST
        )

        staff.is_active = (
            "is_active" in request.POST
        )
        upi_id = (request.POST.get("upi_id") or "").strip()
        if upi_id and not re.fullmatch(
            r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}",
            upi_id,
        ):
            messages.error(request, "Enter a valid staff UPI ID.")
            return redirect(
                "shop_staff_edit",
                shop_id=shop.id,
                staff_id=staff.id,
            )
        staff.upi_id = upi_id

        staff.save()

        permission_fields = (
            "can_manage_products",
            "can_assign_staff",
            "can_view_reports",
            "can_view_orders",
            "can_confirm_orders",
            "can_pack_orders",
            "can_change_order_status",
            "can_manage_stock",
            "can_manage_staff",
            "can_cancel_orders",
            "can_confirm_rider_payments",
        )
        for owner_shop in owner_shops:
            membership = memberships_by_shop.get(owner_shop.pk)
            if owner_shop.pk in selected_shop_ids:
                selected_role = (
                    staff.role
                    if owner_shop.pk == shop.pk
                    else selected_shop_roles[owner_shop.pk]
                )
                if membership is None:
                    membership = ShopStaff.objects.create(
                        shop=owner_shop,
                        user=staff.user,
                        role=selected_role,
                        **{
                            field: getattr(staff, field)
                            for field in permission_fields
                        },
                    )
                    memberships_by_shop[owner_shop.pk] = membership
                elif (
                    not membership.is_active
                    or membership.role != selected_role
                ):
                    membership.is_active = True
                    membership.role = selected_role
                    membership.save(
                        update_fields=["is_active", "role", "updated_at"],
                    )
                continue

            if membership is None or not membership.is_active:
                continue
            if membership.is_public_rider:
                continue

            has_order_history = ShopOrder.objects.filter(
                Q(assigned_rider=membership) | Q(assigned_packer=membership),
            ).exists()
            has_payment_history = (
                CODPaymentApprovalRequest.objects.filter(rider=membership).exists()
                or RiderCashLedgerEntry.objects.filter(rider=membership).exists()
                or SharedRiderDeliveryPayment.objects.filter(rider=membership).exists()
            )
            staff_wallet = ShopStaffWallet.objects.filter(
                staff=membership,
            ).first()
            has_wallet_history = bool(
                staff_wallet
                and (
                    staff_wallet.balance != 0
                    or staff_wallet.transactions.exists()
                )
            )
            has_hr_history = (
                StaffDutySession.objects.filter(staff=membership).exists()
                or StaffLeaveApplication.objects.filter(staff=membership).exists()
                or StaffResignationRequest.objects.filter(staff=membership).exists()
                or StaffPayrollRecord.objects.filter(staff=membership).exists()
            )
            if (
                has_order_history
                or has_payment_history
                or has_wallet_history
                or has_hr_history
            ):
                membership.is_active = False
                membership.save(update_fields=["is_active", "updated_at"])
            else:
                membership.delete()

        messages.success(
            request,
            f"{staff.user.username} staff access and permissions updated successfully."
        )

        return redirect(
            "shop_staff_list_shop",
            shop_id=shop.id
        )

    shop_access_options = [
        {
            "shop": owner_shop,
            "has_access": bool(
                memberships_by_shop.get(owner_shop.pk)
                and memberships_by_shop[owner_shop.pk].is_active
            ),
            "role": (
                memberships_by_shop[owner_shop.pk].role
                if memberships_by_shop.get(owner_shop.pk)
                else staff.role
            ),
            "role_choices": ShopStaff.ROLE_CHOICES,
        }
        for owner_shop in owner_shops
    ]

    return render(
        request,
        "seller/shop_staff_edit.html",
        {
            "shop": shop,
            "staff": staff,
            "shop_access_options": shop_access_options,
        }
    )


@login_required
def staff_payment_settings(request):
    staff = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
    ).select_related("shop").first()
    if not staff:
        raise PermissionDenied
    form = ShopStaffPaymentSettingsForm(
        request.POST or None,
        instance=staff,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your personal UPI payment settings were saved.")
        return redirect("staff_payment_settings")
    return render(request, "seller/staff_payment_settings.html", {
        "form": form,
        "staff": staff,
    })

@login_required
def staff_dashboard(request):

    staff_memberships = (
        ShopStaff.objects
        .select_related("shop")
        .filter(
            user=request.user,
            is_active=True,
            is_public_rider=False,
        )
    )

    if not staff_memberships.exists():
        messages.error(request, "You are not assigned to any shop.")
        return redirect("dashboard")

    staff = staff_memberships.first()
    shop = staff.shop

    product_assignments = (
        ProductShop.objects
        .filter(shop=shop)
        .select_related("product")
        .order_by("product__product_name")
    )
    inventory_assignments = list(product_assignments[:10])
    inventory_assignment_ids = [
        assignment.pk for assignment in inventory_assignments
    ]
    inventory_forecasts = {
        forecast.product_shop_id: forecast
        for forecast in ShopProductForecast.objects.filter(
            product_shop_id__in=inventory_assignment_ids,
        )
    }
    recent_purchase_by_assignment = {}
    for purchase_item in (
        VendorPurchaseItem.objects
        .filter(product_shop_id__in=inventory_assignment_ids)
        .select_related("purchase__vendor")
        .order_by("-purchase__purchase_date", "-purchase_id", "-pk")
    ):
        recent_purchase_by_assignment.setdefault(
            purchase_item.product_shop_id,
            purchase_item,
        )
    for assignment in inventory_assignments:
        forecast = inventory_forecasts.get(assignment.pk)
        target_stock = (
            forecast.recommended_stock
            if forecast
            else Decimal("5.0000")
        )
        assignment.inventory_shortage = max(
            Decimal("0.0000"),
            target_stock - assignment.stock,
        )
        assignment.shortage_display = _quantity_unit_calculation(
            assignment.inventory_shortage,
            assignment.product.unit,
        )
        assignment.is_low_stock = assignment.inventory_shortage > 0
        assignment.recent_purchase = recent_purchase_by_assignment.get(
            assignment.pk,
        )

    shop_orders = (
        ShopOrder.objects
        .filter(shop=shop)
        .select_related("order")
        .order_by("-created_at")
    )

    total_products = product_assignments.count()
    active_products = product_assignments.filter(is_active=True, product__is_active=True).count()
    total_stock = product_assignments.aggregate(total_stock=Sum("stock"))["total_stock"] or 0
    pending_orders = shop_orders.filter(status__in=["PLACED", "CONFIRMED", "PACKING", "READY"]).count()
    delivered_orders = shop_orders.filter(status="DELIVERED").count()

    return render(
        request,
        "seller/staff_dashboard.html",
        {
            "staff_memberships": staff_memberships,
            "staff": staff,
            "shop": shop,
            "can_manage_stock": staff.can_manage_stock,
            "can_manage_staff": staff.can_manage_staff,
            "can_manage_orders": staff.can_view_orders,
            "can_review_rider_payments": ShopStaff.objects.filter(
                user=request.user,
                is_active=True,
                is_public_rider=False,
                role="PAYMENT_MANAGER",
                can_confirm_rider_payments=True,
            ).exists(),
            "product_assignments": inventory_assignments,
            "shop_orders": shop_orders[:10],
            "total_products": total_products,
            "active_products": active_products,
            "total_stock": total_stock,
            "pending_orders": pending_orders,
            "delivered_orders": delivered_orders,
            "total_orders": shop_orders.count(),
        }
    )


def _require_shop_pos_access(request, shop):
    is_owner = shop.owner_id == request.user.pk or request.user.is_superuser
    staff_membership = ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        is_active=True,
        is_public_rider=False,
    ).first()
    if not is_owner and (
        not staff_membership or not staff_membership.can_view_orders
    ):
        raise PermissionDenied


def shop_product_qr_detail(request, shop_id, product_shop_id):
    assignment = get_object_or_404(
        ProductShop.objects.select_related("product", "shop").filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        ),
        pk=product_shop_id,
        shop_id=shop_id,
        is_active=True,
        product__is_active=True,
    )
    is_b2b = _is_b2b_customer(request)
    cart = _address_scoped_cart(request)[0]
    cart_quantity = _cart_quantity(cart, assignment.pk)
    bulk_price = assignment.effective_bulk_price_for_customer(is_b2b=is_b2b)
    bulk_unit = assignment.bulk_unit_for_customer(is_b2b=is_b2b)
    image_urls = assignment.display_image_urls
    return render(
        request,
        "market/product_qr_detail.html",
        {
            "assignment": assignment,
            "product": assignment.product,
            "shop": assignment.shop,
            "is_b2b": is_b2b,
            "price": assignment.effective_price_for_customer(is_b2b=is_b2b),
            "unit": assignment.unit_for_customer(is_b2b=is_b2b),
            "stock": assignment.stock_for_customer(is_b2b=is_b2b),
            "image_url": image_urls[0] if image_urls else "",
            "image_urls": image_urls,
            "bulk_price": bulk_price,
            "bulk_unit": bulk_unit,
            "bulk_min_quantity": assignment.bulk_min_quantity,
            "bulk_cart_quantity": _cart_quantity(
                cart,
                assignment.pk,
                is_bulk_order=True,
            ),
            "has_bulk_offer": bulk_price is not None and bool(bulk_unit),
            "cart_quantity": cart_quantity,
        },
    )


@login_required
def shop_product_qr_label(request, shop_id, product_shop_id=None):
    shop = get_object_or_404(Shop, pk=shop_id)
    _require_shop_pos_access(request, shop)
    all_products = product_shop_id is None
    if all_products:
        assignments = list(
            ProductShop.objects.select_related("product")
            .filter(shop=shop, is_active=True, product__is_active=True)
            .order_by("product__product_name", "pk")
        )
        if not assignments:
            messages.info(request, "This shop has no active products to print.")
            return redirect("shop_point_of_sale", shop_id=shop.pk)
    else:
        assignments = [
            get_object_or_404(
                ProductShop.objects.select_related("product"),
                pk=product_shop_id,
                shop=shop,
                is_active=True,
                product__is_active=True,
            )
        ]

    paper_sizes = {
        "A4": (Decimal("210"), Decimal("297")),
        "A5": (Decimal("148"), Decimal("210")),
        "LETTER": (Decimal("216"), Decimal("279")),
        "CUSTOM": None,
    }
    paper_size = request.GET.get("paper_size", "A4").upper()
    if paper_size not in paper_sizes:
        messages.error(request, "Choose a valid print page size. A4 was selected.")
        paper_size = "A4"

    def read_dimension(parameter, default, minimum, maximum, label):
        raw_value = request.GET.get(parameter, str(default))
        try:
            value = Decimal(raw_value)
        except (InvalidOperation, TypeError, ValueError):
            value = Decimal("NaN")
        if not value.is_finite() or not minimum <= value <= maximum:
            messages.error(
                request,
                f"{label} must be between {minimum} mm and {maximum} mm. "
                f"The default {default} mm was used.",
            )
            return default
        return value.normalize()

    label_width_mm = read_dimension(
        "label_width_mm",
        Decimal("50"),
        Decimal("25"),
        Decimal("200"),
        "Label width",
    )
    label_height_mm = read_dimension(
        "label_height_mm",
        Decimal("65"),
        Decimal("45"),
        Decimal("250"),
        "Label height",
    )
    if all_products:
        label_count = len(assignments)
    else:
        try:
            label_count = int(request.GET.get("label_count", "1"))
        except (TypeError, ValueError):
            label_count = 0
        if not 1 <= label_count <= 500:
            messages.error(request, "Choose between 1 and 500 QR labels. One label was selected.")
            label_count = 1

    if paper_sizes[paper_size] is None:
        paper_width_mm = read_dimension(
            "paper_width_mm",
            Decimal("210"),
            Decimal("60"),
            Decimal("500"),
            "Custom page width",
        )
        paper_height_mm = read_dimension(
            "paper_height_mm",
            Decimal("297"),
            Decimal("60"),
            Decimal("500"),
            "Custom page height",
        )
    else:
        paper_width_mm, paper_height_mm = paper_sizes[paper_size]
    max_label_width = paper_width_mm - Decimal("10")
    max_label_height = paper_height_mm - Decimal("10")
    if label_width_mm > max_label_width:
        messages.error(
            request,
            "The label is wider than the selected page. Its width was reduced to fit.",
        )
        label_width_mm = max_label_width
    if label_height_mm > max_label_height:
        messages.error(
            request,
            "The label is taller than the selected page. Its height was reduced to fit.",
        )
        label_height_mm = max_label_height
    max_qr_size_mm = min(
        label_width_mm - Decimal("8"),
        label_height_mm - Decimal("28"),
    )
    qr_size_mm = read_dimension(
        "qr_size_mm",
        min(Decimal("35"), max_qr_size_mm),
        Decimal("10"),
        Decimal("150"),
        "QR size",
    )
    if qr_size_mm > max_qr_size_mm:
        messages.error(
            request,
            f"QR size must fit inside the label. It was reduced to {max_qr_size_mm} mm.",
        )
        qr_size_mm = max_qr_size_mm

    label_entries = []
    for assignment in assignments:
        product_url = request.build_absolute_uri(
            reverse(
                "shop_product_qr_detail",
                kwargs={"shop_id": shop.pk, "product_shop_id": assignment.pk},
            )
        )
        entry = {
            "assignment": assignment,
            "qr_payload": product_url,
            "qr_code_data_uri": "",
        }
        entry["qr_code_data_uri"] = _qr_code_data_uri(
            entry["qr_payload"],
            box_size=8,
        )
        copies = 1 if all_products else label_count
        label_entries.extend([entry] * copies)
    return render(
        request,
        "shop/product_qr_label.html",
        {
            "shop": shop,
            "assignment": assignments[0] if len(assignments) == 1 else None,
            "qr_payload": label_entries[0]["qr_payload"],
            "qr_code_data_uri": label_entries[0]["qr_code_data_uri"],
            "label_entries": label_entries,
            "all_products": all_products,
            "product_count": len(assignments),
            "label_count": label_count,
            "label_width_mm": label_width_mm,
            "label_height_mm": label_height_mm,
            "qr_size_mm": qr_size_mm,
            "max_qr_size_mm": max_qr_size_mm,
            "paper_size": paper_size,
            "paper_width_mm": paper_width_mm,
            "paper_height_mm": paper_height_mm,
        },
    )


@login_required
def shop_point_of_sale(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    _require_shop_pos_access(request, shop)

    cart_session_key = f"shop_pos_cart_{shop.pk}"
    cart = request.session.get(cart_session_key, {})
    if not isinstance(cart, dict):
        cart = {}

    if request.method == "POST":
        action = request.POST.get("action", "")
        scanned_assignment_id = None
        if action == "scan_item":
            scanned_code = request.POST.get("scan_code", "").strip()
            scanned_product = re.fullmatch(
                r"sellerhelper-pos:(\d+):(\d+)",
                scanned_code,
            )
            scanned_product_url = urlsplit(scanned_code)
            scanned_product_path = re.fullmatch(
                r"/shop/(\d+)/products/(\d+)/qr/",
                scanned_product_url.path,
            )
            if scanned_product:
                scanned_shop_id, assignment_id_from_scan = scanned_product.groups()
            elif (
                scanned_product_path
                and (
                    (not scanned_product_url.scheme and not scanned_product_url.netloc)
                    or (
                        scanned_product_url.scheme in {"http", "https"}
                        and scanned_product_url.netloc.lower()
                        == request.get_host().lower()
                    )
                )
            ):
                scanned_shop_id, assignment_id_from_scan = scanned_product_path.groups()
            else:
                scanned_shop_id, assignment_id_from_scan = None, None
            if not assignment_id_from_scan or int(scanned_shop_id) != shop.pk:
                messages.error(request, "That QR code does not belong to this shop.")
                return redirect("shop_point_of_sale", shop_id=shop.pk)
            action = "add_item"
            scanned_assignment_id = assignment_id_from_scan

        if action in {"add_item", "set_quantity", "remove_item"}:
            assignment_id = (
                scanned_assignment_id
                if scanned_assignment_id is not None
                else request.POST.get("product_shop_id", "")
            )
            assignment = get_object_or_404(
                ProductShop.objects.select_related("product"),
                pk=assignment_id,
                shop=shop,
                is_active=True,
                product__is_active=True,
            )
            key = str(assignment.pk)
            try:
                quantity = (
                    1
                    if scanned_assignment_id is not None
                    else int(request.POST.get("quantity", "1"))
                )
            except (TypeError, ValueError):
                quantity = 0
            if action == "remove_item" or quantity == 0:
                cart.pop(key, None)
                messages.success(request, f"{assignment.product.product_name} removed from the sale.")
            elif quantity < 0:
                messages.error(request, "Quantity must be zero or greater.")
            else:
                current_stock = assignment.stock_for_customer(is_b2b=False)
                try:
                    current_quantity = int(cart.get(key, 0))
                except (TypeError, ValueError):
                    current_quantity = 0
                new_quantity = (
                    current_quantity + quantity
                    if action == "add_item"
                    else quantity
                )
                if new_quantity > current_stock:
                    messages.error(
                        request,
                        f"Only {current_stock} unit(s) of {assignment.product.product_name} are in stock.",
                    )
                else:
                    cart[key] = new_quantity
                    messages.success(
                        request,
                        f"{assignment.product.product_name} quantity updated.",
                    )
            request.session[cart_session_key] = cart
            return redirect(
                f"{reverse('shop_point_of_sale', kwargs={'shop_id': shop.pk})}"
                + (f"?q={quote(request.GET.get('q', ''))}" if request.GET.get("q") else "")
            )

        if action == "place_sale":
            fulfillment_type = request.POST.get("fulfillment_type", "")
            payment_type = request.POST.get("payment_type", "")
            customer_name = request.POST.get("customer_name", "").strip()[:200]
            customer_phone = request.POST.get("customer_phone", "").strip()[:40]
            delivery_address = request.POST.get("delivery_address", "").strip()
            delivery_pincode = request.POST.get("delivery_pincode", "").strip()[:20]
            delivery_latitude_raw = request.POST.get("delivery_latitude", "").strip()
            delivery_longitude_raw = request.POST.get("delivery_longitude", "").strip()
            if fulfillment_type not in {
                ShopOrder.FULFILLMENT_COUNTER,
                ShopOrder.FULFILLMENT_DELIVERY,
            }:
                messages.error(request, "Choose customer carry-out or home delivery.")
            elif payment_type not in {"CASH", "UPI", "CARD", "OTHER"}:
                messages.error(request, "Choose a valid payment method.")
            elif not cart:
                messages.error(request, "Add at least one in-stock product before placing the sale.")
            elif fulfillment_type == ShopOrder.FULFILLMENT_DELIVERY and (
                not customer_name
                or not customer_phone
                or not delivery_address
                or not delivery_latitude_raw
                or not delivery_longitude_raw
            ):
                messages.error(
                    request,
                    "Home delivery requires the customer's name, phone, address, and map coordinates.",
                )
            else:
                try:
                    delivery_latitude = (
                        Decimal(delivery_latitude_raw)
                        if delivery_latitude_raw
                        else None
                    )
                    delivery_longitude = (
                        Decimal(delivery_longitude_raw)
                        if delivery_longitude_raw
                        else None
                    )
                    if (
                        fulfillment_type == ShopOrder.FULFILLMENT_DELIVERY
                        and (
                            delivery_latitude is None
                            or delivery_longitude is None
                            or not delivery_latitude.is_finite()
                            or not delivery_longitude.is_finite()
                            or not Decimal("-90") <= delivery_latitude <= Decimal("90")
                            or not Decimal("-180") <= delivery_longitude <= Decimal("180")
                        )
                    ):
                        raise ValueError
                except (InvalidOperation, TypeError, ValueError):
                    messages.error(request, "Enter valid customer delivery coordinates.")
                else:
                    now = timezone.now()
                    delivery_location = (
                        {
                            "lat": float(delivery_latitude),
                            "lng": float(delivery_longitude),
                        }
                        if fulfillment_type == ShopOrder.FULFILLMENT_DELIVERY
                        else {}
                    )
                    if (
                        fulfillment_type == ShopOrder.FULFILLMENT_DELIVERY
                        and not shop_delivers_to_location(
                            shop,
                            lat=delivery_latitude,
                            lng=delivery_longitude,
                            pincode=delivery_pincode,
                            allow_scheduled_delivery=shop.allow_scheduled_delivery,
                        )
                    ):
                        messages.error(
                            request,
                            "The customer's map pin is outside this shop's delivery area or the shop is not accepting deliveries.",
                        )
                    else:
                        try:
                            requested_quantities = {
                                int(assignment_id): int(quantity)
                                for assignment_id, quantity in cart.items()
                                if int(quantity) > 0
                            }
                        except (TypeError, ValueError):
                            requested_quantities = {}
                            messages.error(request, "The saved sale cart is invalid; please review it.")
                        if requested_quantities:
                            with transaction.atomic():
                                assignments = list(
                                    ProductShop.objects.select_for_update()
                                    .select_related("product", "shop")
                                    .filter(
                                        pk__in=requested_quantities,
                                        shop=shop,
                                        is_active=True,
                                        product__is_active=True,
                                    )
                                    .filter(
                                        Q(product__expiry_management_enabled=False)
                                        | Q(expires_at__isnull=True)
                                        | Q(expires_at__gt=now)
                                    )
                                    .order_by("pk")
                                )
                                if len(assignments) != len(requested_quantities):
                                    messages.error(
                                        request,
                                        "One or more cart products are no longer available. Review the sale cart.",
                                    )
                                else:
                                    items = []
                                    unavailable = []
                                    for assignment in assignments:
                                        quantity = requested_quantities[assignment.pk]
                                        stock = assignment.stock_for_customer(is_b2b=False)
                                        price = assignment.effective_price_for_customer(is_b2b=False)
                                        if quantity > stock:
                                            unavailable.append(
                                                f"{assignment.product.product_name} (only {stock} available)"
                                            )
                                            continue
                                        total = (price * quantity).quantize(
                                            Decimal("0.01"),
                                            rounding=ROUND_HALF_UP,
                                        )
                                        items.append({
                                            "assignment": assignment,
                                            "quantity": quantity,
                                            "price": price,
                                            "unit": assignment.unit_for_customer(is_b2b=False),
                                            "total": total,
                                            "is_bulk_order": False,
                                        })
                                    if unavailable:
                                        messages.error(
                                            request,
                                            "Stock changed; update the sale cart: " + ", ".join(unavailable) + ".",
                                        )
                                    elif items:
                                        subtotal = sum(
                                            (item["total"] for item in items),
                                            Decimal("0.00"),
                                        )
                                        delivery_fee = (
                                            shop.delivery_fee
                                            if fulfillment_type == ShopOrder.FULFILLMENT_DELIVERY
                                            else Decimal("0.00")
                                        )
                                        gst_breakdown = _build_gst_breakdown(
                                            items,
                                            SimpleNamespace(
                                                state_code=shop.gst_state_code,
                                                gstin="",
                                                location=delivery_location,
                                            ),
                                            delivery_fees={shop.pk: delivery_fee},
                                        )
                                        total_amount = (
                                            subtotal
                                            + gst_breakdown["total_gst"]
                                            + gst_breakdown["delivery_total"]
                                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                                        shop_order_number = generate_shop_order_number(shop, now)
                                        tracking_code = _generate_unique_tracking_code()
                                        order_id = f"{tracking_code}-{shop_order_number}"
                                        counter_sale = (
                                            fulfillment_type == ShopOrder.FULFILLMENT_COUNTER
                                        )
                                        order = Order.objects.create(
                                            order_id=order_id,
                                            order_source=Order.SOURCE_POS,
                                            tracking_code=tracking_code,
                                            order_time=now,
                                            order_delivered_time=now if counter_sale else None,
                                            user="Walk-in customer",
                                            customer_name=customer_name,
                                            created_by=request.user,
                                            delivery_address=delivery_address,
                                            delivery_pincode=delivery_pincode,
                                            delivery_location=(
                                                f"{delivery_latitude},{delivery_longitude}"
                                                if delivery_location
                                                else ""
                                            ),
                                            delivery_contact_no=customer_phone,
                                            payment_type=payment_type,
                                            payment_status="PAID",
                                            is_payment_done=True,
                                            payment_amount=total_amount,
                                            finally_received_amount=total_amount,
                                            final_billing_amount=total_amount,
                                            gst_amount=gst_breakdown["total_gst"],
                                            is_valid_order=True,
                                            status="DELIVERED" if counter_sale else "PLACED",
                                            shop=shop,
                                            shop_location=str(shop.location or ""),
                                            shop_delivery_area=str(shop.delivery_area or ""),
                                            shop_delivery_pincode=str(shop.delivery_pincode or ""),
                                            placed_location={},
                                        )
                                        tax_row = gst_breakdown["shops"][shop.pk]
                                        shop_order = ShopOrder.objects.create(
                                            order=order,
                                            shop=shop,
                                            fulfillment_type=fulfillment_type,
                                            pickup_location=_select_shop_pickup_location(shop, order),
                                            shop_order_number=shop_order_number,
                                            total_amount=subtotal,
                                            product_count=sum(
                                                item["quantity"] for item in items
                                            ),
                                            total_weight_kg=Decimal("0.00"),
                                            delivery_fee=delivery_fee,
                                            invoice_number=(
                                                f"GST-{order.order_id}-{shop.pk}"
                                                if shop.gstin
                                                else ""
                                            ),
                                            seller_gstin=tax_row["seller_gstin"],
                                            buyer_gstin=tax_row["buyer_gstin"],
                                            seller_billing_address=tax_row["seller_billing_address"],
                                            seller_state_code=tax_row["seller_state_code"],
                                            buyer_state_code=tax_row["buyer_state_code"],
                                            delivery_hsn_code=tax_row["delivery_hsn_code"],
                                            delivery_gst_rate=tax_row["delivery_gst_rate"],
                                            taxable_amount=tax_row["taxable_amount"],
                                            cgst_amount=tax_row["cgst_amount"],
                                            sgst_amount=tax_row["sgst_amount"],
                                            igst_amount=tax_row["igst_amount"],
                                            delivery_gst_amount=tax_row["delivery_gst_amount"],
                                            status="DELIVERED" if counter_sale else "PLACED",
                                            delivery_completed_at=now if counter_sale else None,
                                            delivery_remark=(
                                                "Customer carried the purchase from the shop."
                                                if counter_sale
                                                else ""
                                            ),
                                        )
                                        item_snapshots = []
                                        estimated_weight_kg = Decimal("0.000")
                                        for item in items:
                                            assignment = item["assignment"]
                                            product = assignment.product
                                            item_tax = item
                                            estimated_weight = (
                                                _estimated_weight_for_quantity(
                                                    item["quantity"],
                                                    item["unit"],
                                                )
                                                if product.is_approximate_weight
                                                else None
                                            )
                                            OrderItem.objects.create(
                                                shop_order=shop_order,
                                                product=product,
                                                product_shop=assignment,
                                                product_name=product.product_name,
                                                sku=product.sku or "",
                                                quantity=item["quantity"],
                                                is_approximate_weight=product.is_approximate_weight,
                                                estimated_weight=estimated_weight,
                                                unit=item["unit"] or "",
                                                price=item["price"],
                                                total=item["total"],
                                                hsn_code=item_tax.get("hsn_code", ""),
                                                gst_rate=item_tax.get("gst_rate", Decimal("0.00")),
                                                taxable_amount=item_tax.get("taxable_amount", item["total"]),
                                                cgst_amount=item_tax.get("cgst_amount", Decimal("0.00")),
                                                sgst_amount=item_tax.get("sgst_amount", Decimal("0.00")),
                                                igst_amount=item_tax.get("igst_amount", Decimal("0.00")),
                                            )
                                            if estimated_weight is not None:
                                                estimated_weight_kg += _weight_in_kilograms(
                                                    estimated_weight,
                                                    item["unit"],
                                                )
                                            item_snapshots.append({
                                                "product_id": product.pk,
                                                "product_shop_id": assignment.pk,
                                                "product_name": product.product_name,
                                                "sku": product.sku or "",
                                                "quantity": item["quantity"],
                                                "unit": item["unit"],
                                                "price": str(item["price"]),
                                                "total": str(item["total"]),
                                                "is_bulk_order": False,
                                            })
                                            assignment.stock -= item["quantity"]
                                            assignment.save(update_fields=["stock"])
                                            StockMovement.objects.create(
                                                product_shop=assignment,
                                                movement_type="SALE",
                                                quantity=-item["quantity"],
                                                reference=order.order_id,
                                                created_by=request.user,
                                            )
                                        shop_order.items = item_snapshots
                                        shop_order.total_weight_kg = estimated_weight_kg.quantize(
                                            Decimal("0.01"),
                                            rounding=ROUND_HALF_UP,
                                        )
                                        shop_order.save(update_fields=["items", "total_weight_kg"])
                                        OrderStatusHistory.objects.create(
                                            shop_order=shop_order,
                                            status=shop_order.status,
                                            remark=(
                                                f"Counter sale completed by {request.user.username}."
                                                if counter_sale
                                                else f"Home delivery order placed at the shop counter by {request.user.username}."
                                            ),
                                        )
                                        if not counter_sale:
                                            reviewer_ids = set(
                                                ShopStaff.objects.filter(
                                                    shop=shop,
                                                    is_active=True,
                                                ).values_list("user_id", flat=True)
                                            )
                                            if shop.owner_id:
                                                reviewer_ids.add(shop.owner_id)
                                            Notification.objects.bulk_create([
                                                Notification(
                                                    user_id=user_id,
                                                    title="New shop counter delivery order",
                                                    message=(
                                                        f"Home delivery order {order.order_id} was placed at the shop counter "
                                                        "and needs preparation."
                                                    ),
                                                    notification_type="ORDER",
                                                    related_url=reverse(
                                                        "shop_order_detail",
                                                        kwargs={"shop_order_id": shop_order.pk},
                                                    ),
                                                )
                                                for user_id in reviewer_ids
                                            ])
                                        request.session.pop(cart_session_key, None)
                                        messages.success(
                                            request,
                                            f"{'Counter sale' if counter_sale else 'Home delivery order'} "
                                            f"{order.order_id} placed. Total paid: ₹{total_amount:.2f}.",
                                        )
                                        return redirect(
                                            "shop_order_detail",
                                            shop_order_id=shop_order.pk,
                                        )

        request.session[cart_session_key] = cart
        return redirect("shop_point_of_sale", shop_id=shop.pk)

    search = request.GET.get("q", "").strip()
    assignments = (
        ProductShop.objects.select_related("product")
        .filter(
            shop=shop,
            is_active=True,
            product__is_active=True,
            stock__gt=0,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
    )
    if search:
        assignments = assignments.filter(
            Q(product__product_name__icontains=search)
            | Q(product__product_title__icontains=search)
            | Q(product__sku__icontains=search)
        )
    product_page = Paginator(assignments.order_by("product__product_name"), 30).get_page(
        request.GET.get("page")
    )

    cart_lines = []
    cleaned_cart = {}
    cart_assignments = {
        assignment.pk: assignment
        for assignment in ProductShop.objects.select_related("product").filter(
            pk__in=[int(key) for key in cart if str(key).isdigit()],
            shop=shop,
            is_active=True,
            product__is_active=True,
        )
    }
    cart_subtotal = Decimal("0.00")
    for assignment_id, raw_quantity in cart.items():
        try:
            quantity = int(raw_quantity)
            assignment = cart_assignments.get(int(assignment_id))
        except (TypeError, ValueError):
            continue
        if quantity <= 0 or assignment is None:
            continue
        cleaned_cart[str(assignment.pk)] = quantity
        price = assignment.effective_price_for_customer(is_b2b=False)
        total = price * quantity
        cart_subtotal += total
        cart_lines.append({
            "assignment": assignment,
            "quantity": quantity,
            "price": price,
            "total": total,
        })
    if cleaned_cart != cart:
        request.session[cart_session_key] = cleaned_cart

    pos_tax_items = [
        {
            "assignment": line["assignment"],
            "quantity": line["quantity"],
            "price": line["price"],
            "total": line["total"],
        }
        for line in cart_lines
    ]
    counter_tax = _build_gst_breakdown(
        pos_tax_items,
        SimpleNamespace(
            state_code=shop.gst_state_code,
            gstin="",
            location={},
        ),
        delivery_fees={shop.pk: Decimal("0.00")},
    )
    delivery_tax = _build_gst_breakdown(
        pos_tax_items,
        SimpleNamespace(
            state_code=shop.gst_state_code,
            gstin="",
            location={},
        ),
        delivery_fees={shop.pk: shop.delivery_fee},
    )
    counter_total = (
        cart_subtotal + counter_tax["total_gst"] + counter_tax["delivery_total"]
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    delivery_total = (
        cart_subtotal + delivery_tax["total_gst"] + delivery_tax["delivery_total"]
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return render(
        request,
        "shop/point_of_sale.html",
        {
            "shop": shop,
            "product_page": product_page,
            "search": search,
            "cart_lines": cart_lines,
            "cart_subtotal": cart_subtotal,
            "counter_total": counter_total,
            "delivery_total": delivery_total,
            "fulfillment_choices": ShopOrder.FULFILLMENT_CHOICES,
        },
    )


STAFF_DUTY_SESSION_MAX_HOURS = 16
STAFF_ONLINE_WINDOW_MINUTES = 5


def _staff_payroll_period(month_value):
    try:
        year, month = (
            [int(part) for part in month_value.split("-", 1)]
            if month_value
            else [timezone.localdate().year, timezone.localdate().month]
        )
        if month < 1 or month > 12:
            raise ValueError
        start_day = date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Choose a valid payroll month in YYYY-MM format.")
    if year < 2000 or year > timezone.localdate().year + 2:
        raise ValueError("Choose a valid payroll year.")
    if month == 12:
        end_day = date(year + 1, 1, 1)
    else:
        end_day = date(year, month + 1, 1)
    start_at = timezone.make_aware(datetime.combine(start_day, datetime.min.time()))
    end_at = timezone.make_aware(datetime.combine(end_day, datetime.min.time()))
    return start_day, end_day, start_at, end_at, (end_day - start_day).days


def _staff_duty_summary(staff, period_start, period_end, *, now=None):
    now = now or timezone.now()
    max_session_length = timedelta(hours=STAFF_DUTY_SESSION_MAX_HOURS)
    expired_sessions = StaffDutySession.objects.filter(
        staff=staff,
        ended_at__isnull=True,
        started_at__lte=now - max_session_length,
    )
    for session in expired_sessions:
        session.ended_at = session.started_at + max_session_length
        session.auto_closed = True
        session.save(update_fields=["ended_at", "auto_closed"])

    sessions = StaffDutySession.objects.filter(
        staff=staff,
        started_at__lt=period_end,
    ).filter(
        Q(ended_at__isnull=True) | Q(ended_at__gt=period_start),
    ).order_by("-started_at")
    hours = Decimal("0.00")
    duty_dates = set()
    for session in sessions:
        actual_end = session.ended_at or now
        actual_end = min(
            actual_end,
            session.started_at + max_session_length,
        )
        cursor = max(session.started_at, period_start)
        actual_end = min(actual_end, period_end)
        if actual_end <= cursor:
            continue
        while cursor < actual_end:
            local_day = timezone.localtime(cursor).date()
            next_day = timezone.make_aware(
                datetime.combine(local_day + timedelta(days=1), datetime.min.time()),
            )
            segment_end = min(actual_end, next_day)
            seconds = Decimal(str((segment_end - cursor).total_seconds()))
            hours += seconds / Decimal("3600")
            if seconds > 0:
                duty_dates.add(local_day)
            cursor = segment_end
    return {
        "duty_hours": hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "duty_dates": duty_dates,
        "duty_days": len(duty_dates),
        "sessions": list(sessions[:20]),
        "open_session": (
            StaffDutySession.objects.filter(staff=staff, ended_at__isnull=True)
            .order_by("-started_at")
            .first()
        ),
    }


def _staff_paid_leave_dates(staff, period_start_day, period_end_day):
    paid_dates = set()
    applications = StaffLeaveApplication.objects.filter(
        staff=staff,
        status=StaffLeaveApplication.STATUS_APPROVED,
        is_paid=True,
        start_date__lt=period_end_day,
        end_date__gte=period_start_day,
    )
    for application in applications:
        day = max(application.start_date, period_start_day)
        final_day = min(application.end_date, period_end_day - timedelta(days=1))
        while day <= final_day:
            paid_dates.add(day)
            day += timedelta(days=1)
    return paid_dates


def _calculate_staff_payroll(staff, period):
    period_start_day, period_end_day, period_start, period_end, days_in_month = period
    duty = _staff_duty_summary(staff, period_start, period_end)
    paid_leave_dates = (
        _staff_paid_leave_dates(staff, period_start_day, period_end_day)
        - duty["duty_dates"]
    )
    paid_leave_days = len(paid_leave_dates)
    completed_order_count = ShopOrder.objects.filter(
        shop=staff.shop,
        status="DELIVERED",
        delivery_completed_at__gte=period_start,
        delivery_completed_at__lt=period_end,
    ).filter(
        Q(assigned_rider=staff) | Q(assigned_packer=staff),
    ).values("pk").distinct().count()
    base_salary = staff.monthly_base_salary
    hourly_rate = staff.hourly_rate
    order_bonus = staff.completed_order_bonus
    if staff.salary_method == ShopStaff.SALARY_METHOD_HOURLY:
        base_pay = duty["duty_hours"] * hourly_rate
    elif staff.salary_method == ShopStaff.SALARY_METHOD_MONTHLY_FIXED:
        base_pay = base_salary
    else:
        paid_duty_days = min(
            days_in_month,
            duty["duty_days"] + paid_leave_days,
        )
        base_pay = (
            base_salary
            * Decimal(paid_duty_days)
            / Decimal(days_in_month)
        )
    gross_amount = (
        base_pay + order_bonus * completed_order_count
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "salary_method": staff.salary_method,
        "duty_hours": duty["duty_hours"],
        "duty_days": duty["duty_days"],
        "paid_leave_days": paid_leave_days,
        "completed_order_count": completed_order_count,
        "monthly_base_salary": base_salary,
        "hourly_rate": hourly_rate,
        "completed_order_bonus": order_bonus,
        "gross_amount": gross_amount,
    }


def _complete_due_staff_resignations():
    today = timezone.localdate()
    due_requests = StaffResignationRequest.objects.filter(
        status=StaffResignationRequest.STATUS_APPROVED,
        requested_last_working_date__lt=today,
    ).select_related("staff")
    for resignation in due_requests:
        membership = resignation.staff
        if membership.is_active:
            membership.is_active = False
            membership.save(update_fields=["is_active", "updated_at"])
        StaffDutySession.objects.filter(
            staff=membership,
            ended_at__isnull=True,
        ).update(ended_at=timezone.now())
        resignation.status = StaffResignationRequest.STATUS_COMPLETED
        resignation.save(update_fields=["status", "updated_at"])


@login_required
@require_POST
def staff_duty_clock(request, staff_id):
    _complete_due_staff_resignations()
    staff = get_object_or_404(
        ShopStaff,
        pk=staff_id,
        user=request.user,
        is_public_rider=False,
    )
    action = request.POST.get("action")
    now = timezone.now()
    if action == "clock_in":
        if not staff.is_active:
            raise PermissionDenied
        _staff_duty_summary(staff, now - timedelta(days=1), now + timedelta(seconds=1), now=now)
        if StaffLeaveApplication.objects.filter(
            staff=staff,
            status=StaffLeaveApplication.STATUS_APPROVED,
            start_date__lte=timezone.localdate(),
            end_date__gte=timezone.localdate(),
        ).exists():
            messages.error(request, "You cannot clock in during approved leave.")
        elif StaffDutySession.objects.filter(staff=staff, ended_at__isnull=True).exists():
            messages.warning(request, "You are already clocked in.")
        else:
            StaffDutySession.objects.create(staff=staff, started_at=now)
            messages.success(request, "Duty clock-in recorded.")
    elif action == "clock_out":
        session = StaffDutySession.objects.filter(
            staff=staff,
            ended_at__isnull=True,
        ).order_by("-started_at").first()
        if not session:
            messages.error(request, "There is no open duty session to clock out.")
        else:
            session.ended_at = min(
                now,
                session.started_at + timedelta(hours=STAFF_DUTY_SESSION_MAX_HOURS),
            )
            session.auto_closed = (
                now - session.started_at
                >= timedelta(hours=STAFF_DUTY_SESSION_MAX_HOURS)
            )
            session.save(update_fields=["ended_at", "auto_closed"])
            messages.success(request, "Duty clock-out recorded.")
    else:
        messages.error(request, "Choose a valid clock-in or clock-out action.")
    return redirect("staff_hr_self_service", staff_id=staff.pk)


@login_required
def staff_hr_self_service(request, staff_id):
    _complete_due_staff_resignations()
    staff = get_object_or_404(
        ShopStaff.objects.select_related("shop", "user"),
        pk=staff_id,
        user=request.user,
        is_public_rider=False,
    )
    if not staff.is_active:
        raise PermissionDenied
    leave_form = StaffLeaveApplicationForm()
    resignation_form = StaffResignationForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "apply_leave":
            leave_form = StaffLeaveApplicationForm(request.POST)
            if leave_form.is_valid():
                application = leave_form.save(commit=False)
                application.staff = staff
                application.save()
                messages.success(request, "Leave application submitted for owner review.")
                return redirect("staff_hr_self_service", staff_id=staff.pk)
        elif action == "apply_resignation":
            resignation_form = StaffResignationForm(request.POST)
            existing_request = StaffResignationRequest.objects.filter(
                staff=staff,
                status__in=[
                    StaffResignationRequest.STATUS_PENDING,
                    StaffResignationRequest.STATUS_APPROVED,
                ],
            ).exists()
            if existing_request:
                messages.error(
                    request,
                    "You already have a pending or approved resignation request.",
                )
            elif resignation_form.is_valid():
                resignation = resignation_form.save(commit=False)
                resignation.staff = staff
                resignation.save()
                messages.success(
                    request,
                    "Resignation submitted. Your requested last day must be at least 15 calendar days after submission and is subject to owner review.",
                )
                return redirect("staff_hr_self_service", staff_id=staff.pk)

    today = timezone.localdate()
    month_start, next_month, period_start, period_end, _days = _staff_payroll_period(
        today.strftime("%Y-%m"),
    )
    duty_summary = _staff_duty_summary(staff, period_start, period_end)
    presence = UserPresence.objects.filter(user=staff.user).first()
    online = bool(
        presence
        and presence.active_portal == "SHOP_STAFF"
        and presence.last_seen >= timezone.now()
        - timedelta(minutes=STAFF_ONLINE_WINDOW_MINUTES)
    )
    return render(
        request,
        "seller/staff_hr_self_service.html",
        {
            "staff": staff,
            "duty_summary": duty_summary,
            "online": online,
            "leave_form": leave_form,
            "resignation_form": resignation_form,
            "leave_applications": StaffLeaveApplication.objects.filter(
                staff=staff,
            )[:20],
            "resignation_requests": StaffResignationRequest.objects.filter(
                staff=staff,
            )[:10],
            "recent_sessions": StaffDutySession.objects.filter(staff=staff)[:20],
            "today": today,
        },
    )


@login_required
@transaction.atomic
def shop_staff_hr_dashboard(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id, owner=request.user)
    _complete_due_staff_resignations()
    month_value = request.POST.get("month") or request.GET.get("month")
    try:
        period = _staff_payroll_period(month_value)
    except ValueError as error:
        messages.error(request, str(error))
        period = _staff_payroll_period(timezone.localdate().strftime("%Y-%m"))
    start_day, end_day, start_at, end_at, days_in_month = period
    period_key = start_day.strftime("%Y-%m")
    staff_members = list(
        ShopStaff.objects.filter(
            shop=shop,
            is_public_rider=False,
        ).select_related("user").order_by("user__username")
    )
    rows = []
    payroll_totals = {
        "gross": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "unpaid": Decimal("0.00"),
    }
    for membership in staff_members:
        duty = _staff_duty_summary(membership, start_at, end_at)
        paid_dates = _staff_paid_leave_dates(
            membership,
            start_day,
            end_day,
        ) - duty["duty_dates"]
        record = StaffPayrollRecord.objects.filter(
            staff=membership,
            year=start_day.year,
            month=start_day.month,
        ).select_related("paid_by").first()
        payroll_preview = (
            _calculate_staff_payroll(membership, period)
            if record is None or record.payment_status == StaffPayrollRecord.STATUS_UNPAID
            else None
        )
        presence = UserPresence.objects.filter(user=membership.user).first()
        online = bool(
            membership.is_active
            and presence
            and presence.active_portal == "SHOP_STAFF"
            and presence.last_seen >= timezone.now()
            - timedelta(minutes=STAFF_ONLINE_WINDOW_MINUTES)
        )
        rows.append({
            "staff": membership,
            "online": online,
            "duty": duty,
            "paid_leave_days": len(paid_dates),
            "payroll_record": record,
            "payroll_preview": payroll_preview,
            "salary_form": ShopStaffPayrollSettingsForm(
                prefix=f"salary-{membership.pk}",
                instance=membership,
            ),
        })
        gross_amount = (
            record.gross_amount
            if record
            else payroll_preview["gross_amount"]
            if payroll_preview
            else Decimal("0.00")
        )
        payroll_totals["gross"] += gross_amount
        if record and record.payment_status == StaffPayrollRecord.STATUS_PAID:
            payroll_totals["paid"] += gross_amount
        else:
            payroll_totals["unpaid"] += gross_amount

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_salary":
            staff_id = request.POST.get("staff_id")
            membership = get_object_or_404(
                ShopStaff,
                pk=staff_id,
                shop=shop,
                is_public_rider=False,
            )
            form = ShopStaffPayrollSettingsForm(
                request.POST,
                prefix=f"salary-{membership.pk}",
                instance=membership,
            )
            if form.is_valid():
                form.save()
                messages.success(
                    request,
                    f"Salary policy saved for {membership.user.username}.",
                )
            else:
                messages.error(
                    request,
                    "Salary settings were not saved; check the entered amounts.",
                )
        elif action in {"calculate_all", "calculate_one"}:
            target_id = request.POST.get("staff_id")
            if action == "calculate_one":
                targets = [get_object_or_404(
                    ShopStaff,
                    pk=target_id,
                    shop=shop,
                    is_public_rider=False,
                )]
            else:
                targets = staff_members
            for membership in targets:
                record, created = StaffPayrollRecord.objects.get_or_create(
                    staff=membership,
                    year=start_day.year,
                    month=start_day.month,
                    defaults=_calculate_staff_payroll(membership, period),
                )
                if not created:
                    if record.payment_status == StaffPayrollRecord.STATUS_PAID:
                        continue
                    for field, value in _calculate_staff_payroll(
                        membership,
                        period,
                    ).items():
                        setattr(record, field, value)
                    record.calculated_at = timezone.now()
                    record.save(update_fields=[
                        "salary_method",
                        "duty_hours",
                        "duty_days",
                        "paid_leave_days",
                        "completed_order_count",
                        "monthly_base_salary",
                        "hourly_rate",
                        "completed_order_bonus",
                        "gross_amount",
                        "calculated_at",
                        "updated_at",
                    ])
            messages.success(request, "Payroll calculated for the selected month.")
        elif action == "set_payment_status":
            record = get_object_or_404(
                StaffPayrollRecord.objects.select_for_update(),
                pk=request.POST.get("record_id"),
                staff__shop=shop,
                staff__is_public_rider=False,
            )
            status = request.POST.get("payment_status")
            if status not in {
                StaffPayrollRecord.STATUS_PAID,
                StaffPayrollRecord.STATUS_UNPAID,
            }:
                messages.error(request, "Choose a valid payroll payment status.")
            else:
                record.payment_status = status
                record.paid_at = (
                    timezone.now()
                    if status == StaffPayrollRecord.STATUS_PAID
                    else None
                )
                record.paid_by = (
                    request.user
                    if status == StaffPayrollRecord.STATUS_PAID
                    else None
                )
                record.save(update_fields=[
                    "payment_status",
                    "paid_at",
                    "paid_by",
                    "updated_at",
                ])
                messages.success(request, "Payroll payment status updated.")
        elif action == "review_leave":
            application = get_object_or_404(
                StaffLeaveApplication.objects.select_for_update(),
                pk=request.POST.get("leave_id"),
                staff__shop=shop,
            )
            status = request.POST.get("status")
            if (
                application.status != StaffLeaveApplication.STATUS_PENDING
                or status not in {
                    StaffLeaveApplication.STATUS_APPROVED,
                    StaffLeaveApplication.STATUS_DECLINED,
                }
            ):
                messages.error(request, "This leave request cannot be reviewed.")
            else:
                application.status = status
                application.is_paid = (
                    status == StaffLeaveApplication.STATUS_APPROVED
                    and request.POST.get("is_paid") == "on"
                )
                application.reviewed_by = request.user
                application.reviewed_at = timezone.now()
                application.save(update_fields=[
                    "status",
                    "is_paid",
                    "reviewed_by",
                    "reviewed_at",
                    "updated_at",
                ])
                messages.success(request, "Leave request reviewed.")
        elif action == "review_resignation":
            resignation = get_object_or_404(
                StaffResignationRequest.objects.select_for_update(),
                pk=request.POST.get("resignation_id"),
                staff__shop=shop,
            )
            status = request.POST.get("status")
            minimum_last_day = (
                timezone.localtime(resignation.created_at).date()
                + timedelta(days=15)
            )
            if (
                resignation.status != StaffResignationRequest.STATUS_PENDING
                or status not in {
                    StaffResignationRequest.STATUS_APPROVED,
                    StaffResignationRequest.STATUS_DECLINED,
                }
            ):
                messages.error(request, "This resignation request cannot be reviewed.")
            elif (
                status == StaffResignationRequest.STATUS_APPROVED
                and resignation.requested_last_working_date < minimum_last_day
            ):
                messages.error(
                    request,
                    "Resignation approval requires at least 15 calendar days’ notice.",
                )
            else:
                resignation.status = status
                resignation.reviewed_by = request.user
                resignation.reviewed_at = timezone.now()
                resignation.save(update_fields=[
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "updated_at",
                ])
                messages.success(request, "Resignation request reviewed.")
        return redirect(f"{reverse('shop_staff_hr', kwargs={'shop_id': shop.pk})}?month={period_key}")

    online_count = sum(1 for row in rows if row["online"])
    pending_leaves = StaffLeaveApplication.objects.filter(
        staff__shop=shop,
        staff__is_public_rider=False,
        status=StaffLeaveApplication.STATUS_PENDING,
    ).select_related("staff__user").order_by("created_at")
    pending_resignations = StaffResignationRequest.objects.filter(
        staff__shop=shop,
        staff__is_public_rider=False,
        status=StaffResignationRequest.STATUS_PENDING,
    ).select_related("staff__user").order_by("created_at")
    return render(
        request,
        "shop/staff_hr_dashboard.html",
        {
            "shop": shop,
            "rows": rows,
            "online_count": online_count,
            "staff_count": len(rows),
            "payroll_totals": payroll_totals,
            "payroll_month": period_key,
            "pending_leaves": pending_leaves,
            "pending_resignations": pending_resignations,
            "recent_leave_applications": StaffLeaveApplication.objects.filter(
                staff__shop=shop,
                staff__is_public_rider=False,
            ).exclude(status=StaffLeaveApplication.STATUS_PENDING).select_related(
                "staff__user",
                "reviewed_by",
            )[:30],
            "recent_resignations": StaffResignationRequest.objects.filter(
                staff__shop=shop,
                staff__is_public_rider=False,
            ).exclude(status=StaffResignationRequest.STATUS_PENDING).select_related(
                "staff__user",
                "reviewed_by",
            )[:30],
            "payroll_status_choices": StaffPayrollRecord.STATUS_CHOICES,
            "leave_status_choices": StaffLeaveApplication.STATUS_CHOICES,
            "resignation_status_choices": StaffResignationRequest.STATUS_CHOICES,
        },
    )


@login_required
def staff_payment_manager_dashboard(request):
    authorized_memberships = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        is_public_rider=False,
        role="PAYMENT_MANAGER",
        can_confirm_rider_payments=True,
    ).select_related("shop")
    shop_ids = list(
        authorized_memberships.values_list("shop_id", flat=True)
    )
    if not shop_ids:
        raise PermissionDenied

    pending_payment_requests = (
        CODPaymentApprovalRequest.objects.filter(
            shop_id__in=shop_ids,
            status="PENDING",
        )
        .select_related(
            "shop",
            "shop_order",
            "shop_order__order",
            "rider__user",
        )
        .order_by("created_at", "pk")
    )
    cash_handover_requests = []
    for collection in (
        RiderCashLedgerEntry.objects.filter(
            shop_id__in=shop_ids,
            entry_type="COLLECTION",
            shop_order__status="DELIVERED",
            shop_order__order__payment_type__iexact="COD",
        )
        .select_related(
            "shop",
            "shop_order",
            "shop_order__order",
            "rider__user",
        )
        .order_by("created_at", "pk")
    ):
        collection.handed_over_amount = (
            RiderCashLedgerEntry.objects.filter(
                shop_order_id=collection.shop_order_id,
                entry_type="HANDOVER",
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        collection.remaining_amount = max(
            Decimal("0.00"),
            collection.amount - collection.handed_over_amount,
        )
        if collection.remaining_amount > 0:
            cash_handover_requests.append(collection)
    return render(
        request,
        "seller/staff_payment_manager_dashboard.html",
        {
            "authorized_shops": authorized_memberships,
            "pending_payment_requests": pending_payment_requests,
            "cash_handover_requests": cash_handover_requests,
        },
    )


@login_required
@transaction.atomic
def purchase_manager_add_product(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    is_owner = shop.owner_id == request.user.pk
    staff = ShopStaff.objects.filter(
            user=request.user,
            shop=shop,
            role="PURCHASE_MANAGER",
            is_active=True,
            is_public_rider=False,
        ).first()
    if not is_owner and not staff:
        return HttpResponseForbidden("Only the shop owner or an active purchase manager can access purchasing.")

    form = ProductForm(
        request.POST or None,
        request.FILES or None,
        user=shop.owner,
        allow_custom_product_name=True,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if ProductShop.objects.filter(
            shop=shop,
            product__product_name__iexact=data["product_name"],
            product__product_category=data["product_category"],
        ).exists():
            form.add_error("product_name", "This product is already added to the shop.")
        else:
            product = form.save(commit=False)
            product.created_by = shop.owner
            product.save()
            assignment = ProductShop(
                product=product,
                shop=shop,
                price=product.price,
                pricing_mode="AUTO",
            )
            assignment.full_clean()
            assignment.save()
            messages.success(
                request,
                f"{product.product_name} added to the shop. Record a purchase to receive stock.",
            )
            return redirect(f"{reverse('purchase_manager_dashboard', kwargs={'shop_id': shop.pk})}?tab=purchase")

    return render(
        request,
        "seller/product_form.html",
        {
            "shop": shop,
            "purchase_manager_shop": shop,
            "form": form,
            "assignment_forms": [],
            "form_title": f"Add product to {shop.name}",
            "form_action": "create",
        },
    )


@login_required
@transaction.atomic
def purchase_manager_dashboard(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    is_owner = shop.owner_id == request.user.pk
    staff = ShopStaff.objects.filter(
            user=request.user,
            shop=shop,
            role="PURCHASE_MANAGER",
            is_active=True,
            is_public_rider=False,
        ).first()
    if not is_owner and not staff:
        return HttpResponseForbidden("Only the shop owner or an active purchase manager can access purchasing.")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "add_vendor":
                name = request.POST.get("name", "").strip()
                if not name:
                    raise ValueError("Enter a vendor name.")
                if ShopVendor.objects.filter(shop=shop, name__iexact=name).exists():
                    raise ValueError("A vendor with that name already exists for this shop.")
                ShopVendor.objects.create(
                    shop=shop,
                    name=name,
                    contact_name=request.POST.get("contact_name", "").strip(),
                    phone=request.POST.get("phone", "").strip(),
                    email=request.POST.get("email", "").strip(),
                    tax_id=request.POST.get("tax_id", "").strip(),
                    address=request.POST.get("address", "").strip(),
                )
                messages.success(request, f"Vendor {name} added.")
            elif action == "create_purchase":
                vendor = get_object_or_404(
                    ShopVendor,
                    pk=request.POST.get("vendor_id"),
                    shop=shop,
                    is_active=True,
                )
                purchase_date = date.fromisoformat(request.POST.get("purchase_date", ""))
                due_date_value = request.POST.get("due_date", "").strip()
                due_date = date.fromisoformat(due_date_value) if due_date_value else None
                product_ids = request.POST.getlist("product_shop_id")
                stock_types = request.POST.getlist("stock_type")
                quantities = request.POST.getlist("quantity")
                wastage_quantities = request.POST.getlist("wastage_quantity")
                purchase_unit_values = request.POST.getlist("purchase_unit_value")
                purchase_unit_texts = request.POST.getlist("purchase_unit_text")
                unit_costs = request.POST.getlist("unit_cost")
                gst_rates = request.POST.getlist("gst_rate")
                if not wastage_quantities:
                    wastage_quantities = ["0"] * len(product_ids)
                if not purchase_unit_values:
                    purchase_unit_values = [""] * len(product_ids)
                if not purchase_unit_texts:
                    purchase_unit_texts = [""] * len(product_ids)
                if not gst_rates:
                    gst_rates = ["0"] * len(product_ids)
                if not product_ids or not (
                    len(product_ids)
                    == len(stock_types)
                    == len(quantities)
                    == len(wastage_quantities)
                    == len(purchase_unit_values)
                    == len(purchase_unit_texts)
                    == len(unit_costs)
                    == len(gst_rates)
                ):
                    raise ValueError("Add at least one complete purchase line.")
                lines = []
                locked_assignments = {}
                subtotal_amount = Decimal("0.00")
                gst_amount = Decimal("0.00")
                for (
                    product_id,
                    stock_type,
                    quantity_value,
                    wastage_value,
                    purchase_unit_value,
                    purchase_unit_text,
                    unit_cost_value,
                    gst_rate_value,
                ) in zip(
                    product_ids,
                    stock_types,
                    quantities,
                    wastage_quantities,
                    purchase_unit_values,
                    purchase_unit_texts,
                    unit_costs,
                    gst_rates,
                ):
                    assignment = locked_assignments.get(product_id)
                    if assignment is None:
                        assignment = ProductShop.objects.select_for_update().select_related(
                            "product"
                        ).get(pk=product_id, shop=shop)
                        locked_assignments[product_id] = assignment
                    try:
                        purchased_quantity = int(quantity_value)
                        wastage_quantity = Decimal(wastage_value or "0")
                    except (TypeError, ValueError, InvalidOperation):
                        raise ValueError("Enter valid purchase and wastage quantities.")
                    unit_cost = Decimal(unit_cost_value)
                    gst_rate = Decimal(gst_rate_value or "0")
                    if stock_type not in {"RETAIL", "B2B"}:
                        raise ValueError("Choose retail or B2B stock for every line.")
                    inventory_unit = (
                        assignment.common_b2b_unit or assignment.product.unit
                        if stock_type == "B2B"
                        else assignment.product.unit
                    )
                    inventory_value, inventory_unit_text = _split_product_unit(
                        inventory_unit
                    )
                    if inventory_value is None:
                        inventory_value = Decimal("1")
                    elif inventory_value <= 0:
                        raise ValueError("Inventory unit values must be greater than zero.")
                    inventory_unit_text = inventory_unit_text or inventory_unit or "unit"
                    try:
                        purchase_unit_value = Decimal(
                            purchase_unit_value or inventory_value
                        )
                    except (TypeError, InvalidOperation):
                        raise ValueError("Enter a valid purchase unit value.")
                    purchase_unit_text = (
                        purchase_unit_text.strip() or inventory_unit_text
                    )
                    if (
                        not purchase_unit_value.is_finite()
                        or purchase_unit_value <= 0
                        or purchase_unit_value > Decimal("999999.9999")
                        or purchase_unit_value
                        != purchase_unit_value.quantize(Decimal("0.0001"))
                        or len(purchase_unit_text) > 30
                    ):
                        raise ValueError(
                            "Purchase unit value must be positive with up to 4 decimal places."
                        )
                    if (
                        purchased_quantity <= 0
                        or purchased_quantity > 2147483647
                        or not wastage_quantity.is_finite()
                        or wastage_quantity < 0
                        or wastage_quantity != wastage_quantity.quantize(Decimal("0.0001"))
                    ):
                        raise ValueError(
                            "Purchased quantity must be positive and wastage must use up to 4 decimal places."
                        )
                    purchased_stock_quantity = _converted_purchase_quantity(
                        purchased_quantity,
                        purchase_unit_value,
                        purchase_unit_text,
                        inventory_value,
                        inventory_unit_text,
                    )
                    if purchased_stock_quantity > Decimal("9999999999.9999"):
                        raise ValueError("The converted purchase quantity is too large.")
                    if wastage_quantity > purchased_stock_quantity:
                        raise ValueError("Wastage cannot exceed the converted purchase quantity.")
                    received_quantity = purchased_stock_quantity - wastage_quantity
                    if (
                        not unit_cost.is_finite()
                        or unit_cost < 0
                        or unit_cost > Decimal("99999999.99")
                        or unit_cost != unit_cost.quantize(Decimal("0.01"))
                    ):
                        raise ValueError("Enter valid supplier costs with up to 2 decimal places.")
                    if (
                        not gst_rate.is_finite()
                        or gst_rate < 0
                        or gst_rate > 100
                        or gst_rate != gst_rate.quantize(Decimal("0.01"))
                    ):
                        raise ValueError("GST rate must be between 0 and 100 with up to 2 decimal places.")
                    line_total = (unit_cost * purchased_quantity).quantize(Decimal("0.01"))
                    line_gst_amount = (line_total * gst_rate / Decimal("100")).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )
                    subtotal_amount += line_total
                    gst_amount += line_gst_amount
                    lines.append(
                        (
                            assignment,
                            stock_type,
                            purchased_quantity,
                            purchase_unit_value,
                            purchase_unit_text,
                            inventory_value,
                            inventory_unit_text,
                            purchased_stock_quantity,
                            received_quantity,
                            wastage_quantity,
                            unit_cost,
                            line_total,
                            gst_rate,
                            line_gst_amount,
                        )
                    )
                subtotal_amount = subtotal_amount.quantize(Decimal("0.01"))
                gst_amount = gst_amount.quantize(Decimal("0.01"))
                delivery_expense = Decimal(
                    request.POST.get("delivery_expense", "0") or "0"
                )
                delivery_gst_rate = Decimal(
                    request.POST.get("delivery_gst_rate", "0") or "0"
                )
                if (
                    not delivery_expense.is_finite()
                    or delivery_expense < 0
                    or delivery_expense > Decimal("999999999.99")
                    or delivery_expense != delivery_expense.quantize(Decimal("0.01"))
                ):
                    raise ValueError("Enter a valid delivery expense with up to 2 decimal places.")
                if (
                    not delivery_gst_rate.is_finite()
                    or delivery_gst_rate < 0
                    or delivery_gst_rate > 100
                    or delivery_gst_rate != delivery_gst_rate.quantize(Decimal("0.01"))
                ):
                    raise ValueError("Delivery GST rate must be between 0 and 100.")
                delivery_gst_amount = (
                    delivery_expense * delivery_gst_rate / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                gst_amount += delivery_gst_amount
                total_amount = (
                    subtotal_amount + gst_amount + delivery_expense
                ).quantize(Decimal("0.01"))
                if total_amount > Decimal("999999999999.99"):
                    raise ValueError("The purchase invoice total is too large.")
                initial_payment = Decimal(request.POST.get("initial_payment", "0") or "0")
                if (
                    not initial_payment.is_finite()
                    or initial_payment < 0
                    or initial_payment > total_amount
                    or initial_payment != initial_payment.quantize(Decimal("0.01"))
                ):
                    raise ValueError("Initial payment must be between zero and the invoice total.")
                payment_method = request.POST.get("payment_method", "BANK")
                if payment_method not in dict(VendorPurchasePayment.METHOD_CHOICES):
                    raise ValueError("Choose a valid payment method.")
                cost_method = request.POST.get("cost_method", "AVERAGE")
                if cost_method not in dict(VendorPurchase.COST_METHOD_CHOICES):
                    raise ValueError("Choose average cost or latest purchase cost.")

                purchase = VendorPurchase.objects.create(
                    shop=shop,
                    vendor=vendor,
                    invoice_number=request.POST.get("invoice_number", "").strip(),
                    purchase_date=purchase_date,
                    due_date=due_date,
                    cost_method=cost_method,
                    total_amount=total_amount,
                    subtotal_amount=subtotal_amount,
                    gst_amount=gst_amount,
                    delivery_expense=delivery_expense,
                    delivery_gst_rate=delivery_gst_rate,
                    delivery_gst_amount=delivery_gst_amount,
                    notes=request.POST.get("notes", "").strip(),
                    created_by=request.user,
                )
                receipt_totals = {}
                assignment_updates = {}
                for (
                    assignment,
                    stock_type,
                    purchased_quantity,
                    purchase_unit_value,
                    purchase_unit_text,
                    inventory_value,
                    inventory_unit_text,
                    purchased_stock_quantity,
                    received_quantity,
                    wastage_quantity,
                    unit_cost,
                    line_total,
                    gst_rate,
                    line_gst_amount,
                ) in lines:
                    VendorPurchaseItem.objects.create(
                        purchase=purchase,
                        product_shop=assignment,
                        stock_type=stock_type,
                        quantity=received_quantity,
                        purchased_quantity=purchased_quantity,
                        wastage_quantity=wastage_quantity,
                        purchase_unit_value=purchase_unit_value,
                        purchase_unit_text=purchase_unit_text,
                        inventory_unit_value=inventory_value,
                        inventory_unit_text=inventory_unit_text,
                        unit_cost=unit_cost,
                        line_total=line_total,
                        gst_rate=gst_rate,
                        gst_amount=line_gst_amount,
                    )
                    key = (assignment.pk, stock_type)
                    if key not in receipt_totals:
                        receipt_totals[key] = {
                            "assignment": assignment,
                            "quantity": Decimal("0"),
                            "cost_total": Decimal("0.00"),
                        }
                    receipt_totals[key]["quantity"] += received_quantity
                    receipt_totals[key]["cost_total"] += line_total
                    if purchased_stock_quantity:
                        StockMovement.objects.create(
                            product_shop=assignment,
                            movement_type="IMPORT",
                            quantity=purchased_stock_quantity,
                            reference=f"Vendor purchase {purchase.invoice_number or purchase.pk} ({stock_type})",
                            created_by=request.user,
                        )
                    if wastage_quantity:
                        StockMovement.objects.create(
                            product_shop=assignment,
                            movement_type="ADJUSTMENT",
                            quantity=-wastage_quantity,
                            reference=f"Purchase wastage {purchase.invoice_number or purchase.pk} ({stock_type})",
                            created_by=request.user,
                        )

                for (_assignment_id, stock_type), receipt in receipt_totals.items():
                    assignment = receipt["assignment"]
                    quantity = receipt["quantity"]
                    if quantity == 0:
                        continue
                    stock_field = "stock" if stock_type == "RETAIL" else "b2b_stock"
                    cost_field = "purchase_price" if stock_type == "RETAIL" else "b2b_purchase_price"
                    old_stock = getattr(assignment, stock_field)
                    new_stock = old_stock + quantity
                    if new_stock > Decimal("9999999999.9999"):
                        raise ValueError("A resulting stock total exceeds the allowed limit.")
                    allocated_delivery = (
                        delivery_expense * receipt["cost_total"] / subtotal_amount
                        if subtotal_amount
                        else Decimal("0.00")
                    )
                    landed_cost = receipt["cost_total"] + allocated_delivery
                    effective_cost = (landed_cost / quantity).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )
                    old_cost = getattr(assignment, cost_field)
                    if cost_method == "AVERAGE" and old_stock and old_cost is not None:
                        effective_cost = (
                            old_cost * old_stock + landed_cost
                        ) / new_stock
                        effective_cost = effective_cost.quantize(
                            Decimal("0.01"),
                            rounding=ROUND_HALF_UP,
                        )
                    setattr(assignment, stock_field, new_stock)
                    setattr(assignment, cost_field, effective_cost)
                    fields = assignment_updates.setdefault(
                        assignment.pk,
                        {"assignment": assignment, "fields": set()},
                    )["fields"]
                    fields.update((stock_field, cost_field))

                for receipt in assignment_updates.values():
                    assignment = receipt["assignment"]
                    update_fields = receipt["fields"]
                    if assignment.pricing_mode == "AUTO":
                        assignment.recalculate_prices_from_purchase_cost()
                        update_fields.update(
                            ("price", "bulk_price", "b2b_price", "b2b_bulk_price")
                        )
                    if assignment.product.expiry_management_enabled:
                        assignment.stocked_at = timezone.now()
                        update_fields.add("stocked_at")
                        if assignment.product.shelf_life_days:
                            assignment.expires_at = assignment.stocked_at + timedelta(
                                days=assignment.product.shelf_life_days
                            )
                            update_fields.add("expires_at")
                    assignment.full_clean()
                    assignment.save(update_fields=sorted(update_fields))

                if initial_payment:
                    VendorPurchasePayment.objects.create(
                        purchase=purchase,
                        amount=initial_payment,
                        payment_method=payment_method,
                        reference=request.POST.get("payment_reference", "").strip(),
                        created_by=request.user,
                    )
                messages.success(request, f"Purchase invoice {purchase.invoice_number or purchase.pk} recorded.")
            elif action == "record_payment":
                purchase = get_object_or_404(
                    VendorPurchase.objects.select_for_update(),
                    pk=request.POST.get("purchase_id"),
                    shop=shop,
                )
                amount = Decimal(request.POST.get("amount", "").strip())
                if (
                    not amount.is_finite()
                    or amount <= 0
                    or amount != amount.quantize(Decimal("0.01"))
                    or amount > purchase.balance_due
                ):
                    raise ValueError("Payment must be greater than zero and cannot exceed the outstanding balance.")
                method = request.POST.get("payment_method", "BANK")
                if method not in dict(VendorPurchasePayment.METHOD_CHOICES):
                    raise ValueError("Choose a valid payment method.")
                VendorPurchasePayment.objects.create(
                    purchase=purchase,
                    amount=amount,
                    payment_method=method,
                    reference=request.POST.get("reference", "").strip(),
                    created_by=request.user,
                )
                messages.success(request, "Vendor payment recorded.")
            elif action == "return_purchase":
                purchase = get_object_or_404(
                    VendorPurchase.objects.select_for_update(),
                    pk=request.POST.get("purchase_id"),
                    shop=shop,
                )
                purchase_items = list(
                    VendorPurchaseItem.objects.select_for_update()
                    .filter(purchase=purchase)
                    .select_related("product_shop__product")
                )
                return_item_ids = request.POST.getlist("return_item_id")
                return_quantities = request.POST.getlist("return_quantity")
                if not return_item_ids or len(return_item_ids) != len(return_quantities):
                    raise ValueError("Choose at least one purchase item and return quantity.")

                return_lines = []
                return_subtotal = Decimal("0.00")
                return_gst = Decimal("0.00")
                for item_id, quantity_value in zip(return_item_ids, return_quantities):
                    if not quantity_value.strip():
                        continue
                    item = next(
                        (line for line in purchase_items if str(line.pk) == item_id),
                        None,
                    )
                    if item is None:
                        raise ValueError("A returned item does not belong to this invoice.")
                    try:
                        quantity = Decimal(quantity_value)
                    except (TypeError, InvalidOperation):
                        raise ValueError("Enter a valid return quantity.")
                    if (
                        not quantity.is_finite()
                        or quantity != quantity.quantize(Decimal("0.0001"))
                    ):
                        raise ValueError(
                            "Return quantities must use up to 4 decimal places."
                        )
                    previously_returned = item.returns.aggregate(
                        total=Sum("quantity"),
                    )["total"] or Decimal("0")
                    remaining_quantity = item.quantity - previously_returned
                    if quantity <= 0 or quantity > remaining_quantity:
                        raise ValueError(
                            f"Return quantity for {item.product_shop.product.product_name} "
                            f"must be greater than zero and no more than {remaining_quantity}."
                        )
                    previous_subtotal = item.returns.aggregate(
                        total=Sum("subtotal_amount"),
                    )["total"] or Decimal("0.00")
                    previous_gst = item.returns.aggregate(
                        total=Sum("gst_amount"),
                    )["total"] or Decimal("0.00")
                    line_subtotal_remaining = item.line_total - previous_subtotal
                    line_gst_remaining = item.gst_amount - previous_gst
                    subtotal = (
                        line_subtotal_remaining
                        if quantity == remaining_quantity
                        else line_subtotal_remaining * quantity / remaining_quantity
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    gst = (
                        line_gst_remaining
                        if quantity == remaining_quantity
                        else line_gst_remaining * quantity / remaining_quantity
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    assignment = ProductShop.objects.select_for_update().get(
                        pk=item.product_shop_id,
                        shop=shop,
                    )
                    stock_field = "stock" if item.stock_type == "RETAIL" else "b2b_stock"
                    if getattr(assignment, stock_field) < quantity:
                        raise ValueError(
                            f"Not enough {item.get_stock_type_display()} stock to return "
                            f"{item.product_shop.product.product_name}."
                        )
                    return_lines.append(
                        (item, assignment, stock_field, quantity, subtotal, gst)
                    )
                    return_subtotal += subtotal
                    return_gst += gst
                if not return_lines:
                    raise ValueError("Enter a return quantity for at least one invoice item.")

                return_subtotal = return_subtotal.quantize(Decimal("0.01"))
                return_gst = return_gst.quantize(Decimal("0.01"))
                purchase_return = VendorPurchaseReturn.objects.create(
                    purchase=purchase,
                    subtotal_amount=return_subtotal,
                    gst_amount=return_gst,
                    total_amount=return_subtotal + return_gst,
                    reason=request.POST.get("return_reason", "").strip()[:240],
                    created_by=request.user,
                )
                for item, assignment, stock_field, quantity, subtotal, gst in return_lines:
                    VendorPurchaseReturnItem.objects.create(
                        purchase_return=purchase_return,
                        purchase_item=item,
                        quantity=quantity,
                        subtotal_amount=subtotal,
                        gst_amount=gst,
                    )
                    setattr(assignment, stock_field, getattr(assignment, stock_field) - quantity)
                    assignment.save(update_fields=[stock_field])
                    StockMovement.objects.create(
                        product_shop=assignment,
                        movement_type="ADJUSTMENT",
                        quantity=-quantity,
                        reference=f"Vendor return {purchase.invoice_number or purchase.pk}",
                        created_by=request.user,
                    )
                messages.success(
                    request,
                    f"Purchase return recorded. Vendor credit ₹{purchase_return.total_amount:.2f}.",
                )
            elif action == "add_stock":
                assignment = ProductShop.objects.select_for_update().select_related(
                    "product",
                ).get(
                    pk=request.POST.get("product_shop_id"),
                    shop=shop,
                )
                stock_type = request.POST.get("stock_type", "RETAIL")
                if stock_type not in {"RETAIL", "B2B"}:
                    raise ValueError("Choose retail or B2B stock.")
                try:
                    quantity = Decimal(request.POST.get("quantity", ""))
                except (TypeError, InvalidOperation):
                    raise ValueError("Enter a valid stock quantity.")
                if (
                    not quantity.is_finite()
                    or quantity <= 0
                    or quantity != quantity.quantize(Decimal("0.0001"))
                ):
                    raise ValueError("Stock quantity must be greater than zero.")
                stock_field = "stock" if stock_type == "RETAIL" else "b2b_stock"
                new_stock = getattr(assignment, stock_field) + quantity
                if new_stock > Decimal("9999999999.9999"):
                    raise ValueError("The resulting stock total exceeds the allowed limit.")
                setattr(assignment, stock_field, new_stock)
                update_fields = [stock_field]
                if assignment.product.expiry_management_enabled:
                    assignment.stocked_at = timezone.now()
                    update_fields.append("stocked_at")
                    if assignment.product.shelf_life_days:
                        assignment.expires_at = assignment.stocked_at + timedelta(
                            days=assignment.product.shelf_life_days,
                        )
                        update_fields.append("expires_at")
                assignment.save(update_fields=update_fields)
                StockMovement.objects.create(
                    product_shop=assignment,
                    movement_type="IMPORT",
                    quantity=quantity,
                    reference=f"Manual stock addition ({stock_type})",
                    created_by=request.user,
                )
                messages.success(
                    request,
                    f"Added {quantity} units to {stock_type} stock for "
                    f"{assignment.product.product_name}.",
                )
            elif action == "save_pricing":
                assignment = ProductShop.objects.select_for_update().select_related(
                    "product"
                ).get(pk=request.POST.get("product_shop_id"), shop=shop)
                pricing_mode = request.POST.get("pricing_mode", "AUTO")
                if pricing_mode not in {"AUTO", "MANUAL"}:
                    raise ValueError("Choose margin-based or manual pricing.")
                assignment.pricing_mode = pricing_mode
                product_unit_changed = False
                product_unit_fields = (
                    ("retail_unit_value", "retail_unit_text", "product"),
                    ("bulk_unit_value", "bulk_unit_text", "bulk_unit"),
                    ("b2b_unit_value", "retail_unit_text", "b2b_unit"),
                    ("b2b_bulk_unit_value", "b2b_bulk_unit_text", "b2b_bulk_unit"),
                )
                for value_field, text_field, target in product_unit_fields:
                    if value_field not in request.POST and text_field not in request.POST:
                        continue
                    raw_value = request.POST.get(value_field, "").strip()
                    unit_value = None
                    if raw_value:
                        try:
                            unit_value = Decimal(raw_value)
                        except InvalidOperation:
                            raise ValueError("Enter valid product unit values.")
                        if (
                            not unit_value.is_finite()
                            or unit_value <= 0
                            or unit_value > Decimal("999999.9999")
                            or unit_value != unit_value.quantize(Decimal("0.0001"))
                        ):
                            raise ValueError(
                                "Product unit values must be positive with up to 4 decimal places."
                            )
                    unit_text = request.POST.get(text_field, "").strip()
                    if unit_value is not None and not unit_text:
                        raise ValueError(
                            "Enter unit text when setting a product unit value."
                        )
                    combined_unit = _join_product_unit(unit_value, unit_text)
                    if len(combined_unit) > 40:
                        raise ValueError("Combined product units must be 40 characters or fewer.")
                    if target == "product":
                        assignment.product.unit = combined_unit
                        product_unit_changed = True
                    else:
                        setattr(assignment, target, combined_unit)
                cost_fields = ("purchase_price", "b2b_purchase_price")
                for field_name in cost_fields:
                    if field_name not in request.POST:
                        continue
                    raw_value = request.POST.get(field_name, "").strip()
                    if not raw_value:
                        setattr(assignment, field_name, None)
                        continue
                    value = Decimal(raw_value)
                    if (
                        not value.is_finite()
                        or value < 0
                        or value > Decimal("99999999.99")
                        or value != value.quantize(Decimal("0.01"))
                    ):
                        raise ValueError("Purchase costs must be non-negative and have up to 2 decimal places.")
                    setattr(assignment, field_name, value)
                pricing_fields = (
                    "retail_margin_percent",
                    "retail_margin_fixed",
                    "bulk_margin_percent",
                    "bulk_margin_fixed",
                    "b2b_margin_percent",
                    "b2b_margin_fixed",
                    "b2b_bulk_margin_percent",
                    "b2b_bulk_margin_fixed",
                )
                for field_name in pricing_fields:
                    value = Decimal(request.POST.get(field_name, "0").strip() or "0")
                    maximum = Decimal("99999.99") if field_name.endswith("_percent") else Decimal("99999999.99")
                    if not value.is_finite() or value < 0 or value > maximum or value != value.quantize(Decimal("0.01")):
                        raise ValueError("Enter non-negative margins with up to 2 decimal places.")
                    setattr(assignment, field_name, value)
                manual_price_fields = (
                    "price",
                    "bulk_price",
                    "b2b_price",
                    "b2b_bulk_price",
                )
                if pricing_mode == "MANUAL":
                    for field_name in manual_price_fields:
                        raw_value = request.POST.get(field_name, "").strip()
                        if not raw_value:
                            if field_name == "price":
                                raise ValueError("Enter a manual retail price.")
                            setattr(assignment, field_name, None)
                            continue
                        value = Decimal(raw_value)
                        if (
                            not value.is_finite()
                            or value < 0
                            or value > Decimal("99999999.99")
                            or value != value.quantize(Decimal("0.01"))
                        ):
                            raise ValueError("Manual prices must be non-negative and have up to 2 decimal places.")
                        setattr(assignment, field_name, value)
                else:
                    assignment.recalculate_prices_from_purchase_cost()
                assignment.full_clean()
                if product_unit_changed:
                    assignment.product.full_clean()
                    assignment.product.save(update_fields=["unit"])
                assignment.save(update_fields=[
                    "pricing_mode",
                    *cost_fields,
                    *pricing_fields,
                    *manual_price_fields,
                    "bulk_unit",
                    "b2b_unit",
                    "b2b_bulk_unit",
                ])
                price_message = (
                    "Manual sale prices saved"
                    if pricing_mode == "MANUAL"
                    else "Margin-based sale prices updated"
                )
                messages.success(request, f"{price_message} for {assignment.product.product_name}.")
            else:
                raise ValueError("Choose a valid purchasing action.")
        except (InvalidOperation, TypeError, ValueError, ValidationError) as exc:
            transaction.set_rollback(True)
            message = exc.message if isinstance(exc, ValidationError) else str(exc)
            messages.error(request, message or "Enter valid purchase information.")
        except ShopVendor.DoesNotExist:
            transaction.set_rollback(True)
            messages.error(request, "Select a vendor belonging to this shop.")
        except ProductShop.DoesNotExist:
            transaction.set_rollback(True)
            messages.error(request, "Select a product belonging to this shop.")
        redirect_url = reverse("purchase_manager_dashboard", kwargs={"shop_id": shop.pk})
        requested_tab = request.POST.get("tab", "")
        if requested_tab in {"overview", "purchase", "history", "products", "reports"}:
            return redirect(f"{redirect_url}?tab={requested_tab}")
        return redirect(redirect_url)

    search_query = request.GET.get("q", "").strip()
    stock_filter = request.GET.get("stock", "all")
    assignments = list(
        ProductShop.objects.select_related("product").filter(shop=shop).order_by(
            "product__product_name", "pk"
        )
    )
    forecasts = {
        forecast.product_shop_id: forecast
        for forecast in ShopProductForecast.objects.filter(
            product_shop_id__in=[assignment.pk for assignment in assignments]
        )
    }
    purchase_items = VendorPurchaseItem.objects.filter(
        product_shop_id__in=[assignment.pk for assignment in assignments]
    ).select_related("purchase__vendor").order_by("-purchase__purchase_date", "-pk")
    recent_by_assignment = defaultdict(list)
    for item in purchase_items:
        if len(recent_by_assignment[item.product_shop_id]) < 3:
            recent_by_assignment[item.product_shop_id].append(item)
    shortage_count = 0
    for assignment in assignments:
        assignment.pricing_image_url = assignment.display_image_url
        unit = assignment.product.unit
        for key, raw_unit in (
            ("retail", unit),
            ("bulk", assignment.bulk_unit or unit),
            ("b2b", assignment.b2b_unit or unit),
            ("b2b_bulk", assignment.b2b_bulk_unit or assignment.b2b_unit or unit),
        ):
            unit_value, unit_text = _split_product_unit(raw_unit)
            setattr(assignment, f"{key}_unit_value", unit_value or Decimal("1"))
            setattr(assignment, f"{key}_unit_text", unit_text or raw_unit or "unit")
        assignment.b2b_unit_text = assignment.retail_unit_text
        assignment.retail_stock_display = _quantity_unit_calculation(assignment.stock, unit)
        assignment.b2b_stock_display = _quantity_unit_calculation(
            assignment.b2b_stock,
            assignment.common_b2b_unit or unit,
        )
        forecast = forecasts.get(assignment.pk)
        assignment.recommended_restock = (
            max(0, math.ceil(forecast.recommended_stock - assignment.stock))
            if forecast else 0
        )
        assignment.is_shortage = bool(forecast and assignment.recommended_restock)
        shortage_count += int(assignment.is_shortage)
        assignment.recent_purchase_items = recent_by_assignment[assignment.pk]
    all_assignments = list(assignments)
    shortage_assignments = [
        assignment for assignment in all_assignments if assignment.is_shortage
    ]
    vendors = list(ShopVendor.objects.filter(shop=shop).order_by("name"))
    selected_vendor = request.GET.get("vendor", "").strip()
    all_purchases = list(
        VendorPurchase.objects.filter(shop=shop)
        .select_related("vendor")
        .prefetch_related(
            "items__product_shop__product",
            "items__returns",
            "payments",
            "returns__items__purchase_item__product_shop__product",
        )
        .order_by("-purchase_date", "-pk")
    )
    vendor_totals = defaultdict(
        lambda: {
            "purchased": Decimal("0.00"),
            "paid": Decimal("0.00"),
            "credited": Decimal("0.00"),
        }
    )
    payment_records = []
    monthly_reports = {}
    selected_report_month = request.GET.get(
        "report_month",
        timezone.localdate().strftime("%Y-%m"),
    ).strip()
    if selected_report_month:
        try:
            selected_report_month = date.fromisoformat(
                f"{selected_report_month}-01"
            ).strftime("%Y-%m")
        except ValueError:
            selected_report_month = timezone.localdate().strftime("%Y-%m")
    for purchase in all_purchases:
        paid = sum((payment.amount for payment in purchase.payments.all()), Decimal("0.00"))
        credited = sum(
            (purchase_return.total_amount for purchase_return in purchase.returns.all()),
            Decimal("0.00"),
        )
        purchase.amount_paid_display = paid
        purchase.credited_amount_display = credited
        purchase.balance_due_display = max(
            Decimal("0.00"),
            purchase.total_amount - paid - credited,
        )
        purchase.payment_status_display = (
            "PAID"
            if paid + credited >= purchase.total_amount
            else "PARTIAL"
            if paid or credited
            else "UNPAID"
        )
        purchase.has_returnable_items = False
        for item in purchase.items.all():
            item.returnable_quantity = max(
                0,
                item.quantity - sum(returned.quantity for returned in item.returns.all()),
            )
            purchase.has_returnable_items = (
                purchase.has_returnable_items or item.returnable_quantity > 0
            )
        vendor_totals[purchase.vendor_id]["purchased"] += purchase.total_amount
        vendor_totals[purchase.vendor_id]["paid"] += paid
        vendor_totals[purchase.vendor_id]["credited"] += credited
        month_key = purchase.purchase_date.strftime("%Y-%m")
        report = monthly_reports.setdefault(
            month_key,
            {
                "month": month_key,
                "invoice_count": 0,
                "subtotal": Decimal("0.00"),
                "gst": Decimal("0.00"),
                "delivery": Decimal("0.00"),
                "total": Decimal("0.00"),
                "paid": Decimal("0.00"),
                "outstanding": Decimal("0.00"),
                "returns": Decimal("0.00"),
            },
        )
        report["invoice_count"] += 1
        report["subtotal"] += purchase.subtotal_amount
        report["gst"] += purchase.gst_amount
        report["delivery"] += purchase.delivery_expense
        report["total"] += purchase.total_amount
        report["paid"] += paid
        report["outstanding"] += purchase.balance_due_display
        for payment in purchase.payments.all():
            payment_records.append(
                {
                    "vendor": purchase.vendor,
                    "purchase": purchase,
                    "payment": payment,
                }
            )
        for purchase_return in purchase.returns.all():
            return_month = purchase_return.returned_at.strftime("%Y-%m")
            return_report = monthly_reports.setdefault(
                return_month,
                {
                    "month": return_month,
                    "invoice_count": 0,
                    "subtotal": Decimal("0.00"),
                    "gst": Decimal("0.00"),
                    "delivery": Decimal("0.00"),
                    "total": Decimal("0.00"),
                    "paid": Decimal("0.00"),
                    "outstanding": Decimal("0.00"),
                    "returns": Decimal("0.00"),
                },
            )
            return_report["returns"] += purchase_return.total_amount
            return_report["subtotal"] -= purchase_return.subtotal_amount
            return_report["gst"] -= purchase_return.gst_amount
            return_report["total"] -= purchase_return.total_amount
    monthly_report_rows = sorted(
        monthly_reports.values(),
        key=lambda report: report["month"],
        reverse=True,
    )
    if request.GET.get("report_format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="purchase-report-{selected_report_month}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Month",
                "Invoices",
                "Taxable purchases",
                "Input GST",
                "Delivery expense",
                "Invoice total",
                "Paid",
                "Outstanding",
                "Vendor credits / returns",
            ]
        )
        for report in monthly_report_rows:
            if selected_report_month and report["month"] != selected_report_month:
                continue
            writer.writerow(
                [
                    report["month"],
                    report["invoice_count"],
                    report["subtotal"],
                    report["gst"],
                    report["delivery"],
                    report["total"],
                    report["paid"],
                    report["outstanding"],
                    report["returns"],
                ]
            )
        return response
    report_purchases = [
        purchase
        for purchase in all_purchases
        if not selected_report_month
        or purchase.purchase_date.strftime("%Y-%m") == selected_report_month
    ]
    gst_by_rate = defaultdict(
        lambda: {"taxable": Decimal("0.00"), "gst": Decimal("0.00")}
    )
    for purchase in report_purchases:
        for item in purchase.items.all():
            rate_row = gst_by_rate[item.gst_rate]
            rate_row["taxable"] += item.line_total
            rate_row["gst"] += item.gst_amount
        if purchase.delivery_expense:
            delivery_row = gst_by_rate[purchase.delivery_gst_rate]
            delivery_row["taxable"] += purchase.delivery_expense
            delivery_row["gst"] += purchase.delivery_gst_amount
    purchase_returns = VendorPurchaseReturn.objects.filter(
        purchase__shop=shop,
    ).prefetch_related("items__purchase_item")
    for purchase_return in purchase_returns:
        if (
            selected_report_month
            and purchase_return.returned_at.strftime("%Y-%m") != selected_report_month
        ):
            continue
        for return_item in purchase_return.items.all():
            rate_row = gst_by_rate[return_item.purchase_item.gst_rate]
            rate_row["taxable"] -= return_item.subtotal_amount
            rate_row["gst"] -= return_item.gst_amount
    gst_rate_rows = [
        {"rate": rate, **totals}
        for rate, totals in sorted(gst_by_rate.items())
    ]
    payment_records.sort(key=lambda entry: entry["payment"].paid_at, reverse=True)
    payment_vendor_totals = defaultdict(
        lambda: {"amount": Decimal("0.00"), "count": 0}
    )
    for entry in payment_records:
        if (
            selected_report_month
            and entry["payment"].paid_at.strftime("%Y-%m") != selected_report_month
        ):
            continue
        vendor_total = payment_vendor_totals[entry["vendor"].pk]
        vendor_total["amount"] += entry["payment"].amount
        vendor_total["count"] += 1
    purchases = all_purchases
    if selected_vendor.isdigit():
        purchases = [purchase for purchase in purchases if purchase.vendor_id == int(selected_vendor)]
    for vendor in vendors:
        vendor.purchased_total = vendor_totals[vendor.pk]["purchased"]
        vendor.paid_total = vendor_totals[vendor.pk]["paid"]
        vendor.credited_total = vendor_totals[vendor.pk]["credited"]
        vendor.balance_due_total = max(
            Decimal("0.00"),
            vendor.purchased_total - vendor.paid_total - vendor.credited_total,
        )
        vendor.selected_month_payment_total = payment_vendor_totals[vendor.pk]["amount"]
        vendor.selected_month_payment_count = payment_vendor_totals[vendor.pk]["count"]
    if search_query:
        needle = search_query.casefold()
        assignments = [
            assignment for assignment in assignments
            if needle in assignment.product.product_name.casefold()
        ]
    if stock_filter == "shortage":
        assignments = [assignment for assignment in assignments if assignment.is_shortage]
    elif stock_filter == "out":
        assignments = [assignment for assignment in assignments if assignment.stock == 0]
    return render(
        request,
        "seller/purchase_manager_dashboard.html",
        {
            "shop": shop,
            "staff": staff,
            "assignments": assignments,
            "all_assignments": all_assignments,
            "shortage_assignments": shortage_assignments,
            "is_shop_owner_access": is_owner,
            "vendors": vendors,
            "purchases": purchases,
            "purchase_count": len(all_purchases),
            "monthly_reports": monthly_report_rows,
            "report_purchases": report_purchases,
            "gst_rate_rows": gst_rate_rows,
            "payment_records": payment_records,
            "selected_report_month": selected_report_month,
            "shortage_count": shortage_count,
            "search_query": search_query,
            "stock_filter": stock_filter,
            "selected_vendor": selected_vendor,
            "payment_methods": VendorPurchasePayment.METHOD_CHOICES,
            "cost_methods": VendorPurchase.COST_METHOD_CHOICES,
            "today": timezone.localdate(),
            "active_tab": (
                request.GET.get("tab")
                if request.GET.get("tab") in {"overview", "purchase", "history", "products", "reports"}
                else "overview"
            ),
        },
    )


def job_openings(request):
    openings = list(
        JobOpening.objects.filter(is_active=True)
        .select_related("shop")
        .order_by("-created_at")
    )
    search_query = request.GET.get("q", "").strip()
    if search_query:
        openings = [
            opening
            for opening in openings
            if any(
                search_query.casefold() in (value or "").casefold()
                for value in (
                    opening.title,
                    opening.role,
                    opening.employment_type,
                    opening.location,
                    opening.shop.name,
                    opening.description,
                )
            )
        ]
    all_openings = openings

    radius_km = 1.25
    location_filter_errors = []
    requested_radius_value = request.GET.get("radius_km")
    try:
        requested_radius = float(request.GET.get("radius_km", radius_km))
        if math.isfinite(requested_radius) and 0.1 <= requested_radius <= 100:
            radius_km = requested_radius
        elif requested_radius_value is not None:
            location_filter_errors.append(
                "The selected radius is invalid. Using the default 1.25 km radius."
            )
    except (TypeError, ValueError):
        if requested_radius_value is not None:
            location_filter_errors.append(
                "The selected radius is invalid. Using the default 1.25 km radius."
            )

    selected_location = None
    requested_latitude = request.GET.get("lat")
    requested_longitude = request.GET.get("lng")
    if requested_latitude is not None or requested_longitude is not None:
        try:
            latitude = float(requested_latitude)
            longitude = float(requested_longitude)
            if (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                selected_location = {"lat": latitude, "lng": longitude}
            else:
                location_filter_errors.append(
                    "The selected location is invalid. Choose a point on the map and try again."
                )
        except (TypeError, ValueError):
            location_filter_errors.append(
                "The selected location is invalid. Choose a point on the map and try again."
            )

    requested_job_tab = request.GET.get("tab")
    job_tab = requested_job_tab if requested_job_tab in {"all", "nearby"} else (
        "nearby" if selected_location else "all"
    )

    map_openings = []
    for opening in all_openings:
        coordinates = _get_shop_coordinates(opening.shop)
        if coordinates:
            map_openings.append(
                {
                    "lat": coordinates["lat"],
                    "lng": coordinates["lng"],
                    "shop": opening.shop.name,
                    "title": opening.title,
                }
            )

    nearby_openings = []
    if selected_location:
        earth_radius_km = 6371.0088
        center_latitude = math.radians(selected_location["lat"])
        center_longitude = math.radians(selected_location["lng"])
        for opening in all_openings:
            coordinates = _get_shop_coordinates(opening.shop)
            if not coordinates:
                continue
            latitude_delta = math.radians(coordinates["lat"]) - center_latitude
            longitude_delta = math.radians(coordinates["lng"]) - center_longitude
            haversine = (
                math.sin(latitude_delta / 2) ** 2
                + math.cos(center_latitude)
                * math.cos(math.radians(coordinates["lat"]))
                * math.sin(longitude_delta / 2) ** 2
            )
            distance_km = 2 * earth_radius_km * math.asin(
                math.sqrt(min(1, haversine))
            )
            if distance_km <= radius_km:
                nearby_openings.append(opening)
    openings = nearby_openings if job_tab == "nearby" else all_openings

    return render(
        request,
        "seller/job_openings.html",
        {
            "openings": openings,
            "job_tab": job_tab,
            "search_query": search_query,
            "radius_km": radius_km,
            "selected_location": selected_location,
            "map_openings": map_openings,
            "location_filter_errors": location_filter_errors,
        },
    )


@login_required
def my_job_applications(request):
    applications = (
        JobApplication.objects
        .filter(user=request.user)
        .select_related("job_opening", "shop")
        .order_by("-created_at")
    )
    return render(request, "seller/my_job_applications.html", {"applications": applications})


@login_required
def job_apply(request, job_id):
    opening = get_object_or_404(JobOpening, id=job_id, is_active=True)

    if request.method == "POST":
        form = JobApplicationForm(request.POST)
        if form.is_valid():
            application = form.save(commit=False)
            application.shop = opening.shop
            application.job_opening = opening
            application.user = request.user
            application.job_title = application.job_title or opening.title
            application.location = application.location or opening.location
            application.save()
            messages.success(request, "Your job request has been submitted successfully.")
            return redirect("job_openings")
    else:
        form = JobApplicationForm(initial={
            "job_opening": opening,
            "job_title": opening.title,
            "location": opening.location,
        })

    return render(request, "seller/job_application_form.html", {"form": form, "opening": opening})


@login_required
def shop_job_openings(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id, owner=request.user)
    openings = JobOpening.objects.filter(shop=shop).order_by("-created_at")
    return render(request, "seller/shop_job_openings.html", {"shop": shop, "openings": openings})


@login_required
def shop_job_opening_create(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id, owner=request.user)

    if request.method == "POST":
        form = JobOpeningForm(request.POST, owner=request.user)
        if form.is_valid():
            opening = form.save(commit=False)
            opening.shop = shop
            opening.save()
            messages.success(request, "Job opening created successfully.")
            return redirect("shop_job_openings", shop_id=shop.id)
    else:
        form = JobOpeningForm(owner=request.user, initial={"shop": shop, "is_active": True})

    return render(request, "seller/shop_job_opening_form.html", {"form": form, "shop": shop})


@login_required
def shop_job_applications(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id, owner=request.user)
    applications = JobApplication.objects.filter(shop=shop).select_related("job_opening").order_by("-created_at")
    return render(request, "seller/shop_job_applications.html", {"shop": shop, "applications": applications})


@login_required
def shop_job_application_status(request, shop_id, application_id):
    shop = get_object_or_404(Shop, id=shop_id, owner=request.user)
    application = get_object_or_404(JobApplication, id=application_id, shop=shop)

    status = request.POST.get("status")
    interview_time = request.POST.get("interview_time")
    interview_notes = request.POST.get("interview_notes", "")

    if status in dict(JobApplication.STATUS_CHOICES):
        application.status = status

    if interview_time:
        try:
            application.interview_datetime = timezone.datetime.fromisoformat(interview_time.replace("Z", "+00:00"))
        except ValueError:
            application.interview_datetime = None

    if interview_notes:
        application.interview_notes = interview_notes

    application.save()
    messages.success(request, f"Application status updated to {application.status}.")
    return redirect("shop_job_applications", shop_id=shop.id)

@login_required
def shop_orders(request, shop_id):

    shop = get_object_or_404(
        Shop,
        id=shop_id
    )

    is_owner = (
        shop.owner_id == request.user.id
    )

    staff = (
        ShopStaff.objects
        .filter(
            shop=shop,
            user=request.user,
            is_active=True
        )
        .first()
    )

    if not is_owner and not staff:
        return HttpResponseForbidden(
            "You do not have access to this shop."
        )

    if staff and not staff.can_view_orders:
        return HttpResponseForbidden(
            "You do not have permission to view orders."
        )

    orders = (
        ShopOrder.objects
        .select_related("order", "shop")
        .prefetch_related("order_items")
        .filter(
            shop=shop
        )
        .order_by("-created_at")
    )

    status = request.GET.get(
        "status"
    )

    if status:
        orders = orders.filter(
            status=status
        )

    search = request.GET.get(
        "q",
        ""
    ).strip()

    if search:

        orders = orders.filter(
            Q(
                order__order_id__icontains=search
            )
            | Q(
                shop_order_number__icontains=search
            )
            |
            Q(
                order__user__icontains=search
            )
            | Q(order__customer_name__icontains=search)
            | Q(order__delivery_contact_no__icontains=search)
        )

    return render(
        request,
        "seller/shop_orders.html",
        {
            "shop": shop,
            "orders": orders,
            "staff": staff,
            "is_owner": is_owner,
        }
    )

@login_required
def packer_dashboard(request):

    staff = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        role="PACKER"
    ).select_related(
        "shop"
    ).first()

    if not staff:
        raise PermissionDenied

    shop_orders = (
        ShopOrder.objects
        .filter(
            shop=staff.shop,
            status__in=[
                "PLACED",
                "CONFIRMED",
                "PACKING",
                "READY",
                "RIDER_ASSIGNED",
                "PICKED_UP",
                "OUT_FOR_DELIVERY",
            ]
        )
        .select_related(
            "order",
            "shop",
            "assigned_packer",
            "assigned_packer__user",
            "assigned_rider",
            "assigned_rider__user",
        )
        .prefetch_related(
            Prefetch(
                "order_items",
                queryset=OrderItem.objects.select_related(
                    "product",
                    "product_shop__product",
                    "product_shop__shop",
                ),
            )
        )
    )

    available_orders = shop_orders.filter(
        status__in=["PLACED", "CONFIRMED", "PACKING"],
        assigned_packer__isnull=True,
    )
    active_orders = shop_orders.filter(
        assigned_packer=staff,
        status__in=["PLACED", "CONFIRMED", "PACKING"],
    )
    ready_orders = shop_orders.filter(
        assigned_packer=staff,
        status="READY",
    )
    verification_orders = shop_orders.filter(
        status="RIDER_ASSIGNED",
        assigned_rider__isnull=False,
    )
    recently_verified_orders = (
        ShopOrder.objects
        .filter(
            shop=staff.shop,
            status_history__status="PICKED_UP",
            status_history__remark__icontains="verified rider",
        )
        .annotate(
            pickup_verified_at=Max(
                "status_history__created_at",
                filter=Q(
                    status_history__status="PICKED_UP",
                    status_history__remark__icontains="verified rider",
                ),
            ),
        )
        .select_related("order", "shop", "assigned_rider", "assigned_rider__user")
        .order_by("-pickup_verified_at", "-pk")
        .distinct()
    )
    packing_history = (
        ShopOrder.objects
        .filter(
            shop=staff.shop,
            assigned_packer=staff,
            status_history__status="READY",
        )
        .select_related(
            "order",
            "shop",
        )
        .prefetch_related(
            Prefetch(
                "order_items",
                queryset=OrderItem.objects.select_related(
                    "product",
                    "product_shop__product",
                    "product_shop__shop",
                ),
            ),
            "status_history",
        )
        .distinct()
    )

    current_view = request.GET.get("view", "available")
    order_views = {
        "available": available_orders,
        "active": active_orders,
        "ready": ready_orders,
        "verify": verification_orders,
        "verified": recently_verified_orders,
        "history": packing_history,
    }
    if current_view not in order_views:
        current_view = "available"

    selected_orders = order_views[current_view].order_by("created_at", "pk")
    customer_usernames = list(
        selected_orders.values_list("order__user", flat=True).distinct()
    )
    customer_names = dict(
        ClientProfile.objects.filter(
            user__username__in=customer_usernames,
        ).values_list("user__username", "name")
    )
    orders = list(selected_orders)
    for order in orders:
        order.customer_display_name = (
            order.order.customer_name
            or customer_names.get(order.order.user)
            or order.order.user
        )

    return render(
        request,
        "shop/packer_dashboard.html",
        {
            "staff": staff,
            "orders": orders,
            "current_view": current_view,
            "available_view": current_view == "available",
            "active_view": current_view == "active",
            "ready_view": current_view == "ready",
            "verify_view": current_view == "verify",
            "verified_view": current_view == "verified",
            "history_view": current_view == "history",
            "available_order_count": available_orders.count(),
            "active_order_count": active_orders.count(),
            "ready_order_count": ready_orders.count(),
            "verification_order_count": verification_orders.count(),
            "recently_verified_order_count": recently_verified_orders.count(),
            "packing_history_count": packing_history.count(),
            "efficiency": _staff_efficiency_card(
                staff.shop,
                "PACKER",
                staff,
            ),
        }
    )


# @login_required
# def rider_dashboard(request):

#     staff = ShopStaff.objects.filter(
#         user=request.user,
#         is_active=True,
#         role="RIDER"
#     ).select_related(
#         "shop"
#     ).first()

#     if not staff:
#         raise PermissionDenied
    
#     orders = (
#         ShopOrder.objects
#         .filter(
#             shop=staff.shop,
#             status__in=[
#                 "READY",
#                 "PICKED_UP",
#                 "OUT_FOR_DELIVERY",
#                 # "CANCELLED",
#             ]
#         )
#         .select_related(
#             "order",
#             "shop",
#             "assigned_rider",
#             "assigned_rider__user",
#         )
#         .prefetch_related(
#             "order_items",
#         )
#         .order_by("-created_at")
#     )

#     rider_profile = getattr(request.user, "shop_staff", None)
#     # print(orders)
#     verify_need=(Shop.objects.filter(name=orders[0].shop).first()).require_customer_otp_verification
#     print('verify_need: ',verify_need)
#     for order_obj in orders:
#         customer_profile = ClientProfile.objects.filter(user__username=order_obj.order.user).first() if order_obj.order.user else None
#         order_obj.customer_otp = customer_profile.verification_otp if customer_profile else "----"
#         order_obj.rider_otp = rider_profile.verification_otp if rider_profile else "----"
#         order_obj.payment_status = order_obj.order.payment_status if hasattr(order_obj.order, "payment_status") else "PENDING"
#         # print(order_obj.shop)
#         # print('shop ',order_obj.shop)
#         # shop=Shop.objects.filter(name=order_obj.shop).first()
        
#         # # shop.require_customer_otp_verification
#         # print('shop ',shop.require_customer_otp_verification)
        
#     # verify_need=orders.require_customer_otp_verification
#     # print('verify_need: ',verify_need)
    
#     return render(
#         request,
#         "seller/rider_dashboard.html",
#         {
#             "staff": staff,
#             "orders": orders,
#             "verify_need": verify_need,
#         }
#     )

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q


def _get_order_delivery_coordinates(order):
    location = order.placed_location if isinstance(order.placed_location, dict) else {}
    latitude = location.get("lat")
    longitude = location.get("lng")
    if latitude is None or longitude is None:
        try:
            latitude, longitude = (
                value.strip()
                for value in order.delivery_location.split(",", 1)
            )
        except (AttributeError, ValueError):
            return None
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
    return {"lat": latitude, "lng": longitude}


def _get_shop_coordinates(shop):
    location = shop.location if isinstance(shop.location, dict) else {}
    latitude = location.get("lat")
    longitude = location.get("lng")
    coordinates = location.get("coordinates")
    if (
        (latitude is None or longitude is None)
        and isinstance(coordinates, (list, tuple))
        and len(coordinates) >= 2
    ):
        latitude, longitude = coordinates[0], coordinates[1]
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
    return {"lat": latitude, "lng": longitude}


def _parse_shop_pickup_locations(raw_value, *, max_locations):
    try:
        locations = json.loads(raw_value or "[]")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Pickup locations must be valid JSON.") from error
    if not isinstance(locations, list):
        raise ValueError("Pickup locations must be provided as a list.")
    if len(locations) > max_locations:
        raise ValueError(
            f"Your shop may configure up to {max_locations} additional pickup locations."
        )

    normalized = []
    for index, location in enumerate(locations, start=1):
        if not isinstance(location, dict):
            raise ValueError(f"Pickup location {index} is invalid.")
        name = str(location.get("name", "")).strip()[:120]
        address = str(location.get("address", "")).strip()[:500]
        try:
            latitude = float(location.get("lat"))
            longitude = float(location.get("lng"))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Pickup location {index} needs valid latitude and longitude."
            ) from error
        if (
            not name
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError(
                f"Pickup location {index} needs a name and valid map coordinates."
            )
        normalized.append({
            "name": name,
            "address": address,
            "lat": latitude,
            "lng": longitude,
        })
    return normalized


def _shop_pickup_locations(shop):
    locations = []
    primary = _get_shop_coordinates(shop)
    if primary:
        locations.append({
            "name": shop.name,
            "address": (
                str(shop.location.get("address", "")).strip()
                if isinstance(shop.location, dict)
                else ""
            ),
            **primary,
        })
    for location in shop.pickup_locations or []:
        if not isinstance(location, dict):
            continue
        coordinates = _valid_route_coordinates(location) and {
            "lat": float(location["lat"]),
            "lng": float(location["lng"]),
        }
        if coordinates:
            locations.append({
                "name": str(location.get("name", "")).strip() or shop.name,
                "address": str(location.get("address", "")).strip(),
                **coordinates,
            })
    return locations


def _select_shop_pickup_location(shop, order):
    locations = _shop_pickup_locations(shop)
    if not locations:
        return {}
    delivery = _get_order_delivery_coordinates(order)
    if not delivery:
        return locations[0]

    def distance_to_delivery(location):
        distance = distance_km_between_points(
            location["lat"],
            location["lng"],
            delivery["lat"],
            delivery["lng"],
        )
        return distance if distance is not None else float("inf")

    return min(locations, key=distance_to_delivery)


def _get_shop_order_pickup_location(shop_order):
    pickup = shop_order.pickup_location
    if isinstance(pickup, dict) and _valid_route_coordinates(pickup):
        return pickup
    coordinates = _get_shop_coordinates(shop_order.shop)
    if not coordinates:
        return {}
    return {
        "name": shop_order.shop.name,
        "address": (
            str(shop_order.shop.location.get("address", "")).strip()
            if isinstance(shop_order.shop.location, dict)
            else ""
        ),
        **coordinates,
    }


def _public_rider_route_estimate(shop_order, rider_location, pay_settings):
    pickup = _get_shop_order_pickup_location(shop_order)
    delivery = _get_order_delivery_coordinates(shop_order.order)
    rider_coordinates = rider_location if _valid_route_coordinates(rider_location) else None
    legacy_road_network_name = getattr(
        pay_settings.road_network_geojson,
        "name",
        "",
    )
    road_network_names = tuple(
        name
        for name in (
            legacy_road_network_name,
            *pay_settings.road_network_files.values_list(
                "geojson_file",
                flat=True,
            ),
        )
        if name
    )
    pickup_to_delivery = None
    rider_to_pickup = None
    straight_line_rider_to_pickup = None
    straight_line_pickup_to_delivery = None
    include_pickup_distance = bool(
        pay_settings.public_rider_pay_include_pickup_distance
    )
    pickup_distance_pay_percentage = (
        pay_settings.public_rider_pickup_distance_pay_percentage
        if include_pickup_distance
        else Decimal("0.00")
    )
    pickup_distance_pay_fraction = (
        pickup_distance_pay_percentage / Decimal("100.00")
    )
    threshold_bonus_enabled = bool(
        pay_settings.public_rider_pickup_threshold_bonus_enabled
    )
    pickup_threshold_km = (
        pay_settings.public_rider_pickup_threshold_km
    )
    pickup_threshold_rate_per_km = (
        pay_settings.public_rider_pickup_threshold_rate_per_km
    )
    threshold_distance_method = (
        pay_settings.public_rider_pickup_threshold_distance_method
    )
    unavailable_reason = ""
    if not road_network_names:
        unavailable_reason = (
            "An administrator must upload local GeoJSON road coverage."
        )
    elif not pickup:
        unavailable_reason = "The selected shop pickup point is missing or invalid."
    elif not delivery:
        unavailable_reason = (
            "The order delivery coordinates are missing or invalid."
        )
    else:
        if road_network_names:
            pickup_to_delivery_km = local_road_route_distance_km(
                road_network_names,
                pickup,
                delivery,
            )
            if pickup_to_delivery_km is None:
                unavailable_reason = (
                    "No connected road route covers the selected pickup point and delivery "
                    "locations. Check the uploaded road coverage and coordinates."
                )
            else:
                pickup_to_delivery = Decimal(
                    str(pickup_to_delivery_km),
                ).quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                )
        if rider_coordinates and road_network_names:
            rider_to_pickup_km = local_road_route_distance_km(
                road_network_names,
                rider_coordinates,
                pickup,
            )
            if rider_to_pickup_km is not None:
                rider_to_pickup = Decimal(
                    str(rider_to_pickup_km),
                ).quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                )
            elif pickup_distance_pay_fraction and not unavailable_reason:
                unavailable_reason = (
                    "No connected road route covers your location and "
                    "the pickup point; this distance cannot be included "
                    "in rider pay."
                )
        elif not rider_coordinates and pickup_distance_pay_fraction:
            unavailable_reason = (
                "Allow location access or update your rider location to "
                "calculate the rider-to-pickup distance."
            )
    if rider_coordinates and pickup:
        rider_distance = distance_km_between_points(
            rider_coordinates["lat"],
            rider_coordinates["lng"],
            pickup["lat"],
            pickup["lng"],
        )
        if rider_distance is not None:
            straight_line_rider_to_pickup = Decimal(
                str(rider_distance),
            ).quantize(
                Decimal("0.001"),
                rounding=ROUND_HALF_UP,
            )
    if pickup and delivery:
        pickup_delivery_distance = distance_km_between_points(
            pickup["lat"],
            pickup["lng"],
            delivery["lat"],
            delivery["lng"],
        )
        if pickup_delivery_distance is not None:
            straight_line_pickup_to_delivery = Decimal(
                str(pickup_delivery_distance),
            ).quantize(
                Decimal("0.001"),
                rounding=ROUND_HALF_UP,
            )
    road_total_distance = (
        rider_to_pickup + pickup_to_delivery
        if rider_to_pickup is not None and pickup_to_delivery is not None
        else None
    )
    road_pay_distance = None
    if pickup_to_delivery is not None and (
        not pickup_distance_pay_fraction
        or rider_to_pickup is not None
    ):
        road_pay_distance = (
            pickup_to_delivery
            + (rider_to_pickup or Decimal("0.000"))
            * pickup_distance_pay_fraction
        )
    straight_line_total_distance = None
    if road_pay_distance is None and pickup and delivery:
        if not rider_coordinates and not pickup_distance_pay_fraction:
            straight_line_rider_to_pickup = Decimal("0.000")
        if (
            straight_line_pickup_to_delivery is not None
            and (
                not pickup_distance_pay_fraction
                or straight_line_rider_to_pickup is not None
            )
        ):
            straight_line_total_distance = (
                (
                    straight_line_pickup_to_delivery
                    + (straight_line_rider_to_pickup or Decimal("0.000"))
                    * pickup_distance_pay_fraction
                )
                * Decimal("1.7")
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    threshold_distance_km = None
    threshold_distance_source = "unavailable"
    if threshold_bonus_enabled:
        if threshold_distance_method == "ROAD" and rider_to_pickup is not None:
            threshold_distance_km = rider_to_pickup
            threshold_distance_source = "road"
        elif (
            threshold_distance_method == "STRAIGHT_LINE"
            and straight_line_rider_to_pickup is not None
        ):
            threshold_distance_km = straight_line_rider_to_pickup
            threshold_distance_source = "straight_line"
        elif rider_to_pickup is not None:
            threshold_distance_km = rider_to_pickup
            threshold_distance_source = "road"
        elif straight_line_rider_to_pickup is not None:
            threshold_distance_km = straight_line_rider_to_pickup
            threshold_distance_source = "straight_line"
    threshold_bonus_distance_km = (
        max(
            Decimal("0.000"),
            threshold_distance_km - pickup_threshold_km,
        ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if threshold_distance_km is not None
        else Decimal("0.000")
    )
    pickup_threshold_bonus = (
        (
            threshold_bonus_distance_km
            * pickup_threshold_rate_per_km
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if threshold_bonus_enabled
        else Decimal("0.00")
    )
    distance_calculation_method = (
        "road" if road_pay_distance is not None
        else "straight_line_fallback"
        if straight_line_total_distance is not None
        else "unavailable"
    )
    estimated_pay_distance = (
        road_pay_distance
        if road_pay_distance is not None
        else straight_line_total_distance
    )
    estimated_pay_before_threshold_bonus = (
        (
            pay_settings.shared_rider_base_pay
            + estimated_pay_distance * pay_settings.shared_rider_rate_per_km
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if estimated_pay_distance is not None
        else None
    )
    estimated_pay = (
        (
            estimated_pay_before_threshold_bonus
            + pickup_threshold_bonus
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if estimated_pay_before_threshold_bonus is not None
        else None
    )
    load_estimate = _shop_order_estimated_load(shop_order)
    return {
        "rider_location": rider_coordinates,
        "rider_to_pickup_km": rider_to_pickup,
        "pickup_to_delivery_km": pickup_to_delivery,
        "total_distance_km": (
            road_total_distance.quantize(
                Decimal("0.001"),
                rounding=ROUND_HALF_UP,
            )
            if road_total_distance is not None
            else None
        ),
        "straight_line_rider_to_pickup_km": straight_line_rider_to_pickup,
        "straight_line_pickup_to_delivery_km": straight_line_pickup_to_delivery,
        "estimated_pay_distance_km": estimated_pay_distance,
        "pickup_distance_included": include_pickup_distance,
        "pickup_distance_pay_percentage": pickup_distance_pay_percentage,
        "pickup_threshold_bonus_enabled": threshold_bonus_enabled,
        "pickup_threshold_km": pickup_threshold_km,
        "pickup_threshold_rate_per_km": pickup_threshold_rate_per_km,
        "pickup_threshold_distance_method": threshold_distance_method,
        "pickup_threshold_distance_km": threshold_distance_km,
        "pickup_threshold_distance_source": threshold_distance_source,
        "pickup_threshold_bonus_distance_km": threshold_bonus_distance_km,
        "pickup_threshold_bonus": pickup_threshold_bonus,
        "estimated_pay_before_threshold_bonus": (
            estimated_pay_before_threshold_bonus
        ),
        "distance_calculation_method": distance_calculation_method,
        "base_fare": pay_settings.shared_rider_base_pay,
        "rate_per_km": pay_settings.shared_rider_rate_per_km,
        "estimated_pay": estimated_pay,
        "item_count": load_estimate["item_count"],
        "weight_kg": load_estimate["weight_kg"],
        "volume_cubic_ft": load_estimate["volume_cubic_ft"],
        "calculated_at": timezone.now().isoformat(),
        "unavailable_reason": unavailable_reason,
        "needs_rider_location": (
            pickup is not None and delivery is not None and rider_coordinates is None
        ),
    }


def _public_rider_acceptance_location(request, rider_membership):
    now_timestamp = timezone.now().timestamp()
    try:
        posted_location = {
            "lat": float(request.POST.get("rider_latitude", "")),
            "lng": float(request.POST.get("rider_longitude", "")),
            "observed_at": float(request.POST.get("rider_location_observed_at", "")),
        }
    except (TypeError, ValueError):
        posted_location = None
    if (
        posted_location
        and _valid_route_coordinates(posted_location)
        and 0 <= now_timestamp - posted_location["observed_at"] <= 120
    ):
        return posted_location

    stored_location = rider_membership.last_location
    if not isinstance(stored_location, dict) or not _valid_route_coordinates(stored_location):
        return None
    try:
        observed_at = float(stored_location.get("observed_at"))
    except (TypeError, ValueError):
        return None
    if not 0 <= now_timestamp - observed_at <= 600:
        return None
    return {
        "lat": float(stored_location["lat"]),
        "lng": float(stored_location["lng"]),
        "observed_at": observed_at,
    }


def _nearby_shared_rider_shops(rider_shop):
    configured_radius = DeliverySharingSettings.objects.filter(
        pk=1,
    ).values_list("shared_rider_radius_km", flat=True).first()
    radius_km = min(
        float(
            configured_radius
            if configured_radius is not None
            else Decimal("0.35")
        ),
        SHARED_RIDER_ASSIGNMENT_RADIUS_KM,
    )
    rider_location = _get_shop_coordinates(rider_shop)
    if rider_location is None:
        return [], {}, radius_km

    shared_shops = Shop.objects.filter(
        share_riders_with_nearby_shops=True,
    ).exclude(pk=rider_shop.pk).only("id", "location", "name")
    nearby_shop_ids = []
    distances = {}
    for shop in shared_shops:
        shop_location = _get_shop_coordinates(shop)
        if shop_location is None:
            continue
        distance = distance_km_between_points(
            rider_location["lat"],
            rider_location["lng"],
            shop_location["lat"],
            shop_location["lng"],
        )
        if distance is not None and distance <= radius_km:
            nearby_shop_ids.append(shop.pk)
            distances[shop.pk] = distance
    return nearby_shop_ids, distances, radius_km


def _shared_rider_group_shop_ids(rider):
    approved_group_ids = SharedRiderGroupShop.objects.filter(
        shop_id=rider.shop_id,
        status=SharedRiderGroupShop.STATUS_APPROVED,
    ).values_list("group_id", flat=True)
    return list(
        SharedRiderGroupShop.objects.filter(
            group_id__in=approved_group_ids,
            status=SharedRiderGroupShop.STATUS_APPROVED,
        ).exclude(shop_id=rider.shop_id).values_list("shop_id", flat=True).distinct()
    )


def _get_rider_pickup_navigation_stops(rider, rider_user=None):
    orders = ShopOrder.objects.filter(
        status__in=["READY", "RIDER_ASSIGNED"],
        delivery_completed_at__isnull=True,
    )
    orders = orders.filter(
        assigned_rider__user=rider_user,
    ) if rider_user else orders.filter(
        assigned_rider=rider,
    )
    orders = (
        orders
        .select_related("shop")
        .order_by("shop__name", "created_at", "id")
    )
    stops_by_location = {}
    for shop_order in orders:
        pickup_location = _get_shop_order_pickup_location(shop_order)
        coordinates = (
            {"lat": pickup_location["lat"], "lng": pickup_location["lng"]}
            if pickup_location
            else None
        )
        if not coordinates:
            continue
        stop_key = (
            shop_order.shop_id,
            round(coordinates["lat"], 6),
            round(coordinates["lng"], 6),
        )
        order_number = shop_order.shop_order_number or shop_order.order.order_id
        if stop_key not in stops_by_location:
            stops_by_location[stop_key] = {
                "shop_name": shop_order.shop.name,
                "pickup_location_name": pickup_location.get(
                    "name",
                    shop_order.shop.name,
                ),
                "pickup_address": pickup_location.get("address", ""),
                "order_count": 1,
                "order_numbers": [order_number],
                **coordinates,
            }
            continue
        stops_by_location[stop_key]["order_count"] += 1
        stops_by_location[stop_key]["order_numbers"].append(order_number)
    return list(stops_by_location.values())


RIDER_PICKUP_OTP_VALIDITY_SECONDS = 300
RIDER_PICKUP_OTP_MAX_ATTEMPTS = 5
RIDER_PICKUP_QR_PREFIX = "SH-TASK:VERIFY_RIDER_PICKUP:"
LEGACY_RIDER_PICKUP_QR_PREFIX = "SH-PICKUP:"


def _rider_pickup_otp_from_qr_value(value):
    value = (value or "").strip()
    for prefix in (
        RIDER_PICKUP_QR_PREFIX,
        LEGACY_RIDER_PICKUP_QR_PREFIX,
    ):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value
SHARED_RIDER_ASSIGNMENT_RADIUS_KM = 0.35
SHARED_RIDER_OFFER_DELAY = timedelta(minutes=1)


def _shared_rider_assignment_radius_km():
    configured_radius = DeliverySharingSettings.objects.filter(
        pk=1,
    ).values_list("shared_rider_radius_km", flat=True).first()
    if configured_radius is None:
        return SHARED_RIDER_ASSIGNMENT_RADIUS_KM
    return float(configured_radius)


def _delivery_sharing_settings():
    return DeliverySharingSettings.objects.get_or_create(pk=1)[0]


def _pickup_otp_response(request, message, *, success, shop_order=None, fallback_url):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "success": success,
                "message": message,
                "redirect_url": (
                    reverse(
                        "shop_order_detail",
                        kwargs={"shop_order_id": shop_order.pk},
                    )
                    if success and shop_order
                    else ""
                ),
            },
            status=200 if success else 400,
        )
    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)
    return redirect(fallback_url)


def _ensure_rider_pickup_otp(shop_order):
    now = timezone.now()
    if (
        shop_order.rider_pickup_otp
        and shop_order.rider_pickup_otp_expires_at
        and shop_order.rider_pickup_otp_expires_at > now
        and shop_order.rider_pickup_otp_attempts < RIDER_PICKUP_OTP_MAX_ATTEMPTS
    ):
        return shop_order.rider_pickup_otp

    while True:
        otp = f"{secrets.randbelow(1_000_000):06d}"
        already_active = ShopOrder.objects.filter(
            rider_pickup_otp=otp,
            rider_pickup_otp_expires_at__gt=now,
        ).exclude(pk=shop_order.pk).exists()
        if not already_active:
            break

    shop_order.rider_pickup_otp = otp
    shop_order.rider_pickup_otp_expires_at = now + timedelta(
        seconds=RIDER_PICKUP_OTP_VALIDITY_SECONDS,
    )
    shop_order.rider_pickup_otp_attempts = 0
    shop_order.save(update_fields=[
        "rider_pickup_otp",
        "rider_pickup_otp_expires_at",
        "rider_pickup_otp_attempts",
        "updated_at",
    ])
    return otp


def _rider_last_accepted_shop(rider):
    last_accepted_order = (
        ShopOrder.objects.filter(
            assigned_rider=rider,
            rider_accepted_at__isnull=False,
        )
        .select_related("shop")
        .order_by("-rider_accepted_at", "-id")
        .first()
    )
    if last_accepted_order:
        return last_accepted_order.shop

    previous_assigned_order = (
        ShopOrder.objects.filter(assigned_rider=rider)
        .select_related("shop")
        .order_by("-updated_at", "-id")
        .first()
    )
    return previous_assigned_order.shop if previous_assigned_order else rider.shop


def _shop_order_ready_at(shop_order):
    ready_history = getattr(shop_order, "ready_history", None)
    if ready_history is None:
        ready_history = list(
            shop_order.status_history.filter(status="READY").order_by("-created_at")[:1]
        )
    return ready_history[0].created_at if ready_history else shop_order.created_at


def _shop_distance_km(first_shop, second_shop):
    first_location = _get_shop_coordinates(first_shop)
    second_location = _get_shop_coordinates(second_shop)
    if not first_location or not second_location:
        return None
    return distance_km_between_points(
        first_location["lat"],
        first_location["lng"],
        second_location["lat"],
        second_location["lng"],
    )


def _shop_is_toward_customer(
    origin_shop,
    candidate_shop,
    order,
    *,
    cross_track_radius_km,
):
    origin = _get_shop_coordinates(origin_shop)
    candidate = _get_shop_coordinates(candidate_shop)
    destination = _get_order_delivery_coordinates(order)
    if not origin or not candidate or not destination:
        return False

    latitude_scale = 111.0
    longitude_scale = 111.0 * math.cos(math.radians(origin["lat"]))
    route_x = (destination["lng"] - origin["lng"]) * longitude_scale
    route_y = (destination["lat"] - origin["lat"]) * latitude_scale
    candidate_x = (candidate["lng"] - origin["lng"]) * longitude_scale
    candidate_y = (candidate["lat"] - origin["lat"]) * latitude_scale
    route_length = math.hypot(route_x, route_y)
    if route_length == 0:
        return False

    along_route_km = (
        candidate_x * route_x + candidate_y * route_y
    ) / route_length
    cross_track_km = abs(
        candidate_x * route_y - candidate_y * route_x
    ) / route_length
    direction_radius = float(
        _delivery_sharing_settings().customer_direction_radius_km
    )
    return (
        0 < along_route_km <= direction_radius
        and cross_track_km <= cross_track_radius_km
    )


def _rider_can_accept_shop_orders(
    rider,
    shop,
    *,
    origin_shop=None,
    group_shop_ids=None,
    radius_km=None,
    customer_order=None,
):
    if rider.shop_id == shop.pk:
        return True
    if group_shop_ids is None:
        group_shop_ids = set(_shared_rider_group_shop_ids(rider))
    if shop.pk in group_shop_ids:
        return True
    if not shop.share_riders_with_nearby_shops:
        return False
    origin_shop = origin_shop or _rider_last_accepted_shop(rider)
    distance = _shop_distance_km(origin_shop, shop)
    radius_km = (
        _shared_rider_assignment_radius_km()
        if radius_km is None
        else radius_km
    )
    if distance is not None and distance <= radius_km:
        return True
    return bool(
        customer_order
        and _shop_is_toward_customer(
            origin_shop,
            shop,
            customer_order,
            cross_track_radius_km=radius_km,
        )
    )


def _rider_membership_for_order(user, shop_order):
    assigned_rider = shop_order.assigned_rider
    if (
        assigned_rider
        and assigned_rider.user_id == user.pk
        and assigned_rider.is_active
        and assigned_rider.role == "RIDER"
    ):
        return assigned_rider
    return ShopStaff.objects.filter(
        user=user,
        shop=shop_order.shop,
        is_active=True,
        role="RIDER",
    ).first()


def _approved_public_rider(user):
    return PublicRiderApplication.objects.filter(
        user=user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).first()


def app_download_page(request):
    branding = SiteBrandingSettings.objects.first()
    if not branding or not (branding.android_app_url or branding.ios_app_url):
        raise Http404("App downloads are not configured.")
    app_downloads = []
    for platform, label, url in (
        ("android", "Download for Android", branding.android_app_url),
        ("ios", "Download for iPhone or iPad", branding.ios_app_url),
    ):
        if url:
            app_downloads.append({
                "platform": platform,
                "label": label,
                "url": url,
                "qr_code_data_uri": _qr_code_data_uri(url, box_size=5),
            })
    return render(
        request,
        "seller/app_download.html",
        {"app_downloads": app_downloads},
    )


def _upi_payment_qr_data_uri(upi_id, payee_name, amount, order_reference):
    try:
        payment_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not upi_id or not payment_amount.is_finite() or payment_amount <= 0:
        return ""

    payment_uri = "upi://pay?" + urlencode({
        "pa": upi_id,
        "pn": payee_name[:80],
        "am": format(payment_amount.quantize(Decimal("0.01")), ".2f"),
        "cu": "INR",
        "tn": f"Order {order_reference}"[:80],
    })
    return _qr_code_data_uri(payment_uri, box_size=5)


def _next_cod_upi_qr_amount(shop, settlement_amount):
    cent = Decimal("0.01")
    amount = Decimal(settlement_amount).quantize(cent)
    minimum_amount = max(cent, amount - Decimal("1.00"))
    active_amounts = set(
        CODPaymentApprovalRequest.objects.filter(
            shop=shop,
            status__in=CODPaymentApprovalRequest.ACTIVE_STATUSES,
        ).values_list("qr_amount", flat=True)
    )
    while amount >= minimum_amount:
        if amount not in active_amounts:
            return amount
        amount -= cent
    return None


def _can_review_cod_payment(user, shop):
    return bool(
        user.is_superuser
        or shop.owner_id == user.pk
        or ShopStaff.objects.filter(
            shop=shop,
            user=user,
            is_active=True,
        ).filter(
            Q(role="MANAGER")
            | Q(role="ORDER", can_view_orders=True)
            | Q(
                role="PAYMENT_MANAGER",
                can_confirm_rider_payments=True,
            )
        ).exists()
    )


def _notify_cod_payment_reviewers(payment_request, *, reminder=False):
    shop = payment_request.shop
    reviewer_user_ids = set(
        ShopStaff.objects.filter(
            shop=shop,
            is_active=True,
        ).filter(
            Q(role="MANAGER")
            | Q(role="ORDER", can_view_orders=True)
            | Q(
                role="PAYMENT_MANAGER",
                can_confirm_rider_payments=True,
            )
        ).values_list("user_id", flat=True)
    )
    if shop.owner_id:
        reviewer_user_ids.add(shop.owner_id)
    order_reference = (
        payment_request.shop_order.shop_order_number
        or payment_request.shop_order.order.order_id
    )
    for user_id in reviewer_user_ids:
        Notification.objects.create(
            user_id=user_id,
            title=(
                "Rider payment reminder"
                if reminder
                else "COD UPI payment needs approval"
            ),
            message=(
                (
                    f"The rider is waiting for confirmation of the ₹"
                    f"{payment_request.qr_amount:.2f} UPI payment for order "
                    f"{order_reference}. Review it against the ₹"
                    f"{payment_request.settlement_amount:.2f} COD balance."
                )
                if reminder
                else (
                    f"Rider reported a ₹{payment_request.qr_amount:.2f} UPI payment "
                    f"for order {order_reference}. Review it against the "
                    f"₹{payment_request.settlement_amount:.2f} COD balance."
                )
            ),
            notification_type="ORDER",
            related_url=reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": payment_request.shop_order_id},
            ),
        )


def _notify_cod_payment_rider(payment_request, title, message):
    view = (
        "completed"
        if payment_request.status == "APPROVED"
        else "active"
    )
    Notification.objects.create(
        user=payment_request.rider.user,
        title=title,
        message=message,
        notification_type="ORDER",
        related_url=reverse(
            "rider_dashboard",
        ) + f"?view={view}#order-{payment_request.shop_order_id}",
    )


def _public_rider_earnings_summary(public_rider, period):
    valid_periods = {"today", "yesterday", "weekly", "monthly", "yearly"}
    period_key = period if period in valid_periods else "today"
    now = timezone.now()
    local_now = timezone.localtime(now)
    if period_key == "today":
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        bucket_size = timedelta(hours=4)
        bucket_count = 6
        label_lambda = lambda idx: f"{(idx * 4):02d}:00"
    elif period_key == "yesterday":
        start = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        bucket_size = timedelta(hours=4)
        bucket_count = 6
        label_lambda = lambda idx: f"{(idx * 4):02d}:00"
    elif period_key == "weekly":
        start = local_now - timedelta(days=6)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = local_now + timedelta(days=1)
        bucket_size = timedelta(days=1)
        bucket_count = 7
        label_lambda = lambda idx: (start + timedelta(days=idx)).strftime("%a")
    elif period_key == "monthly":
        start = local_now - timedelta(days=29)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = local_now + timedelta(days=1)
        bucket_size = timedelta(days=5)
        bucket_count = 6
        label_lambda = lambda idx: (start + timedelta(days=idx * 5)).strftime("%d %b")
    else:
        start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
        bucket_size = timedelta(days=30)
        bucket_count = 12
        label_lambda = lambda idx: (start.replace(month=(start.month + idx - 1) % 12 + 1, year=start.year + ((start.month + idx - 1) // 12))).strftime("%b")

    base_transactions = PublicRiderWalletTransaction.objects.filter(
        wallet__rider=public_rider,
        transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
    )
    if period_key in {"today", "yesterday"}:
        filtered_queryset = base_transactions.filter(created_at__gte=start, created_at__lt=end)
    elif period_key == "weekly":
        filtered_queryset = base_transactions.filter(created_at__date__gte=start.date(), created_at__date__lte=local_now.date())
    elif period_key == "monthly":
        filtered_queryset = base_transactions.filter(created_at__date__gte=start.date(), created_at__date__lte=local_now.date())
    else:
        filtered_queryset = base_transactions.filter(created_at__gte=start, created_at__lt=end)

    total = filtered_queryset.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    completed_orders = filtered_queryset.values("shop_order_id").distinct().count()
    average = (total / Decimal(completed_orders)) if completed_orders else Decimal("0.00")
    chart_points = []
    max_value = Decimal("0.00")
    for idx in range(bucket_count):
        if period_key in {"today", "yesterday"}:
            bucket_start = start + timedelta(hours=idx * 4)
            bucket_end = bucket_start + bucket_size
            label = label_lambda(idx)
        elif period_key == "weekly":
            bucket_start = start + timedelta(days=idx)
            bucket_end = bucket_start + bucket_size
            label = label_lambda(idx)
        elif period_key == "monthly":
            bucket_start = start + timedelta(days=idx * 5)
            bucket_end = min(bucket_start + bucket_size, end)
            label = label_lambda(idx)
        else:
            month_number = ((start.month - 1 + idx) % 12) + 1
            year_number = start.year + ((start.month - 1 + idx) // 12)
            bucket_start = local_now.replace(year=year_number, month=month_number, day=1, hour=0, minute=0, second=0, microsecond=0)
            bucket_end = bucket_start.replace(year=bucket_start.year + (1 if bucket_start.month == 12 else 0), month=(bucket_start.month % 12) + 1, day=1)
            label = label_lambda(idx)
        bucket_total = filtered_queryset.filter(
            created_at__gte=bucket_start,
            created_at__lt=bucket_end,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        if bucket_total > max_value:
            max_value = bucket_total
        chart_points.append({
            "label": label,
            "value": bucket_total,
        })

    if max_value > Decimal("0.00"):
        for point in chart_points:
            height = (float(point["value"]) / float(max_value)) * 100 if max_value else 0
            point["height_percent"] = round(height, 2)
    else:
        for point in chart_points:
            point["height_percent"] = 0

    best_day = None
    if period_key in {"today", "yesterday"}:
        best_day = max(chart_points, key=lambda item: item["value"], default={"label": "n/a", "value": Decimal("0.00")})
    else:
        best_day = max(chart_points, key=lambda item: item["value"], default={"label": "n/a", "value": Decimal("0.00")})

    return {
        "period": period_key,
        "period_label": {
            "today": "Today",
            "yesterday": "Yesterday",
            "weekly": "This week",
            "monthly": "This month",
            "yearly": "This year",
        }[period_key],
        "total": total,
        "orders": completed_orders,
        "average": average,
        "wallet_balance": public_rider.earnings_wallet.balance if hasattr(public_rider, "earnings_wallet") else Decimal("0.00"),
        "best_day": best_day,
        "chart": chart_points,
    }


@login_required
def _rider_available_order_ids(
    staff,
    public_rider,
    user,
    rider_load_weight,
    rider_load_volume,
    *,
    dispatch_public_offers=False,
):
    if public_rider and not public_rider_service_enabled():
        return [], {}, None
    now = timezone.now()
    if ShopOrder.objects.filter(
        assigned_rider__user=user,
        order__is_valid_order=True,
        status="OUT_FOR_DELIVERY",
        delivery_completed_at__isnull=True,
    ).exists():
        return [], {}, None
    recent_delivery = ShopOrder.objects.filter(
        assigned_rider__user=user,
        order__is_valid_order=True,
        delivery_completed_at__gte=(
            now
            - timedelta(
                seconds=RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
            )
        ),
    ).aggregate(last_completed_at=Max("delivery_completed_at"))[
        "last_completed_at"
    ]
    if recent_delivery:
        remaining_seconds = max(
            1,
            math.ceil(
                (
                    recent_delivery
                    + timedelta(
                        seconds=RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
                    )
                    - now
                ).total_seconds()
            ),
        )
        return [], {}, remaining_seconds
    if public_rider:
        rider_shop_ids = set()
        group_shop_ids = set()
    else:
        rider_memberships = list(
            ShopStaff.objects.filter(
                user=user,
                is_active=True,
                role="RIDER",
                is_public_rider=False,
            ).only("id", "shop_id")
        )
        rider_shop_ids = {
            rider_membership.shop_id
            for rider_membership in rider_memberships
        }
        group_shop_ids = set()
        for rider_membership in rider_memberships:
            group_shop_ids.update(
                _shared_rider_group_shop_ids(rider_membership),
            )
    last_accepted_shop = (
        None if public_rider else _rider_last_accepted_shop(staff)
    )
    shared_rider_radius_km = _shared_rider_assignment_radius_km()
    ready_candidates = (
        ShopOrder.objects.filter(
            shop__public_rider_network_enabled=True,
        ).filter(
            Q(public_rider_network_requested=True)
            | Q(shop__rider_delivery_preference="PUBLIC"),
        ).filter(
            status="READY",
            assigned_rider__isnull=True,
            delivery_completed_at__isnull=True,
            public_rider_fallback_to_staff=False,
        )
        if public_rider
        else ShopOrder.objects.filter(
            Q(shop_id__in=rider_shop_ids)
            | Q(shop__share_riders_with_nearby_shops=True)
            | Q(shop_id__in=group_shop_ids),
        ).filter(
            Q(
                shop__rider_delivery_preference="STAFF",
                public_rider_network_requested=False,
            )
            | Q(public_rider_fallback_to_staff=True),
        ).filter(
            status="READY",
            assigned_rider__isnull=True,
            delivery_completed_at__isnull=True,
        )
    )
    if public_rider:
        if dispatch_public_offers:
            for candidate_id in ready_candidates.values_list("pk", flat=True):
                advance_public_rider_offer(candidate_id)
        ready_candidates = ready_candidates.filter(
            public_rider_offer_user=user,
            public_rider_offer_expires_at__gt=timezone.now(),
        )
    ready_candidates = list(
        ready_candidates
        .select_related("shop")
        .prefetch_related(
            "order_items",
            Prefetch(
                "status_history",
                queryset=OrderStatusHistory.objects.filter(
                    status="READY",
                ).order_by("-created_at"),
                to_attr="ready_history",
            ),
        )
        .order_by("-created_at", "-id")
    )
    available_order_ids = []
    shared_shop_distances = {}
    next_shared_order_available_in = None
    for candidate in ready_candidates:
        is_home_shop_order = candidate.shop_id in rider_shop_ids
        if (
            not public_rider
            and not is_home_shop_order
            and not _rider_can_accept_shop_orders(
                staff,
                candidate.shop,
                origin_shop=last_accepted_shop,
                group_shop_ids=group_shop_ids,
                radius_km=shared_rider_radius_km,
                customer_order=candidate.order,
            )
        ):
            continue
        if not public_rider and not is_home_shop_order:
            available_at = _shop_order_ready_at(candidate) + SHARED_RIDER_OFFER_DELAY
            distance = _shop_distance_km(last_accepted_shop, candidate.shop)
            if distance is not None:
                shared_shop_distances[candidate.shop_id] = distance
            if available_at > now:
                seconds_until_available = max(
                    1,
                    math.ceil((available_at - now).total_seconds()),
                )
                if (
                    next_shared_order_available_in is None
                    or seconds_until_available < next_shared_order_available_in
                ):
                    next_shared_order_available_in = seconds_until_available
                continue
        candidate_load = _shop_order_estimated_load(candidate)
        if not public_rider and not _rider_load_fits_capacity(
            staff,
            rider_load_weight + candidate_load["weight_kg"],
            rider_load_volume + candidate_load["volume_cubic_ft"],
        ):
            continue
        available_order_ids.append(candidate.pk)
    return (
        available_order_ids,
        shared_shop_distances,
        next_shared_order_available_in,
    )


def rider_dashboard(request):

    public_rider = None
    public_rider_last_location = None
    if request.session.get("active_portal") == "RIDER":
        public_rider = PublicRiderApplication.objects.select_for_update().filter(
            user=request.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).first()
        if not public_rider:
            raise PermissionDenied
        sync_public_rider_shop_memberships(public_rider)
        public_rider_last_location = _recent_public_rider_location(request.user)

    rider_memberships = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        role="RIDER",
    ).select_related("shop")
    if not public_rider:
        rider_memberships = rider_memberships.filter(is_public_rider=False)
    staff = rider_memberships.order_by("shop_id", "pk").first()

    if not staff and not public_rider:
        raise PermissionDenied
    if not staff:
        public_rider_wallet = PublicRiderWallet.objects.filter(
            rider=public_rider,
        ).first()
        selected_view = request.GET.get("view", "available")
        earnings_view = selected_view == "earnings"
        earnings_period = request.GET.get("period", "today")
        public_rider_earnings_summary = _public_rider_earnings_summary(
            public_rider,
            earnings_period,
        ) if public_rider else None
        return render(
            request,
            "seller/public_rider_dashboard.html",
            {
                "public_rider": public_rider,
                "public_rider_last_location": public_rider_last_location,
                "public_rider_wallet": public_rider_wallet,
                "public_rider_wallet_transactions": (
                    public_rider_wallet.transactions.select_related(
                        "shop_order",
                        "shop_order__order",
                        "shop_order__shop",
                    )[:10]
                    if public_rider_wallet
                    else []
                ),
                "public_rider_earnings_summary": public_rider_earnings_summary,
                "earnings_view": earnings_view,
                "earnings_period": earnings_period,
                "selected_view": selected_view,
                "no_network_shops": True,
            },
        )

    public_rider_wallet = (
        PublicRiderWallet.objects.filter(rider=public_rider).first()
        if public_rider
        else None
    )
    public_rider_wallet_transactions = (
        public_rider_wallet.transactions.select_related(
            "shop_order",
            "shop_order__order",
            "shop_order__shop",
        )[:10]
        if public_rider_wallet
        else []
    )

    selected_view = request.GET.get("view", "available")
    performance_view = selected_view == "performance"
    completed_view = selected_view == "completed"
    all_completed_view = selected_view == "all_completed"
    active_view = selected_view == "active"
    verified_view = selected_view == "verified"
    handover_view = selected_view == "handover"
    earnings_view = selected_view == "earnings"
    earnings_period = request.GET.get("period", "today")
    public_rider_earnings_summary = None
    if public_rider:
        public_rider_earnings_summary = _public_rider_earnings_summary(
            public_rider,
            earnings_period,
        )
    available_view = not (
        performance_view
        or completed_view
        or all_completed_view
        or active_view
        or verified_view
        or handover_view
        or earnings_view
    )
    rider_orders = (
        ShopOrder.objects.filter(
            Q(assigned_rider__user=request.user)
            | Q(
                rider_accepted_at__isnull=False,
                order__assigned_rider=request.user.username,
            )
            if public_rider
            else Q(assigned_rider=staff)
            | Q(
                rider_accepted_at__isnull=False,
                order__assigned_rider=staff.user.username,
            )
        )
        .filter(order__is_valid_order=True)
    )
    assigned_orders = rider_orders
    completed_orders_queryset = assigned_orders.filter(
        Q(delivery_completed_at__isnull=False)
        | Q(status__in=["DELIVERED", "COMPLETED", "RETURNED"])
    )
    today = timezone.localdate()
    today_completed_orders_queryset = completed_orders_queryset.filter(
        Q(delivery_completed_at__date=today)
        | Q(
            delivery_completed_at__isnull=True,
            status__in=["DELIVERED", "COMPLETED", "RETURNED"],
            updated_at__date=today,
        )
    )
    active_load_orders = list(
        assigned_orders.filter(
            status__in=["READY", "RIDER_ASSIGNED", "PICKED_UP", "OUT_FOR_DELIVERY"],
            delivery_completed_at__isnull=True,
        ).exclude(
            status__in=["DELIVERED", "COMPLETED", "CANCELLED"],
        ).prefetch_related("order_items")
    )
    if (
        public_rider
        and not active_load_orders
        and (
            public_rider.public_rider_batch_accepting
            or public_rider.public_rider_batch_started_at
        )
    ):
        public_rider.public_rider_batch_accepting = False
        public_rider.public_rider_batch_started_at = None
        public_rider.save(update_fields=[
            "public_rider_batch_accepting",
            "public_rider_batch_started_at",
            "updated_at",
        ])
    active_delivery_in_progress = assigned_orders.filter(
        status="OUT_FOR_DELIVERY",
        delivery_completed_at__isnull=True,
    ).exists()
    now = timezone.now()
    last_recent_delivery = assigned_orders.filter(
        delivery_completed_at__gte=(
            now
            - timedelta(
                seconds=RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
            )
        ),
    ).aggregate(last_completed_at=Max("delivery_completed_at"))[
        "last_completed_at"
    ]
    rider_offer_cooldown_seconds = (
        max(
            1,
            math.ceil(
                (
                    last_recent_delivery
                    + timedelta(
                        seconds=RIDER_POST_DELIVERY_OFFER_DELAY_SECONDS,
                    )
                    - now
                ).total_seconds()
            ),
        )
        if last_recent_delivery
        else 0
    )
    if available_view and active_delivery_in_progress:
        selected_view = "active"
        available_view = False
        active_view = True
    elif available_view and rider_offer_cooldown_seconds:
        selected_view = "completed"
        available_view = False
        completed_view = True
    public_rider_batch_window_elapsed = bool(
        public_rider
        and public_rider.public_rider_batch_accepting
        and public_rider.public_rider_batch_started_at
        and public_rider.public_rider_batch_started_at
        + timedelta(minutes=5) <= timezone.now()
    )
    public_rider_collection_window_open = bool(
        public_rider
        and public_rider.public_rider_batch_accepting
        and public_rider.public_rider_batch_started_at
        and public_rider.public_rider_batch_started_at
        + timedelta(minutes=5) > timezone.now()
        and active_load_orders
        and all(
            active.status in {"READY", "RIDER_ASSIGNED"}
            for active in active_load_orders
        )
    )
    public_rider_collection_window_expired = (
        public_rider_batch_window_elapsed
        and any(
            active.status in {"READY", "RIDER_ASSIGNED"}
            for active in active_load_orders
        )
    )
    public_rider_collection_window_closed = bool(
        public_rider
        and active_load_orders
        and any(
            active.status in {"READY", "RIDER_ASSIGNED"}
            for active in active_load_orders
        )
        and (
            public_rider_collection_window_expired
            or not public_rider.public_rider_batch_accepting
        )
    )
    if public_rider_collection_window_expired:
        collection_notification_title = "Collection window closed"
        collection_notification_message = (
            "Your five-minute acceptance window expired. Go to the shops and "
            "collect your accepted parcels; scan each shop's printed order QR "
            "after staff verifies your pickup."
        )
        if not Notification.objects.filter(
            user=request.user,
            title=collection_notification_title,
            message=collection_notification_message,
        ).exists():
            Notification.objects.create(
                user=request.user,
                title=collection_notification_title,
                message=collection_notification_message,
                notification_type="ORDER",
                related_url=f"{reverse('rider_dashboard')}?view=active",
            )
    if public_rider_collection_window_closed and selected_view == "available":
        selected_view = "active"
        available_view = False
        active_view = True
    active_loads = [
        _shop_order_estimated_load(row)
        for row in active_load_orders
    ]
    rider_load_weight = sum(
        (load["weight_kg"] for load in active_loads),
        Decimal("0.00"),
    )
    rider_load_volume = sum(
        (load["volume_cubic_ft"] for load in active_loads),
        Decimal("0.00"),
    )
    group_shop_ids = (
        set()
        if public_rider
        else set(_shared_rider_group_shop_ids(staff))
    )
    shared_rider_radius_km = _shared_rider_assignment_radius_km()
    (
        available_order_ids,
        shared_shop_distances,
        next_shared_order_available_in,
    ) = _rider_available_order_ids(
        staff,
        public_rider,
        request.user,
        rider_load_weight,
        rider_load_volume,
        dispatch_public_offers=not public_rider_batch_window_elapsed,
    )
    if public_rider_batch_window_elapsed or (
        public_rider and active_load_orders
        and not public_rider_collection_window_open
    ) or active_delivery_in_progress or rider_offer_cooldown_seconds:
        available_order_ids = []
    available_orders = ShopOrder.objects.filter(pk__in=available_order_ids)
    show_collection_offers = bool(
        public_rider_collection_window_open
        and active_view
        and available_order_ids
    )
    if performance_view or handover_view or earnings_view:
        orders = ShopOrder.objects.none()
    elif completed_view:
        orders = today_completed_orders_queryset
    elif all_completed_view:
        orders = completed_orders_queryset
    elif verified_view:
        orders = (
            assigned_orders
            .annotate(
                pickup_verified_at=Max(
                    "status_history__created_at",
                    filter=Q(
                        status_history__status="PICKED_UP",
                        status_history__remark__icontains="verified rider",
                    ),
                ),
            )
            .filter(pickup_verified_at__isnull=False)
        )
    elif active_view:
        orders = (
            assigned_orders
            .filter(status__in=[
                "READY",
                "RIDER_ASSIGNED",
                "PICKED_UP",
                "OUT_FOR_DELIVERY",
            ])
            .filter(delivery_completed_at__isnull=True)
            .exclude(status__in=["DELIVERED", "COMPLETED"])
        )
        if show_collection_offers:
            orders = orders | ShopOrder.objects.filter(
                pk__in=available_order_ids,
            )
    else:
        orders = available_orders

    orders = (
        orders
        .select_related(
            "order",
            "shop",
            "assigned_rider",
            "assigned_rider__user",
        )
        .prefetch_related(
            Prefetch(
                "status_history",
                queryset=OrderStatusHistory.objects.order_by(
                    "-created_at",
                    "-pk",
                ),
                to_attr="ordered_status_history",
            ),
            Prefetch(
                "order_items",
                queryset=OrderItem.objects.select_related("product"),
            ),
            Prefetch(
                "cash_ledger_entries",
                queryset=RiderCashLedgerEntry.objects.filter(
                    entry_type="COLLECTION",
                ),
                to_attr="rider_cod_collections",
            ),
        )
        .order_by(
            *(
                ("-pickup_verified_at", "-pk")
                if verified_view
                else ("-created_at",)
            )
        )
    )
    if verified_view:
        orders = orders[:50]
    rider_instruction_by_order_id = {
        instruction.shop_order_id: instruction
        for instruction in OrderVoiceInstruction.objects.filter(
            shop_order_id__in=orders.values("pk"),
            recipient=OrderVoiceInstruction.RECIPIENT_RIDER,
        )
    }
    pending_return_items = (
        OrderItem.objects
        .filter(
            returned_quantity__gt=F("returned_received_quantity"),
        )
        .filter(
            Q(shop_order__assigned_rider__user=request.user)
            if public_rider
            else Q(shop_order__assigned_rider=staff)
        )
        .select_related("shop_order", "shop_order__order", "product_shop")
        .order_by("-shop_order__delivery_completed_at", "id")
    )
    rider_cash_entries = RiderCashLedgerEntry.objects.filter(
        rider__user=request.user,
    ) if public_rider else RiderCashLedgerEntry.objects.filter(rider=staff)
    cash_30_day_start = timezone.now() - timedelta(days=30)
    rider_cash_totals = rider_cash_entries.aggregate(
        collected=Sum(
            "amount",
            filter=Q(entry_type="COLLECTION", payment_method="CASH"),
        ),
        personal_upi_collected=Sum(
            "amount",
            filter=Q(entry_type="COLLECTION", payment_method="STAFF_UPI"),
        ),
        handed_over=Sum(
            "amount",
            filter=Q(entry_type="HANDOVER", payment_method="CASH"),
        ),
        personal_upi_handed_over=Sum(
            "amount",
            filter=Q(entry_type="HANDOVER", payment_method="STAFF_UPI"),
        ),
        collected_30_days=Sum(
            "amount",
            filter=Q(
                entry_type="COLLECTION",
                payment_method="CASH",
                created_at__gte=cash_30_day_start,
            ),
        ),
        personal_upi_collected_30_days=Sum(
            "amount",
            filter=Q(
                entry_type="COLLECTION",
                payment_method="STAFF_UPI",
                created_at__gte=cash_30_day_start,
            ),
        ),
        handed_over_30_days=Sum(
            "amount",
            filter=Q(
                entry_type="HANDOVER",
                payment_method="CASH",
                created_at__gte=cash_30_day_start,
            ),
        ),
        personal_upi_handed_over_30_days=Sum(
            "amount",
            filter=Q(
                entry_type="HANDOVER",
                payment_method="STAFF_UPI",
                created_at__gte=cash_30_day_start,
            ),
        ),
    )
    cash_in_hand = (
        rider_cash_totals["collected"] or Decimal("0.00")
    ) - (
        rider_cash_totals["handed_over"] or Decimal("0.00")
    )
    handed_over_by_order = dict(
        rider_cash_entries
        .filter(entry_type="HANDOVER")
        .values("shop_order_id")
        .annotate(total=Sum("amount"))
        .values_list("shop_order_id", "total")
    )
    pending_cash_collections = list(
        rider_cash_entries
        .filter(entry_type="COLLECTION")
        .select_related("shop_order", "shop_order__order", "shop")
        .order_by("-created_at")
    )
    outstanding_cash_collections = []
    for collection in pending_cash_collections:
        collection.handed_over_amount = handed_over_by_order.get(
            collection.shop_order_id,
            Decimal("0.00"),
        )
        collection.remaining_amount = max(
            Decimal("0.00"),
            collection.amount - collection.handed_over_amount,
        )
        collection.edit_cod_amount_available = bool(
            collection.shop_order.status == "DELIVERED"
            and collection.shop_order.order.payment_type.upper() == "COD"
            and collection.shop_order.order.coupon_cashback_amount <= 0
            and collection.shop_order.order.finally_received_amount
            == min(
                collection.amount,
                max(
                    Decimal("0.00"),
                    collection.shop_order.order.final_billing_amount
                    - collection.shop_order.order.wallet_amount_used,
                ),
            )
            and not RiderCashLedgerEntry.objects.filter(
                shop_order__order_id=collection.shop_order.order_id,
                entry_type="COLLECTION",
            ).exclude(pk=collection.pk).exists()
            and not CODBalancePayment.objects.filter(
                shop_order__order_id=collection.shop_order.order_id,
            ).exists()
        )
        collection.edit_cod_amount_max = Decimal("999999999.99")
        if collection.remaining_amount > 0:
            outstanding_cash_collections.append(collection)
    pending_cash_collections = outstanding_cash_collections

    # Get OTP requirement safely
    verify_need = staff.shop.require_customer_otp_verification

    cod_approval_by_order_id = {}
    for payment_request in (
        CODPaymentApprovalRequest.objects.filter(
            shop_order_id__in=orders.values("pk"),
        )
        .select_related("rider__user", "reviewed_by")
        .order_by("-created_at", "-pk")
    ):
        cod_approval_by_order_id.setdefault(
            payment_request.shop_order_id,
            payment_request,
        )

    public_rider_pay_settings = (
        _delivery_sharing_settings() if public_rider else None
    )
    for order_obj in orders:
        order_obj.is_collection_offer = bool(
            show_collection_offers
            and order_obj.pk in available_order_ids
        )
        current_rider_membership = (
            order_obj.assigned_rider
            if (
                public_rider
                and order_obj.assigned_rider_id
                and order_obj.assigned_rider.user_id == request.user.id
            )
            else staff
        )
        order_obj.is_assigned_to_current_rider = bool(
            (
                order_obj.assigned_rider_id == staff.pk
                if not public_rider
                else (
                    order_obj.assigned_rider_id
                    and order_obj.assigned_rider.user_id == request.user.id
                )
            )
            or (
                order_obj.rider_accepted_at
                and order_obj.order.assigned_rider == request.user.username
            )
        )
        order_obj.public_rider_route_display = None
        if public_rider:
            saved_route_estimate = order_obj.public_rider_route_estimate
            if (
                order_obj.is_assigned_to_current_rider
                and isinstance(saved_route_estimate, dict)
                and saved_route_estimate
            ):
                order_obj.public_rider_route_display = {
                    **saved_route_estimate,
                    "pickup_distance_included": saved_route_estimate.get(
                        "pickup_distance_included",
                        True,
                    ),
                    "pickup_distance_pay_percentage": (
                        saved_route_estimate.get(
                            "pickup_distance_pay_percentage",
                            "100.00",
                        )
                    ),
                }
            elif available_view or order_obj.is_collection_offer:
                order_obj.public_rider_route_display = (
                    _public_rider_route_estimate(
                        order_obj,
                        staff.last_location,
                        public_rider_pay_settings,
                    )
                )
        order_obj.cod_payment_approval = cod_approval_by_order_id.get(
            order_obj.pk,
        )
        if (
            order_obj.cod_payment_approval
            and order_obj.cod_payment_approval.status == "PENDING"
        ):
            last_reminded_at = (
                order_obj.cod_payment_approval.last_reminded_at
            )
            order_obj.payment_reminder_wait_seconds = (
                max(
                    0,
                    math.ceil(
                        30
                        - (
                            timezone.now() - last_reminded_at
                        ).total_seconds()
                    ),
                )
                if last_reminded_at
                else 0
            )
        order_obj.rider_voice_instruction = rider_instruction_by_order_id.get(
            order_obj.pk,
        )
        order_obj.rider_voice_instruction_audio_available = (
            _instruction_audio_is_available(
                order_obj.rider_voice_instruction,
                order_obj,
            )
        )
        order_obj.load_estimate = _shop_order_estimated_load(order_obj)
        latest_status_history = getattr(
            order_obj,
            "ordered_status_history",
            [],
        )
        order_obj.last_status_changed_at = (
            latest_status_history[0].created_at
            if latest_status_history
            else order_obj.updated_at
        )
        order_obj.rider_cancel_available = bool(
            order_obj.is_assigned_to_current_rider
            and order_obj.status in {"READY", "RIDER_ASSIGNED"}
        )
        for item in order_obj.order_items.all():
            unit = item.unit or (item.product.unit if item.product_id else "")
            item.quantity_unit_calculation = _quantity_unit_calculation(
                item.quantity,
                unit,
            )
            if item.is_approximate_weight:
                item.weight_display_unit = _weight_display_unit(unit)
                item.estimated_weight_calculation = item.quantity_unit_calculation
                item.rider_weight_value = (
                    item.final_weight
                    if item.final_weight is not None
                    else item.estimated_weight
                )
                item.rider_weight_is_final = item.final_weight is not None

        customer_profile = (
            ClientProfile.objects
            .filter(
                user__username=order_obj.order.user
            )
            .first()
            if order_obj.order.user
            else None
        )

        order_obj.customer_otp = (
            customer_profile.verification_otp
            if customer_profile
            else "----"
        )
        order_obj.customer_cash_collected = sum(
            (
                entry.amount
                for entry in getattr(order_obj, "rider_cod_collections", [])
                if entry.payment_method == "CASH"
            ),
            Decimal("0.00"),
        )
        order_obj.customer_personal_upi_collected = sum(
            (
                entry.amount
                for entry in getattr(order_obj, "rider_cod_collections", [])
                if entry.payment_method == "STAFF_UPI"
            ),
            Decimal("0.00"),
        )
        payment_request = order_obj.cod_payment_approval
        order_obj.customer_shop_upi_collected = (
            payment_request.qr_amount
            if payment_request and payment_request.status == "APPROVED"
            else Decimal("0.00")
        )
        order_obj.customer_cod_collected = (
            order_obj.customer_cash_collected
            + order_obj.customer_personal_upi_collected
            + order_obj.customer_shop_upi_collected
        )
        order_obj.cod_due_amount = max(
            Decimal("0.00"),
            order_obj.order.payment_amount,
        )
        if (
            order_obj.is_assigned_to_current_rider
            and order_obj.status == "OUT_FOR_DELIVERY"
            and order_obj.order.payment_type.upper() == "COD"
        ):
            order_reference = (
                order_obj.shop_order_number or order_obj.order.order_id
            )
            order_obj.staff_cod_upi_qr = _upi_payment_qr_data_uri(
                current_rider_membership.upi_id,
                current_rider_membership.user.get_full_name()
                or current_rider_membership.user.username,
                order_obj.cod_due_amount,
                order_reference,
            )
        else:
            order_obj.staff_cod_upi_qr = ""
        order_obj.shop_cod_upi_qr = (
            _upi_payment_qr_data_uri(
                payment_request.upi_id,
                order_obj.shop.name,
                payment_request.qr_amount,
                order_obj.shop_order_number or order_obj.order.order_id,
            )
            if payment_request and payment_request.status == "QR_READY"
            else ""
        )
        order_obj.customer_wallet_transactions = []
        order_obj.customer_wallet_credits = Decimal("0.00")
        order_obj.customer_wallet_debits = Decimal("0.00")
        if (completed_view or all_completed_view) and customer_profile:
            order_reference = order_obj.order.order_id
            order_obj.customer_wallet_transactions = list(
                customer_profile.wallet_transactions.filter(
                    Q(reference=order_reference)
                    | Q(reference__startswith=f"{order_reference}:")
                ).order_by("created_at", "pk")
            )
            order_obj.customer_wallet_credits = sum(
                (
                    entry.amount
                    for entry in order_obj.customer_wallet_transactions
                    if entry.transaction_type == "CREDIT"
                ),
                Decimal("0.00"),
            )
            order_obj.customer_wallet_debits = sum(
                (
                    entry.amount
                    for entry in order_obj.customer_wallet_transactions
                    if entry.transaction_type == "DEBIT"
                ),
                Decimal("0.00"),
            )
            if (
                order_obj.customer_wallet_debits == Decimal("0.00")
                and order_obj.order.wallet_amount_used > 0
            ):
                order_obj.customer_wallet_debits = (
                    order_obj.order.wallet_amount_used
                )
        order_obj.customer_display_name = (
            order_obj.order.customer_name
            or (
                customer_profile.name.strip()
                if customer_profile and customer_profile.name.strip()
                else order_obj.order.user
            )
        )

        order_obj.rider_otp = (
            current_rider_membership.verification_otp
            if current_rider_membership
            else "----"
        )
        order_obj.verify_need = order_obj.shop.require_customer_otp_verification

        order_obj.payment_status = (
            order_obj.order.payment_status
            if hasattr(order_obj.order, "payment_status")
            else "PENDING"
        )
        order_obj.delivery_coordinates = _get_order_delivery_coordinates(
            order_obj.order
        )
        order_obj.pickup_location_data = _get_shop_order_pickup_location(order_obj)
        order_obj.pickup_coordinates = (
            {
                "lat": order_obj.pickup_location_data["lat"],
                "lng": order_obj.pickup_location_data["lng"],
            }
            if order_obj.pickup_location_data
            else None
        )
        order_obj.nearest_order_coordinates = (
            order_obj.delivery_coordinates
            if order_obj.status == "OUT_FOR_DELIVERY" and order_obj.delivery_coordinates
            else order_obj.pickup_coordinates
        )
        order_obj.is_shared_shop_delivery = (
            not public_rider and order_obj.shop_id != staff.shop_id
        )
        order_obj.is_group_shared_delivery = order_obj.shop_id in group_shop_ids
        order_obj.shared_shop_distance_km = shared_shop_distances.get(
            order_obj.shop_id,
        )
        order_obj.can_share_live_location = (
            order_obj.is_assigned_to_current_rider
            and order_obj.status == "OUT_FOR_DELIVERY"
        )
        order_obj.pickup_otp = (
            _ensure_rider_pickup_otp(order_obj)
            if (
                order_obj.is_assigned_to_current_rider
                and order_obj.status == "RIDER_ASSIGNED"
            )
            else ""
        )
        order_obj.pickup_qr_data_uri = (
            _qr_code_data_uri(
                f"{RIDER_PICKUP_QR_PREFIX}{order_obj.pickup_otp}"
            )
            if order_obj.pickup_otp
            else ""
        )
    order_ids = {order_obj.order_id for order_obj in orders}
    billing_orders = {}
    if order_ids:
        billing_children = list(
            ShopOrder.objects.filter(order_id__in=order_ids)
            .exclude(status__iexact="CANCELLED")
            .select_related("order")
            .order_by("id")
        )
        for child in billing_children:
            billing_orders.setdefault(child.order_id, []).append(child)

    for order_obj in orders:
        children = billing_orders.get(order_obj.order_id, [])
        total_value = sum(
            (child.total_amount for child in children),
            Decimal("0.00"),
        )
        remaining_payment = order_obj.order.payment_amount
        remaining_platform_charge = order_obj.order.platform_charge_amount
        for child in children:
            if total_value > 0:
                share = child.total_amount / total_value
            else:
                share = Decimal("1") / max(len(children), 1)
            if child.id == children[-1].id:
                payment_share = remaining_payment
                platform_share = remaining_platform_charge
            else:
                payment_share = (
                    order_obj.order.payment_amount * share
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                platform_share = (
                    order_obj.order.platform_charge_amount * share
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                remaining_payment -= payment_share
                remaining_platform_charge -= platform_share
            if child.id == order_obj.id:
                order_obj.customer_payment_share = payment_share
                order_obj.platform_charge_share = platform_share

    route_targets = (
        {}
        if public_rider
        else _get_rider_route_delivery_targets(staff, staff.shop)
    )
    for order_obj in orders:
        order_obj.delivery_estimated_arrival = route_targets.get(order_obj.id)

    navigation_stops = []
    assigned_orders = (
        assigned_orders
        .filter(
            status="OUT_FOR_DELIVERY",
            delivery_completed_at__isnull=True,
        )
        .select_related("order", "shop")
        .order_by("created_at")
    )
    if active_view:
        assigned_orders = list(assigned_orders)
        customer_names = {
            profile.user.username: profile.name.strip()
            for profile in ClientProfile.objects.filter(
                user__username__in={
                    assigned_order.order.user
                    for assigned_order in assigned_orders
                    if assigned_order.order.user
                },
            ).select_related("user")
            if profile.name and profile.name.strip()
        }
        for assigned_order in assigned_orders:
            coordinates = _get_order_delivery_coordinates(assigned_order.order)
            if coordinates:
                navigation_stops.append({
                    "order_id": assigned_order.order.order_id,
                    "customer_name": (
                        assigned_order.order.customer_name
                        or customer_names.get(assigned_order.order.user)
                        or assigned_order.order.user
                        or "Customer"
                    ),
                    "address": assigned_order.order.delivery_address,
                    **coordinates,
                })

    pickup_navigation_stops = _get_rider_pickup_navigation_stops(
        staff,
        rider_user=request.user if public_rider else None,
    )
    pickup_pending_count = rider_orders.filter(
        status__in=["READY", "RIDER_ASSIGNED"],
        delivery_completed_at__isnull=True,
    ).count()
    pickup_ready_count = rider_orders.filter(
        status="PICKED_UP",
        delivery_completed_at__isnull=True,
    ).count()
    pickup_start_orders = list(
        rider_orders.filter(
            status="PICKED_UP",
            delivery_completed_at__isnull=True,
            **({"rider_order_acknowledged": True} if public_rider else {}),
        ).only("pk", "order_id")
    )
    pickup_start_count = len(pickup_start_orders)
    public_rider_batch_expires_at = None
    public_rider_base_fare = None
    public_rider_rate_per_km = None
    if public_rider:
        public_rider_base_fare = public_rider_pay_settings.shared_rider_base_pay
        public_rider_rate_per_km = (
            public_rider_pay_settings.shared_rider_rate_per_km
        )
    if (
        public_rider
        and public_rider.public_rider_batch_accepting
        and active_load_orders
        and all(
            active.status in {"READY", "RIDER_ASSIGNED"}
            for active in active_load_orders
        )
    ):
        if public_rider.public_rider_batch_started_at:
            batch_expires_at = (
                public_rider.public_rider_batch_started_at
                + timedelta(minutes=5)
            )
            if batch_expires_at > timezone.now():
                public_rider_batch_expires_at = batch_expires_at

    return render(
        request,
        "seller/rider_dashboard.html",
        {
            "staff": staff,
            "public_rider": public_rider,
            "public_rider_offer_cooldown_until": (
                public_rider.public_rider_offer_cooldown_until
                if public_rider
                else None
            ),
            "public_rider_wallet": public_rider_wallet,
            "public_rider_wallet_transactions": public_rider_wallet_transactions,
            "public_rider_last_location": public_rider_last_location,
            "is_public_rider": bool(public_rider),
            "selected_view": selected_view,
            "active_delivery_in_progress": active_delivery_in_progress,
            "rider_offer_cooldown_seconds": rider_offer_cooldown_seconds,
            "hide_available_tab": bool(
                active_delivery_in_progress
                or rider_offer_cooldown_seconds
                or public_rider_collection_window_closed
            ),
            "earnings_view": earnings_view,
            "earnings_period": earnings_period,
            "public_rider_earnings_summary": public_rider_earnings_summary,
            "public_rider_batch_expires_at": public_rider_batch_expires_at,
            "public_rider_collection_window_expired": (
                public_rider_collection_window_expired
            ),
            "public_rider_collection_window_closed": (
                public_rider_collection_window_closed
            ),
            "public_rider_base_fare": public_rider_base_fare,
            "public_rider_rate_per_km": public_rider_rate_per_km,
            "orders": orders,
            "pending_return_items": pending_return_items,
            "cash_in_hand": cash_in_hand,
            "rider_load_weight_kg": rider_load_weight,
            "rider_load_volume_cubic_ft": rider_load_volume,
            "rider_capacity_configured": (
                staff.max_load_weight_kg is not None
                and staff.max_load_volume_cubic_ft is not None
            ),
            "rider_load_over_capacity": (
                staff.max_load_weight_kg is not None
                and staff.max_load_volume_cubic_ft is not None
                and (
                    rider_load_weight > staff.max_load_weight_kg
                    or rider_load_volume > staff.max_load_volume_cubic_ft
                )
            ),
            "rider_capacity_weight_kg": staff.max_load_weight_kg,
            "rider_capacity_volume_cubic_ft": staff.max_load_volume_cubic_ft,
            "staff_upi_cod_balance": (
                (
                    ShopStaffWallet.objects.filter(
                        staff__user=request.user,
                    ).aggregate(total=Sum("balance"))["total"]
                    if public_rider
                    else ShopStaffWallet.objects.filter(staff=staff)
                    .values_list("balance", flat=True)
                    .first()
                )
                or Decimal("0.00")
            ),
            "cod_collected_30_days": (
                (rider_cash_totals["collected_30_days"] or Decimal("0.00"))
                + (
                    rider_cash_totals["personal_upi_collected_30_days"]
                    or Decimal("0.00")
                )
            ),
            "cod_handed_over_30_days": (
                (rider_cash_totals["handed_over_30_days"] or Decimal("0.00"))
                + (
                    rider_cash_totals["personal_upi_handed_over_30_days"]
                    or Decimal("0.00")
                )
            ),
            "personal_upi_collected_30_days": (
                rider_cash_totals["personal_upi_collected_30_days"]
                or Decimal("0.00")
            ),
            "personal_upi_handed_over_30_days": (
                rider_cash_totals["personal_upi_handed_over_30_days"]
                or Decimal("0.00")
            ),
            "cod_collected_total": (
                (rider_cash_totals["collected"] or Decimal("0.00"))
                + (
                    rider_cash_totals["personal_upi_collected"]
                    or Decimal("0.00")
                )
            ),
            "cod_handed_over_total": (
                (rider_cash_totals["handed_over"] or Decimal("0.00"))
                + (
                    rider_cash_totals["personal_upi_handed_over"]
                    or Decimal("0.00")
                )
            ),
            "efficiency": (
                _staff_efficiency_card(staff.shop, "RIDER", staff)
                if performance_view and not public_rider
                else None
            ),
            "pending_cash_collections": pending_cash_collections,
            "navigation_stops": navigation_stops,
            "combined_route_groups": [
                pickup_navigation_stops,
                navigation_stops,
            ],
            "shared_rider_radius_km": shared_rider_radius_km,
            "next_shared_order_available_in": next_shared_order_available_in,
            "pickup_navigation_stops": pickup_navigation_stops,
            "pickup_pending_count": pickup_pending_count,
            "pickup_ready_count": pickup_ready_count,
            "show_start_active_deliveries": pickup_start_count > 0,
            "can_manage_shared_rider_groups": (
                not public_rider and (Shop.objects.filter(
                    owner=request.user,
                    allow_owner_create_shared_rider_groups=True,
                ).exists()
                or SharedRiderGroup.objects.filter(owner=request.user).exists()
                or SharedRiderGroupShop.objects.filter(
                    shop__owner=request.user,
                ).exists()
            )),
            "shared_group_count": SharedRiderGroupShop.objects.filter(
                shop_id=staff.shop_id,
                status=SharedRiderGroupShop.STATUS_APPROVED,
            ).values("group_id").distinct().count(),
            "verify_need": verify_need,
            "completed_view": completed_view,
            "all_completed_view": all_completed_view,
            "verified_view": verified_view,
            "active_view": active_view,
            "available_view": available_view,
            "handover_view": handover_view,
            "performance_view": performance_view,
            "available_order_count": available_orders.count(),
            "verified_order_count": min(
                orders.count(),
                50,
            ) if verified_view else 0,
            "active_order_count": rider_orders.filter(
                status__in=[
                    "READY",
                    "RIDER_ASSIGNED",
                    "PICKED_UP",
                    "OUT_FOR_DELIVERY",
                ],
                delivery_completed_at__isnull=True,
            ).exclude(status__in=["DELIVERED", "COMPLETED"]).count(),
            "completed_order_count": today_completed_orders_queryset.count(),
            "all_completed_order_count": completed_orders_queryset.count(),
            "handover_count": len(pending_cash_collections),
        }
    )

@login_required
def shop_order_list(request):
    if request.session.get("active_portal") == "B2B_CUSTOMER":
        return redirect("marketplace")

    user = request.user

    owner_shops = Shop.objects.filter(
        owner=user
    )

    staff_shops = Shop.objects.filter(
        staff_members__user=user,
        staff_members__is_active=True,
        staff_members__is_public_rider=False,
    )

    shops = (
        owner_shops
        | staff_shops
    ).distinct()
    point_of_sale_shops = Shop.objects.filter(
        Q(owner=user)
        | Q(
            staff_members__user=user,
            staff_members__is_active=True,
            staff_members__is_public_rider=False,
            staff_members__can_view_orders=True,
        )
    ).distinct()
    pickup_verification_shops = Shop.objects.filter(
        Q(owner=user)
        | (
            Q(
                staff_members__user=user,
                staff_members__is_active=True,
            )
            & (
                Q(staff_members__role__in=["MANAGER", "PACKER"])
                | Q(staff_members__can_change_order_status=True)
            )
        )
    ).distinct()
    can_verify_rider_pickup_otp = (
        user.is_superuser or pickup_verification_shops.exists()
    )

    shop_orders = (
        ShopOrder.objects
        .filter(shop__in=shops)
        .select_related(
            "order",
            "shop",
            "assigned_rider__user__client_profile",
            "assigned_packer__user",
        )
        .prefetch_related(
            "order_items__product_shop",
            "status_history",
            "cash_ledger_entries",
        )
        .order_by("-created_at")
    )

    selected_view = request.GET.get("view")
    verified_view = selected_view == "verified"
    handover_view = selected_view == "handover"
    abandoned_view = selected_view == "abandoned"
    shop_id = request.GET.get("shop")

    if shop_id:
        shop_orders = shop_orders.filter(
            shop_id=shop_id
        )

    delivery_settings = DeliverySharingSettings.objects.first()
    abandoned_checkout_timeout_minutes = (
        delivery_settings.abandoned_checkout_timeout_minutes
        if delivery_settings
        else 3
    )
    abandoned_attempts = CheckoutAttempt.objects.filter(
        shop__in=shops,
        order__isnull=True,
        last_activity_at__lte=timezone.now() - timedelta(
            minutes=abandoned_checkout_timeout_minutes,
        ),
    ).select_related(
        "customer",
        "customer__client_profile",
        "shop",
    ).order_by("-last_activity_at")
    if shop_id:
        abandoned_attempts = abandoned_attempts.filter(shop_id=shop_id)
    abandoned_checkout_count = abandoned_attempts.count()
    if abandoned_view:
        abandoned_attempts = list(abandoned_attempts[:200])
        assignments_by_id = {}
        assignments_by_shop_and_name = {}
        for assignment in ProductShop.objects.filter(
            shop__in=shops,
            is_active=True,
            product__is_active=True,
        ).select_related("product", "shop"):
            assignments_by_id[assignment.pk] = assignment
            assignments_by_shop_and_name[
                (assignment.shop_id, assignment.product.product_name)
            ] = assignment

        for attempt in abandoned_attempts:
            for item in attempt.cart_items:
                product_shop_id = item.get("product_shop_id")
                if isinstance(product_shop_id, str) and product_shop_id.isdigit():
                    product_shop_id = int(product_shop_id)
                assignment = assignments_by_id.get(product_shop_id)
                if assignment is None:
                    assignment = assignments_by_shop_and_name.get(
                        (attempt.shop_id, item.get("product_name")),
                    )
                if assignment is None:
                    item.setdefault(
                        "image_urls",
                        [item["image_url"]] if item.get("image_url") else [],
                    )
                    continue
                item["image_urls"] = list(dict.fromkeys([
                    *([item["image_url"]] if item.get("image_url") else []),
                    *assignment.display_image_urls,
                ]))
                item["image_url"] = (
                    item["image_urls"][0] if item["image_urls"] else ""
                )

    status = request.GET.get("status")

    order_date_value = request.GET.get("date", "").strip()
    if order_date_value:
        try:
            order_date = date.fromisoformat(order_date_value)
        except ValueError:
            try:
                order_date = datetime.strptime(
                    order_date_value,
                    "%d-%m-%Y",
                ).date()
            except ValueError:
                messages.error(request, "Enter a valid order date.")
            else:
                shop_orders = shop_orders.filter(created_at__date=order_date)
        else:
            shop_orders = shop_orders.filter(created_at__date=order_date)

    order_number = request.GET.get("order_number", "").strip()[:120]
    if order_number:
        shop_orders = shop_orders.filter(
            Q(order__order_id__icontains=order_number)
            | Q(shop_order_number__icontains=order_number)
            | Q(order__customer_name__icontains=order_number)
            | Q(order__delivery_contact_no__icontains=order_number),
        )

    verified_orders = (
        shop_orders
        .annotate(
            pickup_verified_at=Max(
                "status_history__created_at",
                filter=Q(
                    status_history__status="PICKED_UP",
                    status_history__remark__icontains="verified rider",
                ),
            ),
        )
        .filter(pickup_verified_at__isnull=False)
        .order_by("-pickup_verified_at", "-pk")
    )
    order_status_tabs = []
    if not verified_view and not handover_view and not abandoned_view:
        status_tab_options = [
            ("", "All"),
            ("pending", "Pending"),
            ("PLACED", "Placed"),
            ("CONFIRMED", "Confirmed"),
            ("PACKING", "Packing"),
            ("READY", "Ready for Pickup"),
            ("RIDER_ASSIGNED", "Accepted by Rider"),
            ("PICKED_UP", "Picked Up"),
            ("OUT_FOR_DELIVERY", "Out for Delivery"),
            ("DELIVERED", "Delivered"),
            ("RETURNED", "Returned"),
            ("CANCELLED", "Cancelled"),
        ]
        status_counts = {
            row["status"]: row["count"]
            for row in shop_orders.order_by().values("status").annotate(
                count=Count("pk", distinct=True),
            )
        }
        for status_value, label in status_tab_options:
            query_params = request.GET.copy()
            if status_value:
                query_params["status"] = status_value
            else:
                query_params.pop("status", None)
            order_status_tabs.append({
                "value": status_value,
                "label": label,
                "count": (
                    sum(status_counts.values())
                    if not status_value
                    else status_counts.get(status_value, 0)
                ),
                "url": (
                    f"{reverse('shop_order_list')}?{query_params.urlencode()}"
                    if query_params
                    else reverse("shop_order_list")
                ),
            })
    verified_order_count = min(verified_orders.count(), 50)
    if verified_view:
        shop_orders = verified_orders[:50]
    elif status:
        shop_orders = shop_orders.filter(status=status)

    legacy_rider_source = verified_orders if verified_view else shop_orders
    legacy_rider_usernames = {
        username
        for username in legacy_rider_source.values_list(
            "order__assigned_rider",
            flat=True,
        ).distinct()
        if username
    }
    legacy_riders_by_username = {
        rider.user.username: rider
        for rider in ShopStaff.objects.filter(
            role="RIDER",
            user__username__in=legacy_rider_usernames,
        ).select_related("user__client_profile")
    }
    legacy_rider_profiles_by_username = {
        profile.user.username: profile
        for profile in ClientProfile.objects.filter(
            user__username__in=legacy_rider_usernames,
        ).select_related("user")
    }

    cod_approval_by_shop_order_id = {}
    for payment_request in (
        CODPaymentApprovalRequest.objects.filter(
            shop_order_id__in=shop_orders.values("pk"),
            status="PENDING",
        )
        .select_related("rider__user")
        .order_by("-created_at", "-pk")
    ):
        cod_approval_by_shop_order_id.setdefault(
            payment_request.shop_order_id,
            payment_request,
        )

    assignable_shop_ids = set(
        Shop.objects.filter(owner=user).values_list("pk", flat=True)
    )
    assignable_shop_ids.update(
        ShopStaff.objects.filter(
            shop__in=shops,
            user=user,
            is_active=True,
            can_assign_staff=True,
        ).values_list("shop_id", flat=True)
    )
    assignment_staff_by_shop = defaultdict(lambda: {"RIDER": [], "PACKER": []})
    for staff_member in (
        ShopStaff.objects.filter(
            shop__in=shops,
            role__in=["RIDER", "PACKER"],
            is_active=True,
            is_public_rider=False,
        )
        .select_related("user")
        .order_by("user__username")
    ):
        assignment_staff_by_shop[staff_member.shop_id][
            staff_member.role
        ].append(staff_member)

    public_rider_enabled = public_rider_service_enabled()
    for shop_order in shop_orders:
        shop_order.can_manage_assignments = (
            shop_order.shop_id in assignable_shop_ids
        )
        shop_order.available_riders = assignment_staff_by_shop[
            shop_order.shop_id
        ]["RIDER"]
        shop_order.available_packers = assignment_staff_by_shop[
            shop_order.shop_id
        ]["PACKER"]
        shop_order.has_rider_assignment_options = bool(
            shop_order.available_riders
            or (
                shop_order.status == "READY"
                and public_rider_enabled
                and shop_order.shop.public_rider_network_enabled
            )
            or (
                public_rider_enabled
                and shop_order.public_rider_network_requested
            )
        )
        product_keys = set()
        for item in shop_order.order_items.all():
            if item.product_id:
                product_keys.add(("product", item.product_id))
            elif item.product_shop_id:
                product_id = item.product_shop.product_id
                product_keys.add(
                    ("product", product_id)
                    if product_id
                    else ("product-shop", item.product_shop_id)
                )
            elif item.sku:
                product_keys.add(("sku", item.sku.strip().casefold()))
            else:
                product_keys.add(("name", item.product_name.strip().casefold()))
        shop_order.distinct_product_count = len(product_keys)
        assigned_rider = (
            shop_order.assigned_rider
            or legacy_riders_by_username.get(shop_order.order.assigned_rider)
        )
        rider_profile = (
            getattr(assigned_rider.user, "client_profile", None)
            if assigned_rider
            else legacy_rider_profiles_by_username.get(
                shop_order.order.assigned_rider,
            )
        )
        shop_order.rider_display_name = (
            assigned_rider.user.get_full_name() or assigned_rider.user.username
            if assigned_rider
            else shop_order.order.assigned_rider
        )
        shop_order.rider_contact_display = (
            (
                assigned_rider.mobile_no
                if assigned_rider and assigned_rider.mobile_no
                else rider_profile.contact_no
                if rider_profile and rider_profile.contact_no
                else shop_order.order.rider_contact_no
            )
        )
        shop_order.cod_payment_approval = cod_approval_by_shop_order_id.get(
            shop_order.pk,
        )
        cash_entries = list(shop_order.cash_ledger_entries.all())
        collection = next(
            (
                entry
                for entry in cash_entries
                if entry.entry_type == "COLLECTION"
            ),
            None,
        )
        handed_over_amount = sum(
            (
                entry.amount
                for entry in cash_entries
                if entry.entry_type == "HANDOVER"
            ),
            Decimal("0.00"),
        )
        shop_order.cash_handover_pending = bool(
            collection and handed_over_amount < collection.amount
        )
        shop_order.cod_cash_collected = collection.amount if collection else Decimal("0.00")
        shop_order.cod_cash_handover_remaining = max(
            Decimal("0.00"),
            shop_order.cod_cash_collected - handed_over_amount,
        )

    pending_collections = list(
        RiderCashLedgerEntry.objects.filter(
            shop__in=shops,
            entry_type="COLLECTION",
            shop_order__status="DELIVERED",
            shop_order__order__payment_type__iexact="COD",
        )
        .select_related("shop", "rider__user", "shop_order")
        .order_by("created_at", "id")
    )
    handover_totals = dict(
        RiderCashLedgerEntry.objects.filter(
            entry_type="HANDOVER",
            shop_order_id__in=[entry.shop_order_id for entry in pending_collections],
        )
        .values("shop_order_id")
        .annotate(total=Sum("amount"))
        .values_list("shop_order_id", "total")
    )
    grouped_handover_totals = {}
    for collection in pending_collections:
        handed_over = handover_totals.get(
            collection.shop_order_id,
            Decimal("0.00"),
        )
        remaining = max(Decimal("0.00"), collection.amount - handed_over)
        if remaining <= 0:
            continue
        key = (
            collection.shop_id,
            collection.rider_id,
            collection.payment_method,
        )
        group = grouped_handover_totals.get(key)
        if group is None:
            group = {
                "shop": collection.shop,
                "rider": collection.rider,
                "payment_method": collection.payment_method,
                "remaining_amount": Decimal("0.00"),
                "order_count": 0,
                "orders": [],
                "can_confirm": (
                    collection.shop.owner_id == user.id
                    or ShopStaff.objects.filter(
                        shop=collection.shop,
                        user=user,
                        role="MANAGER",
                        is_active=True,
                    ).exists()
                ),
            }
            grouped_handover_totals[key] = group
        group["remaining_amount"] += remaining
        group["order_count"] += 1
        group["orders"].append({
            "shop_order": collection.shop_order,
            "collected_amount": collection.amount,
            "handed_over_amount": handed_over,
            "remaining_amount": remaining,
        })

    rider_cod_handover_groups = list(grouped_handover_totals.values())
    handover_action_count = sum(
        group["order_count"]
        for group in rider_cod_handover_groups
        if group["can_confirm"]
    )

    return render(
        request,
        "shop/shop_order_list.html",
        {
            "shops": shops,
            "point_of_sale_shops": point_of_sale_shops,
            "shop_orders": shop_orders,
            "rider_cod_handover_groups": rider_cod_handover_groups,
            "order_date_filter": order_date_value,
            "order_number_filter": order_number,
            "order_status_tabs": order_status_tabs,
            "abandoned_view": abandoned_view,
            "abandoned_attempts": abandoned_attempts,
            "abandoned_checkout_count": abandoned_checkout_count,
            "abandoned_checkout_timeout_minutes": abandoned_checkout_timeout_minutes,
            "public_rider_service_enabled": public_rider_enabled,
            "order_status_options": [
                ("pending", "Pending"),
                *OrderStatusHistory.STATUS_CHOICES,
            ],
            "selected_order_status": status,
            "can_verify_rider_pickup_otp": can_verify_rider_pickup_otp,
            "verified_view": verified_view,
            "verified_order_count": verified_order_count,
            "handover_view": handover_view,
            "handover_action_count": handover_action_count,
        }
    )


@login_required
def save_abandoned_checkout_remark(request, attempt_id):
    if request.method != "POST":
        return redirect("shop_order_list")
    if request.session.get("active_portal") == "B2B_CUSTOMER":
        return redirect("marketplace")

    shops = Shop.objects.filter(owner=request.user) | Shop.objects.filter(
        staff_members__user=request.user,
        staff_members__is_active=True,
        staff_members__is_public_rider=False,
    )
    attempt = get_object_or_404(
        CheckoutAttempt.objects.select_related("shop"),
        pk=attempt_id,
        shop__in=shops,
        order__isnull=True,
    )
    remark = request.POST.get("remark", "").strip()
    if not remark:
        messages.error(request, "Enter a remark before saving.")
        return redirect(f"{reverse('shop_order_list')}?view=abandoned")
    if len(remark) > 1000:
        messages.error(request, "The remark must be 1000 characters or fewer.")
        return redirect(f"{reverse('shop_order_list')}?view=abandoned")

    attempt.shop_remark = remark
    attempt.remark_updated_at = timezone.now()
    attempt.remark_updated_by = request.user
    attempt.save(update_fields=[
        "shop_remark",
        "remark_updated_at",
        "remark_updated_by",
    ])
    messages.success(request, "Remark saved. The checkout remains in abandoned data.")
    return redirect(f"{reverse('shop_order_list')}?view=abandoned")


@login_required
def shop_order_detail(request, shop_order_id):
    if request.session.get("active_portal") == "B2B_CUSTOMER":
        return redirect("marketplace")

    shop_order = get_object_or_404(
        ShopOrder.objects
        .select_related(
            "shop",
            "order",
            "assigned_rider",
            "assigned_rider__user",
        )
        .prefetch_related(
            "order_items__product",
            "status_history",
            "cash_ledger_entries__rider__user",
            "cash_ledger_entries__created_by",
            "wallet_transactions",
        ),
        id=shop_order_id,
    )

    accepted_by_user = bool(
        shop_order.rider_accepted_at
        and shop_order.order.assigned_rider == request.user.username
    )
    public_rider_approved = PublicRiderApplication.objects.filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).exists()
    active_shop_rider = ShopStaff.objects.filter(
        shop=shop_order.shop,
        user=request.user,
        role="RIDER",
        is_active=True,
    ).exists()
    is_assigned_rider = bool(
        (
            shop_order.assigned_rider_id
            and shop_order.assigned_rider.user_id == request.user.id
            and shop_order.assigned_rider.role == "RIDER"
            and shop_order.assigned_rider.is_active
            and (
                not shop_order.assigned_rider.is_public_rider
                or public_rider_approved
            )
        )
        or (
            accepted_by_user
            and (active_shop_rider or public_rider_approved)
        )
    )
    if not user_can_access_shop(request.user, shop_order.shop) and not is_assigned_rider:
        raise PermissionDenied

    available_riders = (
        ShopStaff.objects
        .filter(
            shop=shop_order.shop,
            role="RIDER",
            is_active=True,
            is_public_rider=False,
        )
        .select_related("user")
        .order_by("user__username")
    )

    can_assign_rider = (
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
            can_assign_staff=True,
        ).exists()
    )

    current_rider = ShopStaff.objects.filter(
        shop=shop_order.shop,
        user=request.user,
        is_active=True,
        role="RIDER",
    ).first()

    current_rider_can_accept = bool(
        current_rider
        and shop_order.status == "READY"
        and (
            shop_order.assigned_rider_id is None
            or shop_order.assigned_rider_id == current_rider.id
        )
        and not shop_order.public_rider_network_requested
    )

    available_packers = (
        ShopStaff.objects
        .filter(
            shop=shop_order.shop,
            role="PACKER",
            is_active=True,
        )
        .select_related("user")
        .order_by("user__username")
    )

    can_assign_packer = (
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
            can_assign_staff=True,
        ).exists()
    )

    current_packer = ShopStaff.objects.filter(
        shop=shop_order.shop,
        user=request.user,
        is_active=True,
        role="PACKER",
    ).first()

    current_packer_can_accept = bool(
        current_packer
        and shop_order.status in ["PLACED", "CONFIRMED", "PACKING"]
        and (
            shop_order.assigned_packer_id is None
            or shop_order.assigned_packer_id == current_packer.id
        )
        and not ShopOrder.objects.filter(
            shop=shop_order.shop,
            assigned_packer=current_packer,
            status__in=["PLACED", "CONFIRMED", "PACKING", "READY"],
        ).exclude(id=shop_order.id).exists()
    )
    current_packer_can_self_accept = bool(
        current_packer and shop_order.assigned_packer_id is None
    )
    can_finalize_weights = bool(
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
        ).filter(
            Q(can_pack_orders=True) | Q(id=shop_order.assigned_packer_id)
        ).exists()
    )
    approximate_items = []
    for item in shop_order.order_items.all():
        unit = item.unit or (item.product.unit if item.product_id else "")
        item.price_unit = unit or "unit"
        item.quantity_unit = _quantity_unit_total(item.quantity, unit)
        item.quantity_unit_calculation = _quantity_unit_calculation(
            item.quantity,
            unit,
        )
        if not item.is_approximate_weight:
            continue
        item.weight_display_unit = _weight_display_unit(unit)
        item.estimated_weight_calculation = item.quantity_unit_calculation
        approximate_items.append(item)
    can_receive_returns = _can_manage_shop_stock(request, shop_order.shop)
    has_pending_returns = shop_order.order_items.filter(
        returned_quantity__gt=F("returned_received_quantity"),
    ).exists()
    cash_collection = shop_order.cash_ledger_entries.filter(
        entry_type="COLLECTION",
    ).select_related("rider__user", "created_by").first()
    cod_payment_approval = (
        shop_order.cod_payment_approval_requests
        .select_related("rider__user", "reviewed_by")
        .order_by("-created_at", "-pk")
        .first()
    )
    cash_handover = shop_order.cash_ledger_entries.filter(
        entry_type="HANDOVER",
    ).select_related("created_by").first()
    cash_handover_amount = shop_order.cash_ledger_entries.filter(
        entry_type="HANDOVER",
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cash_handover_remaining = max(
        Decimal("0.00"),
        (cash_collection.amount if cash_collection else Decimal("0.00"))
        - cash_handover_amount,
    )
    cod_overpayment_refund = shop_order.wallet_transactions.filter(
        transaction_type="DEBIT",
        description__startswith="Customer overpayment refunded",
    ).first()
    can_confirm_rider_cash = (
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="MANAGER",
            is_active=True,
        ).exists()
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="PAYMENT_MANAGER",
            can_confirm_rider_payments=True,
            is_active=True,
            is_public_rider=False,
        ).exists()
    )
    order_qr_data_uri = _qr_code_data_uri(
        request.build_absolute_uri(
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": shop_order.id},
            )
        )
    )
    rider_order_is_accepted = bool(
        shop_order.rider_accepted_at
        and shop_order.order.assigned_rider
    )
    can_collect_cod_balance = can_confirm_rider_cash
    can_confirm_rider_pickup = (
        request.user.is_superuser
        or shop_order.shop.owner_id == request.user.pk
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
        ).filter(
            Q(role__in=["MANAGER", "PACKER"]) | Q(can_change_order_status=True)
        ).exists()
    )
    show_rider_order_qr = bool(
        shop_order.status in {
            "READY",
            "RIDER_ASSIGNED",
            "PICKED_UP",
            "OUT_FOR_DELIVERY",
        }
        and (shop_order.assigned_rider_id or rider_order_is_accepted)
    )
    customer_name = (
        shop_order.order.customer_name
        or ClientProfile.objects.filter(
            user__username=shop_order.order.user,
        ).values_list("name", flat=True).first()
        or shop_order.order.user
    )
    voice_instruction = (
        OrderVoiceInstruction.objects
        .filter(
            shop_order=shop_order,
            recipient=OrderVoiceInstruction.RECIPIENT_SHOP,
        )
        .first()
    )
    voice_instruction_audio_available = _instruction_audio_is_available(
        voice_instruction,
        shop_order,
    )

    return render(
        request,
        "shop/order_detail.html",
        {
            "shop_order": shop_order,
            "voice_instruction": voice_instruction,
            "voice_instruction_audio_available": voice_instruction_audio_available,
            "customer_name": customer_name,
            "customer_coordinates": _get_order_delivery_coordinates(
                shop_order.order,
            ),
            "shop_coordinates": _get_shop_coordinates(shop_order.shop),
            "rider_coordinates": (
                shop_order.rider_last_location
                if _valid_route_coordinates(shop_order.rider_last_location)
                else None
            ),
            "available_riders": available_riders,
            "can_assign_rider": can_assign_rider,
            "current_rider_can_accept": current_rider_can_accept,
            "available_packers": available_packers,
            "can_assign_packer": can_assign_packer,
            "current_packer_can_accept": current_packer_can_accept,
            "current_packer": current_packer,
            "current_packer_can_self_accept": current_packer_can_self_accept,
            "can_finalize_weights": can_finalize_weights,
            "approximate_items": approximate_items,
            "can_receive_returns": can_receive_returns,
            "has_pending_returns": has_pending_returns,
            "cash_collection": cash_collection,
            "cod_payment_approval": cod_payment_approval,
            "cash_handover": cash_handover,
            "cash_handover_amount": cash_handover_amount,
            "cash_handover_remaining": cash_handover_remaining,
            "is_assigned_rider": is_assigned_rider,
            "cod_collection_edit_max": Decimal("999999999.99"),
            "can_edit_rider_cod_collection": bool(
                is_assigned_rider
                and shop_order.status == "DELIVERED"
                and cash_collection
                and cash_handover_remaining > 0
                and shop_order.order.payment_type.upper() == "COD"
                and shop_order.order.coupon_cashback_amount <= 0
                and shop_order.order.finally_received_amount
                == min(
                    cash_collection.amount,
                    max(
                        Decimal("0.00"),
                        shop_order.order.final_billing_amount
                        - shop_order.order.wallet_amount_used,
                    ),
                )
                and not RiderCashLedgerEntry.objects.filter(
                    shop_order__order=shop_order.order,
                    entry_type="COLLECTION",
                ).exclude(pk=cash_collection.pk).exists()
                and not CODBalancePayment.objects.filter(
                    shop_order__order=shop_order.order,
                ).exists()
            ),
            "cod_overpayment_refund": cod_overpayment_refund,
            "can_confirm_rider_cash": can_confirm_rider_cash,
            "can_review_cod_payment": _can_review_cod_payment(
                request.user,
                shop_order.shop,
            ),
            "cod_balance_payments": shop_order.cod_balance_payments.select_related(
                "received_by",
            ),
            "can_collect_cod_balance": can_collect_cod_balance,
            "can_edit_order_items": (
                shop_order.status.upper() in {"PLACED", "CONFIRMED"}
                and all(
                    row.status.upper() in {"PLACED", "CONFIRMED"}
                    for row in ShopOrder.objects.filter(order=shop_order.order)
                )
                and (
                    shop_order.shop.owner_id == request.user.id
                    or bool(
                        ShopStaff.objects.filter(
                            shop=shop_order.shop,
                            user=request.user,
                            is_active=True,
                        ).filter(
                            Q(role="MANAGER")
                            | Q(role="ORDER", can_view_orders=True)
                        ).exists()
                    )
                )
            ),
            "can_change_order_status": user_can_change_status(
                request.user,
                shop_order.shop,
            ),
            "can_confirm_rider_pickup": can_confirm_rider_pickup,
            "order_qr_data_uri": order_qr_data_uri,
            "show_rider_order_qr": show_rider_order_qr,
            "rider_order_is_accepted": rider_order_is_accepted,
        }
    )


@login_required
@transaction.atomic
def confirm_rider_cod_handover_total(request, shop_id, rider_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop = get_object_or_404(Shop, pk=shop_id)
    rider = get_object_or_404(
        ShopStaff,
        pk=rider_id,
        role="RIDER",
        is_active=True,
    )
    can_confirm = (
        shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop,
            user=request.user,
            role="MANAGER",
            is_active=True,
        ).exists()
        or ShopStaff.objects.filter(
            shop=shop,
            user=request.user,
            role="PAYMENT_MANAGER",
            can_confirm_rider_payments=True,
            is_active=True,
            is_public_rider=False,
        ).exists()
    )
    if not can_confirm:
        raise PermissionDenied

    payment_method = (request.POST.get("payment_method") or "").strip().upper()
    if payment_method not in {"CASH", "STAFF_UPI"}:
        messages.error(request, "Select a valid COD handover type.")
        return redirect(f"{reverse('shop_order_list')}?view=handover")

    eligible_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(
            shop=shop,
            assigned_rider=rider,
            status="DELIVERED",
            order__payment_type__iexact="COD",
        )
        .order_by("created_at", "pk")
    )
    collections = list(
        RiderCashLedgerEntry.objects.select_for_update()
        .filter(
            shop_order__in=eligible_orders,
            rider=rider,
            payment_method=payment_method,
            entry_type="COLLECTION",
        )
        .select_related("shop_order__order")
        .order_by("created_at", "pk")
    )
    handover_totals = dict(
        RiderCashLedgerEntry.objects.filter(
            shop_order__in=eligible_orders,
            payment_method=payment_method,
            entry_type="HANDOVER",
        )
        .values("shop_order_id")
        .annotate(total=Sum("amount"))
        .values_list("shop_order_id", "total")
    )
    remaining_by_collection = [
        (
            collection,
            max(
                Decimal("0.00"),
                collection.amount
                - handover_totals.get(collection.shop_order_id, Decimal("0.00")),
            ),
        )
        for collection in collections
    ]
    total_remaining = sum(
        (remaining for _collection, remaining in remaining_by_collection),
        Decimal("0.00"),
    )
    raw_amount = (request.POST.get("received_amount") or "").strip()
    try:
        received_amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a valid total COD amount received.")
        return redirect(f"{reverse('shop_order_list')}?view=handover")
    if (
        not received_amount.is_finite()
        or received_amount <= 0
        or received_amount > total_remaining
        or received_amount != received_amount.quantize(Decimal("0.01"))
    ):
        messages.error(
            request,
            f"Enter an amount greater than ₹0.00 and no more than ₹{total_remaining:.2f}.",
        )
        return redirect(f"{reverse('shop_order_list')}?view=handover")

    staff_wallet = None
    if payment_method == "STAFF_UPI":
        staff_wallet, _ = ShopStaffWallet.objects.select_for_update().get_or_create(
            staff=rider,
        )
        if staff_wallet.balance < received_amount:
            messages.error(
                request,
                "The rider's personal-UPI balance is lower than this handover amount.",
            )
            return redirect(f"{reverse('shop_order_list')}?view=handover")

    amount_left = received_amount
    touched_orders = []
    for collection, outstanding in remaining_by_collection:
        if amount_left <= 0:
            break
        amount_for_order = min(amount_left, outstanding)
        if amount_for_order <= 0:
            continue
        shop_order = collection.shop_order
        RiderCashLedgerEntry.objects.create(
            rider=rider,
            shop=shop,
            shop_order=shop_order,
            entry_type="HANDOVER",
            amount=amount_for_order,
            payment_method=payment_method,
            created_by=request.user,
        )
        if staff_wallet:
            staff_wallet.balance -= amount_for_order
            ShopStaffWalletTransaction.objects.create(
                wallet=staff_wallet,
                shop_order=shop_order,
                transaction_type="HANDOVER",
                amount=amount_for_order,
                balance_after=staff_wallet.balance,
                description=(
                    f"Personal UPI COD handed over for order "
                    f"{shop_order.order.order_id}."
                ),
            )
        amount_left -= amount_for_order
        touched_orders.append(shop_order)

    if staff_wallet:
        staff_wallet.save(update_fields=["balance", "updated_at"])
    for shop_order in touched_orders:
        _credit_shop_wallet_for_delivery(shop_order)

    if received_amount < total_remaining:
        messages.success(
            request,
            f"₹{received_amount:.2f} received. ₹{total_remaining - received_amount:.2f} remains to be handed over for this rider.",
        )
    else:
        messages.success(
            request,
            f"Full outstanding COD handover of ₹{received_amount:.2f} confirmed for this rider.",
        )
    return redirect(f"{reverse('shop_order_list')}?view=handover")


@login_required
@transaction.atomic
def edit_rider_cod_collection(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "order",
            "assigned_rider__user",
            "assigned_rider__shop",
        ),
        pk=shop_order_id,
    )
    rider = _rider_membership_for_order(request.user, shop_order)
    if (
        not rider
        or not shop_order.assigned_rider_id
        or shop_order.assigned_rider.user_id != request.user.id
        or shop_order.status != "DELIVERED"
    ):
        raise PermissionDenied

    collection = get_object_or_404(
        RiderCashLedgerEntry.objects.select_for_update(),
        shop_order=shop_order,
        rider=rider,
        entry_type="COLLECTION",
    )
    handed_over_amount = RiderCashLedgerEntry.objects.filter(
        shop_order=shop_order,
        entry_type="HANDOVER",
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if handed_over_amount >= collection.amount:
        messages.error(request, "COD received cannot be edited after handover is complete.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    payable_total = max(
        Decimal("0.00"),
        order.final_billing_amount - order.wallet_amount_used,
    )
    has_other_order_collections = RiderCashLedgerEntry.objects.filter(
        shop_order__order=order,
        entry_type="COLLECTION",
    ).exclude(pk=collection.pk).exists()
    has_other_cod_payments = CODBalancePayment.objects.filter(
        shop_order__order=order,
    ).exists()
    if (
        order.payment_type.upper() != "COD"
        or order.coupon_cashback_amount > 0
        or order.finally_received_amount != min(collection.amount, payable_total)
        or has_other_order_collections
        or has_other_cod_payments
    ):
        messages.error(
            request,
            "This COD amount cannot be edited because other order payments or adjustments are already recorded.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    raw_amount = (request.POST.get("received_amount") or "").strip()
    try:
        received_amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a valid COD amount.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)
    if (
        not received_amount.is_finite()
        or received_amount < handed_over_amount
        or received_amount > Decimal("999999999.99")
        or received_amount != received_amount.quantize(Decimal("0.01"))
    ):
        messages.error(
            request,
            f"COD amount must be at least ₹{handed_over_amount:.2f} already handed over.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    previous_amount = collection.amount
    if received_amount == previous_amount:
        messages.info(request, "The COD amount is unchanged.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    previous_overpayment = max(
        Decimal("0.00"),
        previous_amount - payable_total,
    )
    corrected_overpayment = max(
        Decimal("0.00"),
        received_amount - payable_total,
    )
    overpayment_change = corrected_overpayment - previous_overpayment
    customer = get_user_model().objects.filter(
        username=order.user,
    ).first()
    client_profile = (
        ClientProfile.objects.select_for_update().filter(user=customer).first()
        if customer
        else None
    )
    if overpayment_change and not client_profile:
        messages.error(
            request,
            "Customer wallet is unavailable, so the overpayment adjustment cannot be applied.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)
    if (
        overpayment_change < 0
        and client_profile
        and client_profile.wallet_balance < abs(overpayment_change)
    ):
        messages.error(
            request,
            "The customer wallet does not have enough balance to reverse the earlier overpayment credit.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    collection.amount = received_amount
    collection.save(update_fields=["amount"])
    applied_received_amount = min(received_amount, payable_total)
    order.finally_received_amount = applied_received_amount
    order.payment_amount = max(
        Decimal("0.00"),
        payable_total - applied_received_amount,
    )
    order.payment_status = "PAID" if order.payment_amount == 0 else "COD"
    order.is_payment_done = order.payment_amount == 0
    order.save(update_fields=[
        "finally_received_amount",
        "payment_amount",
        "payment_status",
        "is_payment_done",
        "updated_at",
    ])

    if overpayment_change and client_profile:
        if overpayment_change > 0:
            client_profile.wallet_balance += overpayment_change
            wallet_transaction_type = "CREDIT"
            wallet_description = (
                f"Extra payment from rider collection for order "
                f"{order.order_id}."
            )
        else:
            client_profile.wallet_balance -= abs(overpayment_change)
            wallet_transaction_type = "DEBIT"
            wallet_description = (
                f"Reversal of reduced rider overpayment for order "
                f"{order.order_id}."
            )
        client_profile.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client_profile,
            transaction_type=wallet_transaction_type,
            amount=abs(overpayment_change),
            balance_after=client_profile.wallet_balance,
            reference=order.order_id,
            description=wallet_description,
        )
        shop_wallet, _ = ShopWallet.objects.select_for_update().get_or_create(
            shop=shop_order.shop,
        )
        if overpayment_change > 0:
            shop_wallet.balance -= overpayment_change
            shop_transaction_type = "DEBIT"
        else:
            shop_wallet.balance += abs(overpayment_change)
            shop_transaction_type = "CREDIT"
        shop_wallet.save(update_fields=["balance", "updated_at"])
        ShopWalletTransaction.objects.create(
            wallet=shop_wallet,
            transaction_type=shop_transaction_type,
            amount=abs(overpayment_change),
            balance_after=shop_wallet.balance,
            shop_order=shop_order,
            description=(
                f"COD overpayment adjustment for order {order.order_id}."
            ),
        )

    if collection.payment_method == "STAFF_UPI":
        staff_wallet, _ = ShopStaffWallet.objects.select_for_update().get_or_create(
            staff=rider,
        )
        difference = received_amount - previous_amount
        staff_wallet.balance += difference
        staff_wallet.save(update_fields=["balance", "updated_at"])
        ShopStaffWalletTransaction.objects.create(
            wallet=staff_wallet,
            shop_order=shop_order,
            transaction_type="ADJUSTMENT",
            amount=abs(difference),
            balance_after=staff_wallet.balance,
            description=(
                f"COD collection corrected from ₹{previous_amount:.2f} "
                f"to ₹{received_amount:.2f}."
            ),
        )

    remaining_handover = received_amount - handed_over_amount
    if remaining_handover == 0:
        _credit_shop_wallet_for_delivery(shop_order)
        messages.success(
            request,
            "The COD amount was updated and the received amount is fully handed over.",
        )
    else:
        messages.success(
            request,
            (
                f"The COD amount was updated. ₹{remaining_handover:.2f} remains to be handed over."
                + (
                    f" The extra ₹{corrected_overpayment:.2f} was credited to the customer's wallet."
                    if overpayment_change > 0
                    else ""
                )
            ),
        )
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)


@login_required
@transaction.atomic
def confirm_rider_cash_handover(request, shop_order_id):
    if request.method != "POST":
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        pk=shop_order_id,
    )
    can_confirm = (
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="MANAGER",
            is_active=True,
        ).exists()
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="PAYMENT_MANAGER",
            can_confirm_rider_payments=True,
            is_active=True,
            is_public_rider=False,
        ).exists()
    )
    if not can_confirm:
        raise PermissionDenied

    if (
        shop_order.status != "DELIVERED"
        or shop_order.order.payment_type.upper() != "COD"
    ):
        messages.error(request, "Only delivered COD orders can be confirmed as cash handovers.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    collection = get_object_or_404(
        RiderCashLedgerEntry.objects.select_for_update().select_related("rider"),
        shop_order=shop_order,
        entry_type="COLLECTION",
    )
    handed_over_amount = RiderCashLedgerEntry.objects.filter(
        shop_order=shop_order,
        entry_type="HANDOVER",
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    remaining_amount = max(
        Decimal("0.00"),
        collection.amount - handed_over_amount,
    )
    if remaining_amount == 0:
        messages.info(request, "This rider cash handover has already been completed.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    raw_amount = (request.POST.get("received_amount") or "").strip()
    try:
        handover_amount = Decimal(raw_amount) if raw_amount else remaining_amount
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a valid amount received from the rider.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)
    if (
        not handover_amount.is_finite()
        or handover_amount <= 0
        or handover_amount > remaining_amount
        or handover_amount != handover_amount.quantize(Decimal("0.01"))
    ):
        messages.error(
            request,
            f"Handover amount must be greater than ₹0.00 and no more than ₹{remaining_amount:.2f}.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    RiderCashLedgerEntry.objects.create(
        rider=collection.rider,
        shop=shop_order.shop,
        shop_order=shop_order,
        entry_type="HANDOVER",
        amount=handover_amount,
        payment_method=collection.payment_method,
        created_by=request.user,
    )
    if collection.payment_method == "STAFF_UPI":
        staff_wallet, _ = ShopStaffWallet.objects.select_for_update().get_or_create(
            staff=collection.rider,
        )
        staff_wallet.balance -= handover_amount
        staff_wallet.save(update_fields=["balance", "updated_at"])
        ShopStaffWalletTransaction.objects.create(
            wallet=staff_wallet,
            shop_order=shop_order,
            transaction_type="HANDOVER",
            amount=handover_amount,
            balance_after=staff_wallet.balance,
            description=(
                f"Personal UPI COD handed over for order "
                f"{shop_order.order.order_id}."
            ),
        )
    _credit_shop_wallet_for_delivery(shop_order)

    if handover_amount < remaining_amount:
        messages.success(
            request,
            f"₹{handover_amount:.2f} received. ₹{remaining_amount - handover_amount:.2f} remains to be handed over.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    if SiteWalletTransaction.objects.filter(shop_order=shop_order).exists():
        messages.success(
            request,
            f"₹{collection.amount:.2f} COD handover confirmed and shop proceeds credited after commission.",
        )
    else:
        messages.warning(
            request,
            f"₹{collection.amount:.2f} handover recorded. Shop settlement remains pending until the COD order is fully paid.",
        )
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)


@login_required
@transaction.atomic
def collect_cod_balance(request, shop_order_id):
    if request.method != "POST":
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        pk=shop_order_id,
    )
    can_collect = (
        shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="MANAGER",
            is_active=True,
        ).exists()
    )
    if not can_collect:
        raise PermissionDenied
    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    if shop_order.status != "DELIVERED" or order.payment_type.upper() != "COD":
        messages.error(request, "Only delivered COD orders can accept a remaining-balance payment.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    try:
        amount = Decimal(request.POST.get("amount", "").strip())
        if (
            not amount.is_finite()
            or amount <= 0
            or amount > order.payment_amount
            or amount > Decimal("999999999.99")
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise InvalidOperation
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        messages.error(
            request,
            f"Enter a valid payment up to the remaining COD balance of ₹{order.payment_amount:.2f}.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    previous_due = order.payment_amount
    CODBalancePayment.objects.create(
        shop_order=shop_order,
        amount=amount,
        received_by=request.user,
    )
    applied_amount = _settle_order_wallet(order, amount)
    if applied_amount != amount:
        raise RuntimeError("COD balance payment did not reconcile with the order balance.")

    for delivered_shop_order in (
        ShopOrder.objects.select_for_update()
        .filter(order=order, status="DELIVERED")
        .select_related("order", "shop")
        .order_by("id")
    ):
        _credit_shop_wallet_for_delivery(delivered_shop_order)

    order.refresh_from_db(fields=["payment_amount", "is_payment_done"])
    if order.is_payment_done:
        messages.success(
            request,
            f"₹{amount:.2f} recorded. The full COD balance is paid and eligible shop settlements have been released.",
        )
    else:
        messages.success(
            request,
            f"₹{amount:.2f} recorded. Remaining COD balance: ₹{order.payment_amount:.2f}.",
        )
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)


@login_required
@transaction.atomic
def confirm_returned_items_received(request, shop_order_id):
    if request.method != "POST":
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        pk=shop_order_id,
    )
    if not (
        request.user.is_superuser
        or _can_manage_shop_stock(request, shop_order.shop)
    ):
        raise PermissionDenied

    pending_items = list(
        OrderItem.objects.select_for_update()
        .filter(
            shop_order=shop_order,
            returned_quantity__gt=F("returned_received_quantity"),
        )
        .order_by("id")
    )
    if not pending_items:
        messages.info(request, "There are no rider-held returned items awaiting receipt.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    if any(item.product_shop_id is None for item in pending_items):
        messages.error(
            request,
            "A returned item no longer has a shop stock record. Restore its product listing before confirming receipt.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    assignments = {
        assignment.pk: assignment
        for assignment in ProductShop.objects.select_for_update()
        .filter(pk__in={item.product_shop_id for item in pending_items})
        .order_by("pk")
    }
    if any(item.product_shop_id not in assignments for item in pending_items):
        messages.error(
            request,
            "A returned item's shop stock record could not be found. No returns were received.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    received_by_assignment = defaultdict(int)
    for item in pending_items:
        quantity = item.returned_quantity - item.returned_received_quantity
        received_by_assignment[item.product_shop_id] += quantity
        item.returned_received_quantity = item.returned_quantity
        item.save(update_fields=["returned_received_quantity"])
        StockMovement.objects.create(
            product_shop=assignments[item.product_shop_id],
            movement_type="ADJUSTMENT",
            quantity=quantity,
            reference=(
                f"Customer return received for order {shop_order.order.order_id} "
                f"(item {item.pk})"
            ),
            created_by=request.user,
        )

    for assignment_id, quantity in received_by_assignment.items():
        assignment = assignments[assignment_id]
        stock_field = "b2b_stock" if shop_order.order.is_b2b_order else "stock"
        current_stock = assignment.stock_for_customer(
            is_b2b=shop_order.order.is_b2b_order,
        )
        setattr(assignment, stock_field, current_stock + quantity)
        assignment.save(update_fields=[stock_field])

    messages.success(
        request,
        f"Received and restocked {sum(received_by_assignment.values())} returned item(s).",
    )
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)

@login_required
@transaction.atomic
def finalize_approximate_weights(request, shop_order_id):
    if request.method != "POST":
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        id=shop_order_id,
    )
    staff = ShopStaff.objects.filter(
        shop=shop_order.shop,
        user=request.user,
        is_active=True,
    ).first()
    can_finalize = (
        shop_order.shop.owner_id == request.user.id
        or bool(staff and (
            staff.can_pack_orders
            or shop_order.assigned_packer_id == staff.id
        ))
    )
    if not can_finalize:
        raise PermissionDenied

    order_items = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order=shop_order)
        .select_related("product")
        .order_by("id")
    )
    approximate_items = [
        item for item in order_items if item.is_approximate_weight
    ]
    if not approximate_items:
        messages.error(request, "This order has no approximate-weight items.")
        return _shop_order_assignment_redirect(request, shop_order)

    final_weights = {}
    for item in approximate_items:
        raw_weight = (request.POST.get(f"final_weight_{item.id}") or "").strip()
        try:
            weight = Decimal(raw_weight)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, f"Enter a valid final weight for {item.product_name}.")
            return _shop_order_assignment_redirect(request, shop_order)
        if not weight.is_finite() or weight <= 0 or weight > Decimal("9999999.999"):
            messages.error(request, f"Final weight for {item.product_name} must be greater than zero.")
            return _shop_order_assignment_redirect(request, shop_order)
        quantized_weight = weight.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if quantized_weight != weight:
            messages.error(request, f"Final weight for {item.product_name} can have at most 3 decimal places.")
            return _shop_order_assignment_redirect(request, shop_order)
        final_weights[item.id] = quantized_weight

    for item in approximate_items:
        item.final_weight = final_weights[item.id]
        item.total = _approximate_weight_total(
            item.price,
            item.final_weight,
            item.unit or (item.product.unit if item.product_id else ""),
        )
        item.save(update_fields=["final_weight", "total"])

    updated_shop_total = sum(
        (item.total for item in order_items),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shop_order.total_amount = updated_shop_total
    shop_order.total_weight_kg = sum(
        (
            _weight_in_kilograms(
                item.final_weight or item.estimated_weight or Decimal("0.000"),
                item.unit or (item.product.unit if item.product_id else ""),
            )
            for item in order_items
            if item.is_approximate_weight
        ),
        Decimal("0.000"),
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shop_order.save(update_fields=["total_amount", "total_weight_kg", "updated_at"])

    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    all_shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
    )
    delivery_fees = {
        row.shop_id: row.delivery_fee
        for row in all_shop_orders
    }
    tax_shops = {
        row.shop_id: _gst_shop_snapshot(row)
        for row in all_shop_orders
    }
    all_order_items = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order__order=order)
        .select_related("shop_order__shop", "product")
        .order_by("id")
    )
    gst_items = [
        {
            "item": {"total": line.total},
            "total": line.total,
            "shop": tax_shops[line.shop_order.shop_id],
            "product": line.product,
            "gst_rate": line.gst_rate,
            "hsn_code": line.hsn_code,
        }
        for line in all_order_items
    ]
    buyer_state_code = next(
        (row.buyer_state_code for row in all_shop_orders if row.buyer_state_code),
        "",
    )
    gst_breakdown = _build_gst_breakdown(
        gst_items,
        SimpleNamespace(state_code=buyer_state_code, gstin=""),
        order.coupon_discount_amount,
        delivery_fees=delivery_fees,
    )
    for line, gst_item in zip(all_order_items, gst_items):
        line.taxable_amount = gst_item["taxable_amount"]
        line.cgst_amount = gst_item["cgst_amount"]
        line.sgst_amount = gst_item["sgst_amount"]
        line.igst_amount = gst_item["igst_amount"]
        line.save(update_fields=[
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
        ])
    for row in all_shop_orders:
        breakdown = gst_breakdown["shops"][row.shop_id]
        row.taxable_amount = breakdown["taxable_amount"]
        row.cgst_amount = breakdown["cgst_amount"]
        row.sgst_amount = breakdown["sgst_amount"]
        row.igst_amount = breakdown["igst_amount"]
        row.delivery_gst_amount = breakdown["delivery_gst_amount"]
        row.buyer_state_code = breakdown["buyer_state_code"]
        row.buyer_gstin = breakdown["buyer_gstin"]
        row.save(update_fields=[
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "delivery_gst_amount",
            "buyer_state_code",
            "buyer_gstin",
        ])
    gross_subtotal = (
        OrderItem.objects.filter(shop_order__order=order).aggregate(total=Sum("total"))["total"]
        or Decimal("0.00")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    coupon_discount = min(order.coupon_discount_amount, gross_subtotal)
    discounted_total = max(Decimal("0.00"), gross_subtotal - coupon_discount)
    wallet_used = min(order.wallet_amount_used, discounted_total)
    charge_method = (
        "RAZORPAY"
        if order.payment_type == "RAZORPAY" or order.razorpay_fee_amount > 0
        else "COD"
    )
    checkout_extra = gst_breakdown["total_gst"] + gst_breakdown["delivery_total"]
    updated_charges = _checkout_charge_totals(
        gross_subtotal,
        coupon_discount,
        wallet_used,
        charge_method,
        checkout_extra,
    )
    revised_total = (
        discounted_total
        + updated_charges["platform_charge"]
        + updated_charges["razorpay_fee"]
        + checkout_extra
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    previous_due = max(
        Decimal("0.00"),
        order.final_billing_amount - order.wallet_amount_used,
    )
    settled_amount = (
        order.finally_received_amount
        if order.finally_received_amount > 0
        else previous_due if order.is_payment_done else Decimal("0.00")
    )
    wallet_refund = order.wallet_amount_used - wallet_used
    revised_due = max(Decimal("0.00"), revised_total - wallet_used)
    paid_refund = max(Decimal("0.00"), settled_amount - revised_due)
    refund = wallet_refund + paid_refund

    if refund:
        customer_user = get_user_model().objects.filter(username=order.user).first()
        client = (
            ClientProfile.objects.select_for_update().filter(user=customer_user).first()
            if customer_user else None
        )
        if client:
            client.wallet_balance += refund
            client.save(update_fields=["wallet_balance", "updated_at"])
            ClientWalletTransaction.objects.create(
                client=client,
                transaction_type="CREDIT",
                amount=refund,
                balance_after=client.wallet_balance,
                reference=order.order_id,
                description="Refund after final approximate-weight billing.",
            )

    settled_amount = min(settled_amount, revised_due)
    due_amount = revised_due - settled_amount
    order.final_billing_amount = revised_total
    order.wallet_amount_used = wallet_used
    order.coupon_discount_amount = coupon_discount
    order.platform_charge_amount = updated_charges["platform_charge"]
    order.razorpay_fee_amount = updated_charges["razorpay_fee"]
    order.gst_amount = gst_breakdown["total_gst"]
    order.finally_received_amount = settled_amount
    order.payment_amount = due_amount
    order.is_payment_done = due_amount == 0
    order.payment_status = "PAID" if due_amount == 0 else "PENDING"
    order.final_billing_notified_at = timezone.now()
    order.save(update_fields=[
        "final_billing_amount",
        "wallet_amount_used",
        "coupon_discount_amount",
        "platform_charge_amount",
        "razorpay_fee_amount",
        "gst_amount",
        "finally_received_amount",
        "payment_amount",
        "is_payment_done",
        "payment_status",
        "final_billing_notified_at",
        "updated_at",
    ])

    customer_user = get_user_model().objects.filter(username=order.user).first()
    if customer_user:
        final_weight_details = ", ".join(
            f"{item.product_name}: {item.final_weight} "
            f"{_weight_display_unit(item.unit or (item.product.unit if item.product_id else ''))}"
            for item in approximate_items
        )
        Notification.objects.create(
            user=customer_user,
            title="Final order weight and bill updated",
            message=(
                f"Your order {order.order_id} has been packed. "
                f"Final weight: {final_weight_details}. "
                f"Updated order total: ₹{revised_total:.2f}."
            ),
            notification_type="ORDER",
            related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
        )

    messages.success(
        request,
        f"Final weights saved. Updated order total: ₹{revised_total:.2f}.",
    )
    return _shop_order_assignment_redirect(request, shop_order)

@login_required
@transaction.atomic
def shop_order_status(request, shop_order_id):

    if request.method != "POST":
        return redirect(
            "shop_order_detail",
            shop_order_id=shop_order_id
        )

    shop_order = get_object_or_404(
        ShopOrder.objects.select_related(
            "shop",
            "order"
        ),
        id=shop_order_id
    )

    if not user_can_change_status(
        request.user,
        shop_order.shop
    ):
        raise PermissionDenied

    if shop_order.status.upper() in {"DELIVERED", "CANCELLED"}:
        messages.error(
            request,
            "Delivered or cancelled orders cannot have their status changed.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    new_status = request.POST.get("status")

    allowed_statuses = {
        "pending",
        "PLACED",
        "CONFIRMED",
        "PACKING",
        "READY",
        "PICKED_UP",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
        "CANCELLED",
    }

    if new_status not in allowed_statuses:

        messages.error(
            request,
            "Invalid order status."
        )

        return redirect(
            "shop_order_detail",
            shop_order_id=shop_order_id
        )

    old_status = shop_order.status
    if new_status in {"PICKED_UP", "OUT_FOR_DELIVERY"}:
        messages.error(
            request,
            "The rider confirms parcel pickup; out-for-delivery starts automatically after all assigned parcels are collected.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    shop_order.status = new_status
    update_fields = ["status", "updated_at"]
    if new_status == "CANCELLED":
        refund_ok, refund_message = _refund_cancelled_shop_order(
            shop_order,
            request.user,
        )
        if not refund_ok:
            messages.error(request, refund_message)
            return _shop_order_assignment_redirect(request, shop_order)
        shop_order.cancellation_source = "SHOP"
        update_fields.append("cancellation_source")
        _restock_cancelled_shop_order(shop_order, request.user)
    shop_order.save(
        update_fields=update_fields
    )

    if new_status != "CANCELLED":
        shop_order.order.status = new_status
        shop_order.order.save(update_fields=["status", "updated_at"])
    elif not ShopOrder.objects.filter(
        order=shop_order.order
    ).exclude(status__iexact="CANCELLED").exists():
        shop_order.order.status = "CANCELLED"
        shop_order.order.cancellation_source = "SHOP"
        shop_order.order.save(update_fields=["status", "cancellation_source", "updated_at"])

    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status=new_status,
        remark=f"Status changed from {old_status} to {new_status} by {request.user.username}"
    )

    if new_status == "READY" and old_status != "READY":
        if not _route_ready_order_to_public_riders_if_no_staff(shop_order):
            _notify_riders_order_ready(shop_order)

    messages.success(
        request,
        f"Order status changed to {new_status}."
    )

    return redirect(
        "shop_order_detail",
        shop_order_id=shop_order_id
    )

def can_change_status(staff, current_status, new_status):

    if not staff:
        return False

    if new_status == "CONFIRMED":
        return staff.can_confirm_orders

    if new_status == "PACKING":
        return staff.can_pack_orders

    if new_status == "READY":
        return staff.can_pack_orders or staff.can_change_order_status

    if new_status == "PICKED_UP":
        return staff.can_change_order_status

    if new_status == "OUT_FOR_DELIVERY":
        return staff.can_change_order_status

    if new_status == "DELIVERED":
        return staff.can_change_order_status

    if new_status == "CANCELLED":
        return staff.can_cancel_orders

    return False

@login_required
@transaction.atomic
def shop_order_status_update(
    request,
    shop_order_id
):

    shop_order = get_object_or_404(
        ShopOrder.objects.select_related(
            "shop",
            "order"
        ),
        id=shop_order_id
    )

    shop = shop_order.shop

    is_owner = (
        shop.owner_id == request.user.id
    )

    staff = None

    if not is_owner:

        staff = (
            ShopStaff.objects
            .filter(
                shop=shop,
                user=request.user,
                is_active=True
            )
            .first()
        )

        if not staff:
            return HttpResponseForbidden(
                "Unauthorized."
            )

    new_status = request.POST.get(
        "status"
    )

    current_status = shop_order.status

    if current_status.upper() in {"DELIVERED", "CANCELLED"}:
        messages.error(
            request,
            "Delivered or cancelled orders cannot have their status changed.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.id)

    if not is_owner:

        if not can_change_status(
            staff,
            current_status,
            new_status
        ):
            return HttpResponseForbidden(
                "You do not have permission."
            )

    allowed_transitions = {
        "PLACED": [
            "CONFIRMED",
            "CANCELLED",
        ],

        "CONFIRMED": [
            "PACKING",
            "CANCELLED",
        ],

        "PACKING": [
            "READY",
            "CANCELLED",
        ],

        "READY": [
            "CANCELLED",
        ],

        "PICKED_UP": [
            "CANCELLED",
        ],

        "OUT_FOR_DELIVERY": [
            "DELIVERED",
            "CANCELLED",
        ],

        "DELIVERED": [],

        "CANCELLED": [],
    }

    if new_status in {"PICKED_UP", "OUT_FOR_DELIVERY"}:
        messages.error(
            request,
            "The rider confirms parcel pickup; out-for-delivery starts automatically after all assigned parcels are collected.",
        )
        return redirect(
            "shop_order_detail",
            shop_order_id=shop_order.id,
        )

    if new_status not in allowed_transitions.get(
        current_status,
        []
    ):

        messages.error(
            request,
            "Invalid order status transition."
        )

        return redirect(
            "shop_order_detail",
            shop_order_id=shop_order.id
        )

    shop_order.status = new_status
    update_fields = ["status", "updated_at"]
    if new_status == "CANCELLED":
        refund_ok, refund_message = _refund_cancelled_shop_order(
            shop_order,
            request.user,
        )
        if not refund_ok:
            messages.error(request, refund_message)
            return redirect("shop_order_detail", shop_order_id=shop_order.id)
        shop_order.cancellation_source = "SHOP"
        update_fields.append("cancellation_source")
        _restock_cancelled_shop_order(shop_order, request.user)
    shop_order.save(update_fields=update_fields)

    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status=new_status,
        remark=request.POST.get(
            "remark",
            ""
        )
    )

    if new_status == "READY" and current_status != "READY":
        if not _route_ready_order_to_public_riders_if_no_staff(shop_order):
            _notify_riders_order_ready(shop_order)

    if new_status != "CANCELLED":
        shop_order.order.status = new_status
        shop_order.order.save(update_fields=["status", "updated_at"])
    elif not ShopOrder.objects.filter(
        order=shop_order.order
    ).exclude(status__iexact="CANCELLED").exists():
        shop_order.order.status = "CANCELLED"
        shop_order.order.cancellation_source = "SHOP"
        shop_order.order.save(update_fields=["status", "cancellation_source", "updated_at"])

    messages.success(
        request,
        f"Order moved to {new_status}."
    )

    if (
        new_status == "READY"
        and staff
        and staff.role == "PACKER"
        and shop_order.assigned_packer_id == staff.id
    ):
        return redirect(
            f"{reverse('packer_dashboard')}?view=ready#order-{shop_order.id}"
        )

    return redirect(
        "shop_order_detail",
        shop_order_id=shop_order.id
    ) 


def _notify_riders_order_ready(shop_order):
    riders = ShopStaff.objects.filter(
        shop_id=shop_order.shop_id,
        role="RIDER",
        is_active=True,
    )
    if shop_order.assigned_rider_id:
        riders = riders.filter(pk=shop_order.assigned_rider_id)
    rider_user_ids = riders.values_list("user_id", flat=True).distinct()
    Notification.objects.bulk_create([
        Notification(
            user_id=user_id,
            title="Delivery ready for pickup",
            message=f"Order {shop_order.order.order_id} is ready. Open it to review the delivery and accept it.",
            notification_type="ORDER",
            related_url=(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
                if shop_order.assigned_rider_id
                else f"{reverse('rider_dashboard')}?view=available"
            ),
        )
        for user_id in rider_user_ids
    ])


def _route_ready_order_to_public_riders_if_no_staff(shop_order):
    if shop_order.assigned_rider_id or not public_rider_service_enabled():
        return False

    has_shop_rider = ShopStaff.objects.filter(
            shop_id=shop_order.shop_id,
            role="RIDER",
            is_active=True,
            is_public_rider=False,
        ).exists()
    if has_shop_rider:
        return False

    if (
        not shop_order.shop.public_rider_network_enabled
        or shop_order.shop.rider_delivery_preference != "PUBLIC"
    ):
        shop_order.shop.public_rider_network_enabled = True
        shop_order.shop.rider_delivery_preference = "PUBLIC"
        shop_order.shop.save(update_fields=[
            "public_rider_network_enabled",
            "rider_delivery_preference",
            "updated_at",
        ])

    shop_order.public_rider_network_requested = True
    shop_order.public_rider_fallback_to_staff = False
    shop_order.public_rider_offer_user = None
    shop_order.public_rider_offer_expires_at = None
    shop_order.public_rider_offer_retry_at = None
    shop_order.public_rider_offer_attempted_user_ids = []
    shop_order.public_rider_offer_declined_until = {}
    shop_order.save(update_fields=[
        "public_rider_network_requested",
        "public_rider_fallback_to_staff",
        "public_rider_offer_user",
        "public_rider_offer_expires_at",
        "public_rider_offer_retry_at",
        "public_rider_offer_attempted_user_ids",
        "public_rider_offer_declined_until",
        "updated_at",
    ])
    advance_public_rider_offer(shop_order.pk)
    return True


def _shop_order_assignment_redirect(request, shop_order):
    next_url = (request.POST.get("next") or "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("shop_order_detail", shop_order_id=shop_order.id)


@login_required
def assign_packer(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_related(
            "shop"
        ),
        id=shop_order_id
    )

    shop = shop_order.shop

    if shop.owner_id != request.user.id:

        staff = ShopStaff.objects.filter(
            shop=shop,
            user=request.user,
            is_active=True,
            can_assign_staff=True
        ).first()

        if not staff:
            return HttpResponseForbidden(
                "You cannot assign packers."
            )

    packer_id = request.POST.get(
        "packer"
    )

    packer = get_object_or_404(
        ShopStaff,
        id=packer_id,
        shop=shop,
        role="PACKER",
        is_active=True,
    )

    shop_order.assigned_packer = packer
    # assign packer and update status to PACKING when appropriate
    if shop_order.status in ["PLACED", "CONFIRMED"]:
        shop_order.assigned_packer = packer
        shop_order.status = "PACKING"
        shop_order.save(update_fields=["assigned_packer", "status", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PACKING",
            remark="Packer assigned by owner."
        )
        if shop_order.order:
            shop_order.order.assigned_packer = packer.user.username
            shop_order.order.status = "PACKING"
            shop_order.order.save(update_fields=["assigned_packer", "status", "updated_at"])
    else:
        shop_order.assigned_packer = packer
        shop_order.save(
            update_fields=[
                "assigned_packer",
                "updated_at",
            ]
        )

    messages.success(
        request,
        "Packer assigned successfully."
    )

    return _shop_order_assignment_redirect(request, shop_order)

@login_required
def assign_rider(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_related("shop", "order"),
        id=shop_order_id,
    )

    shop = shop_order.shop

    if shop.owner_id != request.user.id:
        staff = ShopStaff.objects.filter(
            shop=shop,
            user=request.user,
            is_active=True,
            can_assign_staff=True,
        ).first()

        if not staff:
            return HttpResponseForbidden("You cannot assign riders.")

    rider_id = request.POST.get("rider")

    if rider_id == "PUBLIC":
        if (
            not public_rider_service_enabled()
            or not shop.public_rider_network_enabled
        ):
            messages.error(
                request,
                "The public rider network is disabled by the site administrator or this shop.",
            )
            return _shop_order_assignment_redirect(request, shop_order)
        if shop_order.status != "READY":
            messages.error(
                request,
                "Only ready orders can be sent to the public rider network.",
            )
            return _shop_order_assignment_redirect(request, shop_order)

        shop_order.assigned_rider = None
        shop_order.rider_accepted_at = None
        shop_order.public_rider_network_requested = True
        shop_order.public_rider_fallback_to_staff = False
        shop_order.public_rider_offer_user = None
        shop_order.public_rider_offer_expires_at = None
        shop_order.public_rider_offer_retry_at = None
        shop_order.public_rider_offer_attempted_user_ids = []
        shop_order.public_rider_offer_declined_until = {}
        shop_order.public_rider_route_estimate = {}
        shop_order.rider_identity_otp = ""
        shop_order.rider_identity_otp_expires_at = None
        shop_order.rider_identity_otp_attempts = 0
        shop_order.rider_order_acknowledged = False
        shop_order.rider_pickup_otp = ""
        shop_order.rider_pickup_otp_expires_at = None
        shop_order.rider_pickup_otp_attempts = 0
        shop_order.save(update_fields=[
            "assigned_rider",
            "rider_accepted_at",
            "public_rider_network_requested",
            "public_rider_fallback_to_staff",
            "public_rider_offer_user",
            "public_rider_offer_expires_at",
            "public_rider_offer_retry_at",
            "public_rider_offer_attempted_user_ids",
            "public_rider_offer_declined_until",
            "public_rider_route_estimate",
            "rider_identity_otp",
            "rider_identity_otp_expires_at",
            "rider_identity_otp_attempts",
            "rider_order_acknowledged",
            "rider_pickup_otp",
            "rider_pickup_otp_expires_at",
            "rider_pickup_otp_attempts",
            "updated_at",
        ])
        if shop_order.order:
            shop_order.order.assigned_rider = ""
            shop_order.order.rider_contact_no = ""
            shop_order.order.save(update_fields=[
                "assigned_rider",
                "rider_contact_no",
                "updated_at",
            ])
        messages.success(request, "Order sent to the public rider network.")
        return _shop_order_assignment_redirect(request, shop_order)

    if not rider_id:
        messages.error(request, "Please select a rider.")
        return _shop_order_assignment_redirect(request, shop_order)

    rider = get_object_or_404(
        ShopStaff,
        id=rider_id,
        shop=shop,
        role="RIDER",
        is_active=True,
        is_public_rider=False,
    )

    shop_order.assigned_rider = rider
    shop_order.rider_accepted_at = timezone.now()
    shop_order.public_rider_network_requested = False
    shop_order.public_rider_fallback_to_staff = False
    shop_order.public_rider_offer_user = None
    shop_order.public_rider_offer_expires_at = None
    shop_order.public_rider_offer_retry_at = None
    shop_order.public_rider_offer_attempted_user_ids = []
    shop_order.public_rider_offer_declined_until = {}
    shop_order.public_rider_route_estimate = {}
    shop_order.rider_identity_otp = ""
    shop_order.rider_identity_otp_expires_at = None
    shop_order.rider_identity_otp_attempts = 0
    shop_order.rider_order_acknowledged = False
    shop_order.rider_pickup_otp = ""
    shop_order.rider_pickup_otp_expires_at = None
    shop_order.rider_pickup_otp_attempts = 0
    shop_order.save(update_fields=[
        "assigned_rider",
        "rider_accepted_at",
        "public_rider_network_requested",
        "public_rider_fallback_to_staff",
        "public_rider_offer_user",
        "public_rider_offer_expires_at",
        "public_rider_offer_retry_at",
        "public_rider_offer_attempted_user_ids",
        "public_rider_offer_declined_until",
        "public_rider_route_estimate",
        "rider_identity_otp",
        "rider_identity_otp_expires_at",
        "rider_identity_otp_attempts",
        "rider_order_acknowledged",
        "rider_pickup_otp",
        "rider_pickup_otp_expires_at",
        "rider_pickup_otp_attempts",
        "updated_at",
    ])

    if shop_order.order:
        shop_order.order.assigned_rider = rider.user.username
        shop_order.order.rider_contact_no = rider.mobile_no
        shop_order.order.save(update_fields=[
            "assigned_rider",
            "rider_contact_no",
            "updated_at",
        ])

    messages.success(request, "Rider assigned successfully.")

    return _shop_order_assignment_redirect(request, shop_order)


@login_required
@transaction.atomic
def cancel_rider_assignment(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "shop",
            "order",
            "assigned_rider",
            "assigned_rider__user",
        ),
        pk=shop_order_id,
    )
    shop = shop_order.shop
    if shop.owner_id != request.user.id and not ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        is_active=True,
        can_assign_staff=True,
    ).exists():
        return HttpResponseForbidden("You cannot cancel rider assignments.")
    if not shop_order.assigned_rider_id:
        messages.error(request, "There is no assigned rider to cancel.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)
    if shop_order.status not in {"READY", "RIDER_ASSIGNED"}:
        messages.error(
            request,
            "A rider assignment can only be cancelled before the parcel is picked up.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    assigned_rider = shop_order.assigned_rider
    was_public_rider = assigned_rider.is_public_rider
    reason = (request.POST.get("reason") or "").strip()
    cancellation_time = timezone.now()
    declined_public_riders = {}
    if was_public_rider:
        cooldown_minutes = _delivery_sharing_settings().public_rider_decline_cooldown_minutes
        declined_public_riders[str(assigned_rider.user_id)] = (
            cancellation_time
            + timedelta(minutes=float(cooldown_minutes))
        ).timestamp()
    shop_order.assigned_rider = None
    shop_order.rider_accepted_at = None
    shop_order.status = "READY"
    shop_order.rider_pickup_otp = ""
    shop_order.rider_pickup_otp_expires_at = None
    shop_order.rider_pickup_otp_attempts = 0
    shop_order.rider_last_location = {}
    shop_order.public_rider_route_estimate = {}
    shop_order.public_rider_offer_user = None
    shop_order.public_rider_offer_expires_at = None
    shop_order.public_rider_offer_retry_at = None
    shop_order.public_rider_offer_attempted_user_ids = []
    shop_order.public_rider_offer_declined_until = declined_public_riders
    shop_order.public_rider_network_requested = was_public_rider
    shop_order.public_rider_fallback_to_staff = bool(
        not was_public_rider
        and public_rider_service_enabled()
        and shop.public_rider_network_enabled
        and shop.rider_delivery_preference == "PUBLIC"
    )
    shop_order.save(update_fields=[
        "assigned_rider",
        "rider_accepted_at",
        "status",
        "rider_pickup_otp",
        "rider_pickup_otp_expires_at",
        "rider_pickup_otp_attempts",
        "rider_last_location",
        "public_rider_route_estimate",
        "public_rider_offer_user",
        "public_rider_offer_expires_at",
        "public_rider_offer_retry_at",
        "public_rider_offer_attempted_user_ids",
        "public_rider_offer_declined_until",
        "public_rider_network_requested",
        "public_rider_fallback_to_staff",
        "updated_at",
    ])
    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="READY",
        remark=(
            f"{'Public' if was_public_rider else 'Shop'} rider assignment "
            f"cancelled by {request.user.username}"
            + (f": {reason}" if reason else ".")
        ),
    )
    Notification.objects.create(
        user=assigned_rider.user,
        title="Delivery assignment cancelled",
        message=(
            f"Your assignment for order "
            f"{shop_order.shop_order_number or shop_order.order.order_id} "
            f"from {shop.name} was cancelled by the shop."
        ),
        notification_type="ORDER",
        related_url=reverse("rider_dashboard"),
    )

    if shop_order.order:
        other_assigned_rider = (
            ShopOrder.objects.filter(
                order=shop_order.order,
                assigned_rider__isnull=False,
            )
            .select_related("assigned_rider__user")
            .order_by("pk")
            .first()
        )
        if other_assigned_rider:
            shop_order.order.assigned_rider = (
                other_assigned_rider.assigned_rider.user.username
            )
            shop_order.order.status = "RIDER_ASSIGNED"
            shop_order.order.save(update_fields=[
                "assigned_rider",
                "status",
                "updated_at",
            ])
        else:
            other_statuses = list(
                ShopOrder.objects.filter(order=shop_order.order)
                .exclude(pk=shop_order.pk)
                .values_list("status", flat=True)
            )
            if all(
                status in {"PLACED", "CONFIRMED", "PACKING", "READY", "CANCELLED"}
                for status in other_statuses
            ):
                shop_order.order.assigned_rider = ""
                shop_order.order.status = "READY"
                shop_order.order.save(update_fields=[
                    "assigned_rider",
                    "status",
                    "updated_at",
                ])

    if was_public_rider:
        advance_public_rider_offer(shop_order.pk)
        message = (
            "Public rider assignment cancelled. The order is back in the "
            "public rider offer queue."
        )
    else:
        message = (
            "Shop rider assignment cancelled. The order is ready for "
            "reassignment by the shop."
        )
    messages.success(request, message)
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)


@login_required
@transaction.atomic
def save_public_rider_work_area(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    if request.session.get("active_portal") != "RIDER":
        raise PermissionDenied
    application = PublicRiderApplication.objects.select_for_update().filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).first()
    if not application:
        raise PermissionDenied

    work_areas = []
    raw_work_areas = (request.POST.get("work_areas") or "").strip()
    if raw_work_areas:
        try:
            parsed_work_areas = json.loads(raw_work_areas)
        except json.JSONDecodeError:
            parsed_work_areas = []
        if isinstance(parsed_work_areas, list):
            for value in parsed_work_areas:
                try:
                    work_areas.append(validate_work_area(value))
                except ValueError as error:
                    messages.error(request, str(error) or "Enter valid work areas.")
                    return redirect("rider_dashboard")

    raw_work_area = (request.POST.get("work_area") or "").strip()
    if raw_work_area:
        try:
            work_area = validate_work_area(json.loads(raw_work_area))
        except (json.JSONDecodeError, ValueError) as error:
            messages.error(request, str(error) or "Enter a valid work area.")
            return redirect("rider_dashboard")
        if not work_areas:
            work_areas = [work_area]
        elif work_area not in work_areas:
            work_areas.insert(0, work_area)
        work_area = work_areas[0]
    elif work_areas:
        work_area = work_areas[0]
    else:
        work_area = {}
    if work_areas:
        application.work_areas = work_areas
    else:
        application.work_areas = []
    application.work_area = work_area

    home_area = {}
    raw_home_area = (request.POST.get("home_area") or "").strip()
    if raw_home_area:
        try:
            candidate_home_area = validate_work_area(json.loads(raw_home_area))
        except (json.JSONDecodeError, ValueError) as error:
            messages.error(request, str(error) or "Enter a valid home area.")
            return redirect("rider_dashboard")
        if candidate_home_area.get("type") != "circle":
            messages.error(request, "Home area must be a circular radius around your home location.")
            return redirect("rider_dashboard")
        home_area = candidate_home_area
    application.home_area = home_area

    raw_return_home_enabled = request.POST.get("return_home_enabled")
    return_home_enabled = str(raw_return_home_enabled).lower() in {
        "on",
        "true",
        "1",
        "yes",
    }
    if return_home_enabled and not home_area:
        messages.error(
            request,
            "Set your home circle before enabling automatic homeward offers.",
        )
        return redirect("rider_dashboard")
    return_home_after_minutes = request.POST.get(
        "return_home_after_minutes",
        "120",
    ).strip()
    try:
        parsed_minutes = int(return_home_after_minutes)
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid homeward offer duration.")
        return redirect("rider_dashboard")
    if not 5 <= parsed_minutes <= 1440:
        messages.error(
            request,
            "Homeward offer duration must be between 5 and 1440 minutes.",
        )
        return redirect("rider_dashboard")
    raw_start_time = request.POST.get("return_home_start_time", "13:00").strip()
    try:
        parsed_start_time = datetime.strptime(
            raw_start_time,
            "%H:%M",
        ).time()
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid homeward offer start time.")
        return redirect("rider_dashboard")
    application.return_home_enabled = return_home_enabled
    application.return_home_after_minutes = parsed_minutes
    application.return_home_start_time = parsed_start_time

    application.save(update_fields=[
        "work_area",
        "work_areas",
        "home_area",
        "return_home_enabled",
        "return_home_after_minutes",
        "return_home_start_time",
        "updated_at",
    ])
    messages.success(
        request,
        "Public rider work area and home return settings saved."
        if work_area or home_area
        else "Public rider work area cleared. You will not receive public offers until you draw a new area.",
    )
    return redirect("rider_dashboard")


@login_required
@transaction.atomic
def set_public_rider_availability(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    if request.session.get("active_portal") != "RIDER":
        raise PermissionDenied
    application = PublicRiderApplication.objects.select_for_update().filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).first()
    if not application:
        raise PermissionDenied

    is_online = request.POST.get("is_online") == "true"
    application.is_online = is_online
    application.save(update_fields=["is_online", "updated_at"])

    if not is_online:
        pending_orders = list(
            ShopOrder.objects.select_for_update()
            .filter(
                public_rider_offer_user=request.user,
                public_rider_offer_expires_at__gt=timezone.now(),
                status="READY",
                assigned_rider__isnull=True,
                delivery_completed_at__isnull=True,
            )
            .values_list("pk", flat=True)
        )
        for shop_order_id in pending_orders:
            ShopOrder.objects.filter(
                pk=shop_order_id,
                public_rider_offer_user=request.user,
            ).update(
                public_rider_offer_user=None,
                public_rider_offer_expires_at=None,
                public_rider_offer_retry_at=None,
                updated_at=timezone.now(),
            )
            advance_public_rider_offer(shop_order_id)

    messages.success(
        request,
        "You are online and can receive new offers."
        if is_online
        else "You are offline. You will not receive new delivery offers; active deliveries are unchanged.",
    )
    selected_view = request.POST.get("view", "available")
    if selected_view not in {
        "available",
        "active",
        "verified",
        "completed",
        "performance",
        "handover",
    }:
        selected_view = "available"
    return redirect(f"{reverse('rider_dashboard')}?view={selected_view}")


@login_required
@transaction.atomic
def stop_public_rider_batch_offers(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    if request.session.get("active_portal") != "RIDER":
        raise PermissionDenied
    application = PublicRiderApplication.objects.select_for_update().filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).first()
    if not application:
        raise PermissionDenied
    application.public_rider_batch_accepting = False
    application.save(update_fields=[
        "public_rider_batch_accepting",
        "updated_at",
    ])

    pending_offer_ids = list(
        ShopOrder.objects.select_for_update()
        .filter(
            public_rider_offer_user=request.user,
            public_rider_offer_expires_at__gt=timezone.now(),
            status="READY",
            assigned_rider__isnull=True,
            delivery_completed_at__isnull=True,
        )
        .values_list("pk", flat=True)
    )
    for shop_order_id in pending_offer_ids:
        ShopOrder.objects.filter(
            pk=shop_order_id,
            public_rider_offer_user=request.user,
        ).update(
            public_rider_offer_user=None,
            public_rider_offer_expires_at=None,
            public_rider_offer_retry_at=None,
            updated_at=timezone.now(),
        )
        advance_public_rider_offer(shop_order_id)

    messages.success(
        request,
        "New offers stopped. Collect your accepted parcels from the shops and scan each shop's order QR to start delivery.",
    )
    return redirect(f"{reverse('rider_dashboard')}?view=active")


def _settle_order_wallet(order, received_amount=None):
    if not order:
        return Decimal("0.00")

    user = get_user_model().objects.filter(username=order.user).first()
    if not user:
        return Decimal("0.00")

    client = ClientProfile.objects.select_for_update().filter(user=user).first()
    if not client:
        return Decimal("0.00")

    try:
        received = (
            Decimal(str(received_amount))
            if received_amount not in (None, "")
            else order.payment_amount
        )
    except (TypeError, ValueError, InvalidOperation):
        received = order.payment_amount

    previously_received = order.finally_received_amount
    outstanding = max(
        Decimal("0.00"),
        order.final_billing_amount - order.wallet_amount_used - previously_received,
    )
    received = max(Decimal("0.00"), received)
    applied_received_amount = min(received, outstanding)
    change = max(Decimal("0.00"), received - outstanding)
    order.finally_received_amount = previously_received + applied_received_amount
    remaining_due = max(
        Decimal("0.00"),
        order.final_billing_amount
        - order.wallet_amount_used
        - order.finally_received_amount,
    )
    cashback = Decimal("0.00")

    if not remaining_due and order.coupon_code and order.coupon_cashback_amount == 0:
        coupon = Coupon.objects.filter(code__iexact=order.coupon_code).first()
        if coupon:
            if coupon.cashback_type == "PERCENT":
                cashback = order.final_billing_amount * coupon.cashback_value / Decimal("100")
            elif coupon.cashback_type == "FLAT":
                cashback = coupon.cashback_value
            cashback = max(Decimal("0.00"), cashback)

    total_credit = change + cashback
    if total_credit:
        client.wallet_balance += total_credit
        client.save(update_fields=["wallet_balance", "updated_at"])
        if change:
            ClientWalletTransaction.objects.create(
                client=client,
                transaction_type="CREDIT",
                amount=change,
                balance_after=client.wallet_balance,
                reference=order.order_id,
                description="Change returned from rider payment.",
            )
        if cashback:
            ClientWalletTransaction.objects.create(
                client=client,
                transaction_type="CREDIT",
                amount=cashback,
                balance_after=client.wallet_balance,
                reference=order.order_id,
                description="Coupon cashback after completed order.",
            )

    order.coupon_cashback_amount += cashback
    order.payment_amount = remaining_due
    order.payment_status = "PAID" if not remaining_due else "COD"
    order.is_payment_done = not remaining_due
    order.save(update_fields=[
        "finally_received_amount",
        "payment_amount",
        "coupon_cashback_amount",
        "payment_status",
        "is_payment_done",
        "updated_at",
    ])
    return applied_received_amount


@transaction.atomic
def _record_shared_rider_delivery_payment(shop_order):
    if (
        not shop_order.assigned_rider_id
        or shop_order.assigned_rider.shop_id == shop_order.shop_id
    ):
        return None

    existing_payment = SharedRiderDeliveryPayment.objects.filter(
        shop_order=shop_order,
    ).first()
    if existing_payment:
        return existing_payment

    shop_coordinates = _get_shop_order_pickup_location(shop_order)
    customer_coordinates = _get_order_delivery_coordinates(shop_order.order)
    if not shop_coordinates or not customer_coordinates:
        logger.error(
            "Cannot calculate shared-rider pay for shop order %s: "
            "shop/customer coordinates are unavailable.",
            shop_order.pk,
        )
        return None

    distance = distance_km_between_points(
        shop_coordinates["lat"],
        shop_coordinates["lng"],
        customer_coordinates["lat"],
        customer_coordinates["lng"],
    )
    if distance is None or not math.isfinite(distance):
        logger.error(
            "Cannot calculate shared-rider pay for shop order %s: "
            "delivery distance is invalid.",
            shop_order.pk,
        )
        return None

    sharing_settings = _delivery_sharing_settings()
    distance_decimal = Decimal(str(distance)).quantize(
        Decimal("0.001"),
        rounding=ROUND_HALF_UP,
    )
    rate = sharing_settings.shared_rider_rate_per_km
    amount = (
        sharing_settings.shared_rider_base_pay
        + distance_decimal * rate
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    amount = max(Decimal("20.00"), amount)

    paying_wallet, _ = ShopWallet.objects.select_for_update().get_or_create(
        shop=shop_order.shop,
    )
    rider_home_shop = shop_order.assigned_rider.shop
    receiving_wallet, _ = ShopWallet.objects.select_for_update().get_or_create(
        shop=rider_home_shop,
    )
    payment = SharedRiderDeliveryPayment.objects.create(
        shop_order=shop_order,
        paying_shop=shop_order.shop,
        rider_home_shop=rider_home_shop,
        rider=shop_order.assigned_rider,
        delivery_distance_km=distance_decimal,
        rate_per_km=rate,
        amount=amount,
        delivered_at=shop_order.delivery_completed_at or timezone.now(),
    )

    paying_wallet.balance -= amount
    paying_wallet.save(update_fields=["balance", "updated_at"])
    ShopWalletTransaction.objects.create(
        wallet=paying_wallet,
        transaction_type="RIDER_FEE",
        amount=amount,
        balance_after=paying_wallet.balance,
        shop_order=shop_order,
        description=(
            f"Shared rider fee for {shop_order.order.order_id}: "
            f"{distance_decimal} km at ₹{rate}/km."
        ),
    )
    receiving_wallet.balance += amount
    receiving_wallet.save(update_fields=["balance", "updated_at"])
    ShopWalletTransaction.objects.create(
        wallet=receiving_wallet,
        transaction_type="RIDER_INCOME",
        amount=amount,
        balance_after=receiving_wallet.balance,
        shop_order=shop_order,
        description=(
            f"Shared rider income from {shop_order.shop.name} for "
            f"{shop_order.order.order_id}."
        ),
    )
    return payment


@transaction.atomic
def _record_public_rider_delivery_fare(shop_order):
    if (
        not shop_order.assigned_rider_id
        or not shop_order.assigned_rider.is_public_rider
    ):
        return None
    existing_transaction = PublicRiderWalletTransaction.objects.filter(
        shop_order=shop_order,
        transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
    ).first()
    if existing_transaction:
        return existing_transaction

    route_estimate = shop_order.public_rider_route_estimate
    try:
        fare = Decimal(str(route_estimate["estimated_pay"])).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (AttributeError, KeyError, TypeError, InvalidOperation, ValueError):
        logger.error(
            "Cannot credit public rider fare for shop order %s: "
            "accepted route estimate is missing or invalid.",
            shop_order.pk,
        )
        return None
    if not fare.is_finite() or fare < 0 or fare > Decimal("999999999999.99"):
        logger.error(
            "Cannot credit public rider fare for shop order %s: "
            "estimated fare is outside the supported range.",
            shop_order.pk,
        )
        return None

    rider_application = PublicRiderApplication.objects.select_for_update().filter(
        user_id=shop_order.assigned_rider.user_id,
    ).first()
    if not rider_application:
        logger.error(
            "Cannot credit public rider fare for shop order %s: "
            "public rider application is missing.",
            shop_order.pk,
        )
        return None

    rider_wallet, _ = PublicRiderWallet.objects.select_for_update().get_or_create(
        rider=rider_application,
    )
    paying_wallet, _ = ShopWallet.objects.select_for_update().get_or_create(
        shop=shop_order.shop,
    )
    rider_wallet.balance += fare
    rider_wallet.save(update_fields=["balance", "updated_at"])
    paying_wallet.balance -= fare
    paying_wallet.save(update_fields=["balance", "updated_at"])

    PublicRiderWalletTransaction.objects.create(
        wallet=rider_wallet,
        shop_order=shop_order,
        transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
        amount=fare,
        balance_after=rider_wallet.balance,
        description=(
            f"Delivery fare for order {shop_order.order.order_id} "
            f"from {shop_order.shop.name}."
        ),
    )
    ShopWalletTransaction.objects.create(
        wallet=paying_wallet,
        transaction_type="RIDER_FEE",
        amount=fare,
        balance_after=paying_wallet.balance,
        shop_order=shop_order,
        description=(
            f"Public rider fare for order {shop_order.order.order_id}."
        ),
    )
    return PublicRiderWalletTransaction.objects.get(shop_order=shop_order)


@transaction.atomic
def _credit_shop_wallet_for_delivery(shop_order):
    shop_order = ShopOrder.objects.select_for_update().select_related("order", "shop").get(pk=shop_order.pk)
    if (
        shop_order.status == "DELIVERED"
        and shop_order.delivery_completed_at
        and shop_order.assigned_rider_id
    ):
        shop_order = ShopOrder.objects.select_related(
            "order",
            "shop",
            "assigned_rider__shop",
        ).get(pk=shop_order.pk)
        if shop_order.assigned_rider.is_public_rider:
            _record_public_rider_delivery_fare(shop_order)
        else:
            _record_shared_rider_delivery_payment(shop_order)
    if shop_order.order.order_source == Order.SOURCE_POS:
        return
    if (
        shop_order.status != "DELIVERED"
        or not shop_order.delivery_completed_at
        or SiteWalletTransaction.objects.filter(shop_order=shop_order).exists()
    ):
        return
    collection_amount = shop_order.cash_ledger_entries.filter(
        entry_type="COLLECTION",
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    handover_amount = shop_order.cash_ledger_entries.filter(
        entry_type="HANDOVER",
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    waiting_for_cod_handover = (
        shop_order.order.payment_type.upper() == "COD"
        and collection_amount > handover_amount
    )
    product_taxable_total = OrderItem.objects.filter(
        shop_order=shop_order,
    ).aggregate(total=Sum("taxable_amount"))["total"]
    if product_taxable_total is None:
        commission_base = max(Decimal("0.00"), shop_order.total_amount)
    else:
        commission_base = max(Decimal("0.00"), product_taxable_total)
    seller_product_amount = max(
        Decimal("0.00"),
        shop_order.taxable_amount,
    )
    seller_gst_amount = max(
        Decimal("0.00"),
        shop_order.cgst_amount
        + shop_order.sgst_amount
        + shop_order.igst_amount
        - shop_order.delivery_gst_amount,
    )
    seller_proceeds = seller_product_amount + seller_gst_amount
    if (
        not shop_order.seller_gstin
        and shop_order.taxable_amount == 0
        and shop_order.total_amount > 0
    ):
        seller_proceeds = shop_order.total_amount

    if not shop_order.order.is_payment_done or waiting_for_cod_handover:
        if shop_order.order.wallet_amount_used <= 0:
            return
        active_shop_orders = list(
            ShopOrder.objects
            .filter(order=shop_order.order)
            .exclude(status__iexact="CANCELLED")
            .order_by("pk")
        )
        merchandise_total = sum(
            (row.total_amount for row in active_shop_orders),
            Decimal("0.00"),
        )
        if merchandise_total <= 0:
            return
        wallet_funding_ratio = min(
            Decimal("1.00"),
            shop_order.order.wallet_amount_used / merchandise_total,
        )
        cumulative_seller_proceeds = Decimal("0.00")
        previously_allocated = Decimal("0.00")
        expected_wallet_funded_proceeds = Decimal("0.00")
        for sibling in active_shop_orders:
            sibling_proceeds = (
                sibling.taxable_amount
                + sibling.cgst_amount
                + sibling.sgst_amount
                + sibling.igst_amount
                - sibling.delivery_gst_amount
            )
            if (
                not sibling.seller_gstin
                and sibling.taxable_amount == 0
                and sibling.total_amount > 0
            ):
                sibling_proceeds = sibling.total_amount
            cumulative_seller_proceeds += max(Decimal("0.00"), sibling_proceeds)
            allocated_to_date = (
                cumulative_seller_proceeds * wallet_funding_ratio
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if sibling.pk == shop_order.pk:
                expected_wallet_funded_proceeds = (
                    allocated_to_date - previously_allocated
                )
                break
            previously_allocated = allocated_to_date
        wallet_funded_increment = max(
            Decimal("0.00"),
            expected_wallet_funded_proceeds - shop_order.wallet_funded_proceeds,
        )
        if wallet_funded_increment <= 0:
            return

        wallet, _ = ShopWallet.objects.select_for_update().get_or_create(shop=shop_order.shop)
        wallet.balance += wallet_funded_increment
        wallet.save(update_fields=["balance", "updated_at"])
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            transaction_type="CREDIT",
            amount=wallet_funded_increment,
            balance_after=wallet.balance,
            shop_order=shop_order,
            description=(
                f"Wallet-funded proceeds from delivered order "
                f"{shop_order.order.order_id}."
            ),
        )
        shop_order.wallet_funded_proceeds = expected_wallet_funded_proceeds
        shop_order.save(update_fields=["wallet_funded_proceeds", "updated_at"])
        return

    wallet, _ = ShopWallet.objects.select_for_update().get_or_create(shop=shop_order.shop)
    site_wallet, _ = SiteWallet.objects.select_for_update().get_or_create(
        key="SITE",
        defaults={"commission_rate_percent": Decimal("3.00")},
    )
    commission_rate = site_wallet.commission_rate_percent
    commission_amount = (
        commission_base * commission_rate / Decimal("100")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    commission_amount = min(commission_amount, seller_proceeds)

    remaining_seller_proceeds = max(
        Decimal("0.00"),
        seller_proceeds - shop_order.wallet_funded_proceeds,
    )
    if remaining_seller_proceeds:
        wallet.balance += remaining_seller_proceeds
        wallet.save(update_fields=["balance", "updated_at"])
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            transaction_type="CREDIT",
            amount=remaining_seller_proceeds,
            balance_after=wallet.balance,
            shop_order=shop_order,
            description=(
                f"Remaining proceeds from delivered order "
                f"{shop_order.order.order_id}."
            ),
        )
    wallet.balance -= commission_amount
    wallet.save(update_fields=["balance", "updated_at"])
    ShopWalletTransaction.objects.create(
        wallet=wallet,
        transaction_type="COMMISSION",
        amount=commission_amount,
        balance_after=wallet.balance,
        shop_order=shop_order,
        description=(
            f"{commission_rate:.2f}% site commission for "
            f"delivered order {shop_order.order.order_id}."
        ),
    )

    site_wallet.balance += commission_amount
    site_wallet.save(update_fields=["balance", "updated_at"])
    SiteWalletTransaction.objects.create(
        wallet=site_wallet,
        shop_order=shop_order,
        commission_base_amount=commission_base,
        commission_rate_percent=commission_rate,
        amount=commission_amount,
        balance_after=site_wallet.balance,
        description=(
            f"{commission_rate:.2f}% commission from {shop_order.shop.name} "
            f"for order {shop_order.order.order_id}."
        ),
    )


def _process_delivery_returns(
    shop_order,
    order,
    shop_orders,
    order_items,
    post_data,
    *,
    returning_to_shop=False,
):
    quantities = {}
    for item in order_items:
        if item.shop_order_id != shop_order.id:
            quantities[item.id] = item.returned_quantity
            continue
        raw_quantity = post_data.get(
            f"returned_quantity_{item.id}",
            item.returned_quantity,
        )
        try:
            returned_quantity = int(raw_quantity)
        except (TypeError, ValueError):
            raise ValueError(f"Enter a valid returned quantity for {item.product_name}.")
        if returned_quantity < 0 or returned_quantity > item.quantity:
            raise ValueError(
                f"Returned quantity for {item.product_name} must be between 0 and {item.quantity}."
            )
        if returned_quantity < item.returned_quantity:
            raise ValueError(
                f"Recorded returns for {item.product_name} cannot be reduced."
            )
        quantities[item.id] = returned_quantity

    returns_recorded = any(
        quantities[item.id] > item.returned_quantity
        for item in order_items
        if item.shop_order_id == shop_order.id
    )
    if not returns_recorded:
        return Decimal("0.00"), Decimal("0.00")

    delivery_fees = {
        row.shop_id: row.delivery_fee
        for row in shop_orders
        if row.status.upper() != "CANCELLED"
    }
    if returning_to_shop:
        delivery_fees[shop_order.shop_id] = Decimal("0.00")
    shop_snapshots = {
        row.shop_id: _gst_shop_snapshot(row)
        for row in shop_orders
        if row.status.upper() != "CANCELLED"
    }
    gst_items = []
    surviving_by_shop = defaultdict(list)
    previous_gross_subtotal = Decimal("0.00")
    for item in order_items:
        prior_returned = item.returned_quantity
        if item.quantity:
            prior_remaining_total = (
                item.total
                * Decimal(item.quantity - prior_returned)
                / Decimal(item.quantity)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            prior_remaining_total = Decimal("0.00")
        previous_gross_subtotal += prior_remaining_total
        if item.shop_order.status.upper() == "CANCELLED":
            continue
        remaining_quantity = item.quantity - quantities[item.id]
        if item.quantity:
            remaining_total = (
                item.total * Decimal(remaining_quantity) / Decimal(item.quantity)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            remaining_total = Decimal("0.00")
        surviving_by_shop[item.shop_order_id].append(
            (item, remaining_quantity, remaining_total)
        )
        gst_items.append({
            "item": {"line": item, "total": remaining_total},
            "total": remaining_total,
            "shop": shop_snapshots[item.shop_order.shop_id],
            "product": item.product,
            "gst_rate": item.gst_rate,
            "hsn_code": item.hsn_code,
        })

    gross_subtotal = sum(
        (entry["total"] for entry in gst_items),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    coupon_code = order.coupon_code
    coupon_discount = (
        min(
            gross_subtotal,
            (
                order.coupon_discount_amount
                * gross_subtotal
                / previous_gross_subtotal
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        if previous_gross_subtotal and order.coupon_discount_amount
        else Decimal("0.00")
    )
    buyer_state = next(
        (row.buyer_state_code for row in shop_orders if row.buyer_state_code),
        "",
    )
    buyer_gstin = next(
        (row.buyer_gstin for row in shop_orders if row.buyer_gstin),
        "",
    )
    has_gst_seller = any(
        row.seller_gstin for row in shop_orders
        if row.status.upper() != "CANCELLED"
    )
    try:
        gst_breakdown = _build_gst_breakdown(
            gst_items,
            SimpleNamespace(state_code=buyer_state, gstin=buyer_gstin),
            coupon_discount,
            require_state=has_gst_seller,
            delivery_fees=delivery_fees,
        )
    except ValueError as error:
        raise ValueError(str(error))

    discounted_subtotal = max(
        Decimal("0.00"),
        gross_subtotal - coupon_discount,
    )
    payment_type = (order.payment_type or "").upper()
    previous_received = order.finally_received_amount
    previous_wallet_used = order.wallet_amount_used
    wallet_used = min(previous_wallet_used, discounted_subtotal)
    charges = _checkout_charge_totals(
        gross_subtotal,
        coupon_discount,
        wallet_used,
        "RAZORPAY" if payment_type == "RAZORPAY" else "COD",
        gst_breakdown["total_gst"] + gst_breakdown["delivery_total"],
    )
    revised_total = (
        discounted_subtotal
        + charges["platform_charge"]
        + charges["razorpay_fee"]
        + gst_breakdown["total_gst"]
        + gst_breakdown["delivery_total"]
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if payment_type == "WALLET":
        wallet_used = revised_total
    elif payment_type == "RAZORPAY":
        wallet_used = min(previous_wallet_used, revised_total)
    wallet_refund = max(
        Decimal("0.00"),
        previous_wallet_used - wallet_used,
    )
    if payment_type == "RAZORPAY":
        wallet_refund = max(
            wallet_refund,
            previous_received + previous_wallet_used - revised_total,
        )
    cash_refund = Decimal("0.00")
    if payment_type not in {"COD", "WALLET", "RAZORPAY"} and (
        order.is_payment_done or order.finally_received_amount > 0
    ) and revised_total < order.final_billing_amount:
        raise ValueError(
            "This payment method does not support automatic return credits. No return was recorded."
        )

    client = None
    if wallet_refund:
        client = ClientProfile.objects.select_for_update().filter(
            user__username=order.user,
        ).first()
        if not client:
            raise ValueError("The customer's wallet could not be found; the return was not recorded.")

    if wallet_refund:
        client.wallet_balance += wallet_refund
        client.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client,
            transaction_type="CREDIT",
            amount=wallet_refund,
            balance_after=client.wallet_balance,
            reference=order.order_id,
            description=f"Credit for returned items in order {order.order_id}.",
        )

    for item in order_items:
        item.returned_quantity = quantities[item.id]
        item.save(update_fields=["returned_quantity"])

    for gst_item in gst_items:
        line = gst_item["item"]["line"]
        if line.shop_order.status.upper() == "CANCELLED":
            continue
        line.taxable_amount = gst_item["taxable_amount"]
        line.cgst_amount = gst_item["cgst_amount"]
        line.sgst_amount = gst_item["sgst_amount"]
        line.igst_amount = gst_item["igst_amount"]
        line.save(update_fields=[
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
        ])

    for row in shop_orders:
        if row.status.upper() == "CANCELLED":
            continue
        row_lines = surviving_by_shop.get(row.id, [])
        row.total_amount = sum(
            (remaining_total for _item, _quantity, remaining_total in row_lines),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        row.product_count = sum(quantity for _item, quantity, _total in row_lines)
        row.total_weight_kg = sum(
            (
                _weight_in_kilograms(
                    (
                        (item.final_weight or item.estimated_weight or Decimal("0.000"))
                        * Decimal(quantity)
                        / Decimal(item.quantity)
                    ),
                    item.unit or (item.product.unit if item.product_id else ""),
                )
                for item, quantity, _total in row_lines
                if item.is_approximate_weight and item.quantity
            ),
            Decimal("0.000"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        row.items = [
            {
                "product_id": item.product_id,
                "product_shop_id": item.product_shop_id,
                "product_name": item.product_name,
                "product_title": item.product.product_title if item.product_id else "",
                "sku": item.sku,
                "quantity": item.quantity,
                "returned_quantity": quantities[item.id],
                "delivered_quantity": quantity,
                "price": str(item.price),
                "total": str(remaining_total),
            }
            for item, quantity, remaining_total in row_lines
        ]
        tax_row = gst_breakdown["shops"].get(row.shop_id)
        if tax_row:
            row.taxable_amount = tax_row["taxable_amount"]
            row.cgst_amount = tax_row["cgst_amount"]
            row.sgst_amount = tax_row["sgst_amount"]
            row.igst_amount = tax_row["igst_amount"]
            row.delivery_gst_amount = tax_row["delivery_gst_amount"]
            row.buyer_state_code = tax_row["buyer_state_code"]
            row.buyer_gstin = tax_row["buyer_gstin"]
        row.save(update_fields=[
            "total_amount",
            "product_count",
            "total_weight_kg",
            "items",
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "delivery_gst_amount",
            "buyer_state_code",
            "buyer_gstin",
            "updated_at",
        ])

    order.coupon_code = coupon_code
    order.coupon_discount_amount = coupon_discount
    order.platform_charge_amount = charges["platform_charge"]
    order.razorpay_fee_amount = charges["razorpay_fee"]
    order.final_billing_amount = revised_total
    order.wallet_amount_used = wallet_used
    order.gst_amount = gst_breakdown["total_gst"]
    if payment_type == "RAZORPAY":
        order.finally_received_amount = min(
            previous_received,
            max(Decimal("0.00"), revised_total - wallet_used),
        )
    order.payment_amount = max(
        Decimal("0.00"),
        revised_total - wallet_used - order.finally_received_amount,
    )
    if payment_type == "RAZORPAY":
        order.payment_status = "PAID"
        order.is_payment_done = True
    elif payment_type == "WALLET":
        order.payment_status = "PAID"
        order.is_payment_done = True
    elif payment_type == "COD":
        order.payment_status = "COD" if order.payment_amount else "PAID"
        order.is_payment_done = order.payment_amount == 0
    order.save(update_fields=[
        "coupon_code",
        "coupon_discount_amount",
        "platform_charge_amount",
        "razorpay_fee_amount",
        "final_billing_amount",
        "wallet_amount_used",
        "gst_amount",
        "finally_received_amount",
        "payment_amount",
        "payment_status",
        "is_payment_done",
        "updated_at",
    ])
    if cash_refund or wallet_refund:
        shop_order.razorpay_refund_amount += cash_refund
        shop_order.wallet_refund_amount += wallet_refund
        shop_order.save(update_fields=[
            "razorpay_refund_amount",
            "wallet_refund_amount",
            "updated_at",
        ])
    customer_user = get_user_model().objects.filter(
        username=order.user,
    ).first()
    if customer_user:
        refund_total = cash_refund + wallet_refund
        refund_summary = (
            f"₹{refund_total:.2f} was credited to your wallet."
            if refund_total
            else f"Updated amount due: ₹{order.payment_amount:.2f}."
        )
        Notification.objects.create(
            user=customer_user,
            title="Returned items and bill updated",
            message=(
                f"Returned quantities from order {order.order_id} were recorded. "
                f"{refund_summary} Updated total: ₹{revised_total:.2f}."
            ),
            notification_type="ORDER",
            related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
        )
    return cash_refund, wallet_refund


def _finish_rider_delivery(
    shop_order,
    order,
    rider,
    *,
    remark="",
    rating=None,
    latitude="",
    longitude="",
):
    if latitude and longitude:
        try:
            shop_order.rider_last_location = {
                "lat": float(latitude),
                "lng": float(longitude),
            }
        except ValueError:
            pass

    shop_order.delivery_completed_at = timezone.now()
    shop_order.delivery_remark = remark
    shop_order.customer_rating = rating
    shop_order.status = "DELIVERED"
    shop_order.save(update_fields=[
        "delivery_completed_at",
        "delivery_remark",
        "customer_rating",
        "rider_last_location",
        "status",
        "updated_at",
    ])
    _credit_shop_wallet_for_delivery(shop_order)

    if shop_order.order_id:
        order.status = "DELIVERED"
        order.save(update_fields=["status", "updated_at"])

    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="DELIVERED",
        remark=(remark or "Delivery completed by rider."),
    )
    return SharedRiderDeliveryPayment.objects.filter(
        shop_order=shop_order,
    ).first()


@login_required
@transaction.atomic
def complete_delivery(request, shop_order_id):
    if request.method != "POST":
        return redirect("rider_dashboard")
    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "shop",
            "order",
            "assigned_rider__user",
        ),
        id=shop_order_id,
    )
    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    rider = _rider_membership_for_order(request.user, shop_order)
    if not rider:
        raise PermissionDenied
    if (
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id != request.user.id
    ):
        messages.error(request, "This order is assigned to another rider.")
        return redirect("rider_dashboard")
    if (
        rider.shop_id != shop_order.shop_id
        and shop_order.assigned_rider_id != rider.id
    ):
        raise PermissionDenied
    if shop_order.status != "OUT_FOR_DELIVERY":
        messages.error(
            request,
            "Customer delivery can only be completed after all accepted parcels are collected.",
        )
        return redirect("rider_dashboard")

    if request.POST.get("return_to_shop") == "1":
        shop_orders = list(
            ShopOrder.objects.select_for_update()
            .filter(order=order)
            .select_related("shop")
            .order_by("id")
        )
        order_items = list(
            OrderItem.objects.select_for_update()
            .filter(shop_order__order=order)
            .select_related("shop_order__shop", "product")
            .order_by("id")
        )
        full_return_quantities = {
            f"returned_quantity_{item.id}": item.quantity
            for item in order_items
            if item.shop_order_id == shop_order.id
        }
        try:
            _process_delivery_returns(
                shop_order,
                order,
                shop_orders,
                order_items,
                full_return_quantities,
                returning_to_shop=True,
            )
        except ValueError as error:
            messages.error(request, str(error))
            return redirect(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
            )

        shop_order.status = "RETURNED"
        shop_order.delivery_remark = (
            request.POST.get("return_reason", "").strip()
            or "Customer unavailable; full order returned with rider."
        )
        shop_order.save(update_fields=[
            "status",
            "delivery_remark",
            "updated_at",
        ])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="RETURNED",
            remark=shop_order.delivery_remark,
        )

        sibling_statuses = list(
            ShopOrder.objects.filter(order=order).values_list("status", flat=True)
        )
        terminal_statuses = {"DELIVERED", "COMPLETED", "RETURNED", "CANCELLED"}
        if sibling_statuses and all(
            status.upper() in terminal_statuses
            for status in sibling_statuses
        ):
            order.status = (
                "RETURNED"
                if all(status.upper() in {"RETURNED", "CANCELLED"} for status in sibling_statuses)
                else "DELIVERED"
            )
            order.save(update_fields=["status", "updated_at"])

        messages.success(
            request,
            "Full order return recorded. Keep the parcel with you and hand it to the shop; stock will update after the shop confirms receipt.",
        )
        return redirect(
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.id}"
        )

    # Allow completion only if OTP has been verified when shop requires it
    if shop_order.shop.require_customer_otp_verification and not shop_order.customer_otp_verified:
        messages.error(request, "Customer OTP has not been verified.")
        return redirect(
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
        )

    received_amount = request.POST.get("received_amount")
    cod_payment_method = (
        request.POST.get("cod_payment_method") or "CASH"
    ).strip().upper()
    prepare_shop_upi_payment = (
        request.POST.get("prepare_shop_upi_payment") == "1"
    )
    if order.payment_type.upper() == "COD" and cod_payment_method not in {
        "CASH",
        "SHOP_UPI",
        "STAFF_UPI",
    }:
        messages.error(request, "Select a valid COD payment method.")
        return redirect("rider_dashboard")
    if (
        order.payment_type.upper() == "COD"
        and received_amount is not None
        and cod_payment_method != "SHOP_UPI"
    ):
        try:
            parsed_received_amount = Decimal(received_amount.strip())
        except (AttributeError, InvalidOperation, ValueError):
            messages.error(request, "Enter the actual cash received from the customer.")
            return redirect("rider_dashboard")
        if (
            not parsed_received_amount.is_finite()
            or parsed_received_amount < 0
            or parsed_received_amount > Decimal("999999999.99")
            or parsed_received_amount != parsed_received_amount.quantize(Decimal("0.01"))
        ):
            messages.error(request, "Cash received must be a valid amount with at most two decimal places.")
            return redirect("rider_dashboard")
    if order.payment_type.upper() == "COD" and cod_payment_method == "SHOP_UPI":
        if not shop_order.shop.upi_id:
            messages.error(request, "This shop has not configured a UPI ID.")
            return redirect("rider_dashboard")
    if prepare_shop_upi_payment and (
        order.payment_type.upper() != "COD"
        or cod_payment_method != "SHOP_UPI"
    ):
        messages.error(request, "Select the shop UPI payment option to prepare its QR.")
        return redirect("rider_dashboard")
    active_cod_payment_request = (
        CODPaymentApprovalRequest.objects.select_for_update()
        .filter(
            shop_order=shop_order,
            status__in=CODPaymentApprovalRequest.ACTIVE_STATUSES,
        )
        .first()
    )
    if active_cod_payment_request:
        messages.info(
            request,
            "This order already has a shop UPI QR or is waiting for shop approval.",
        )
        return redirect(
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
        )
    if (
        order.payment_type.upper() == "COD"
        and cod_payment_method == "SHOP_UPI"
        and not prepare_shop_upi_payment
    ):
        messages.error(
            request,
            "Prepare the unique shop UPI QR and get shop approval before completing delivery.",
        )
        return redirect(
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
        )
    if prepare_shop_upi_payment and request.POST.get("return_to_shop") == "1":
        messages.error(request, "A returned order cannot start a shop UPI payment.")
        return redirect("rider_dashboard")

    if order.payment_type.upper() == "COD" and cod_payment_method == "STAFF_UPI":
        if (
            not shop_order.assigned_rider_id
            or shop_order.assigned_rider_id != rider.id
            or not rider.upi_id
        ):
            messages.error(
                request,
                "Personal UPI collection is available only for the assigned rider with a configured UPI ID.",
            )
            return redirect("rider_dashboard")

    shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
        .order_by("id")
    )
    order_items = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order__order=order)
        .select_related("shop_order__shop", "product")
        .order_by("id")
    )
    shop_order_items = [
        item for item in order_items
        if item.shop_order_id == shop_order.id
    ]
    if shop_order_items:
        try:
            all_items_returned = all(
                int(
                    request.POST.get(
                        f"returned_quantity_{item.id}",
                        item.returned_quantity,
                    )
                ) == item.quantity
                for item in shop_order_items
            )
        except (TypeError, ValueError):
            all_items_returned = False
        if all_items_returned:
            messages.error(
                request,
                "For a full-order return, use “Return full order to shop” so the order is not marked delivered.",
            )
            return redirect(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
            )
    if prepare_shop_upi_payment:
        original_cod_amount = order.payment_amount
        try:
            with transaction.atomic():
                _process_delivery_returns(
                    shop_order,
                    order,
                    shop_orders,
                    order_items,
                    request.POST,
                )
                settlement_amount = max(
                    Decimal("0.00"),
                    order.payment_amount,
                )
                return_quantities = {
                    str(item.id): item.returned_quantity
                    for item in shop_order_items
                }
                transaction.set_rollback(True)
        except ValueError as error:
            messages.error(request, str(error))
            return redirect("rider_dashboard")

        order.refresh_from_db()
        for item in order_items:
            item.refresh_from_db()
        if settlement_amount <= 0:
            messages.error(
                request,
                "There is no COD balance due after the returns. Complete delivery without a payment QR.",
            )
            return redirect(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
            )
        locked_shop = Shop.objects.select_for_update().get(pk=shop_order.shop_id)
        qr_amount = _next_cod_upi_qr_amount(locked_shop, settlement_amount)
        if qr_amount is None:
            messages.error(
                request,
                "No unique shop UPI amount is available within ₹1 of this balance. Ask shop staff to approve another pending payment first.",
            )
            return redirect(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
            )
        rating_value = request.POST.get("rating")
        try:
            rating = int(rating_value) if rating_value else None
        except (TypeError, ValueError):
            rating = None
        if rating is not None and not 1 <= rating <= 5:
            rating = None
        payment_request = CODPaymentApprovalRequest.objects.create(
            shop_order=shop_order,
            shop=locked_shop,
            rider=rider,
            qr_amount=qr_amount,
            settlement_amount=settlement_amount,
            original_cod_amount=original_cod_amount,
            return_quantities=return_quantities,
            upi_id=locked_shop.upi_id,
            delivery_remark=request.POST.get("remark", "").strip(),
            customer_rating=rating,
        )
        queue_order_update(shop_order)
        messages.success(
            request,
            f"Shop UPI QR prepared for ₹{qr_amount:.2f}. After the customer pays, request shop approval. The order balance after returns is ₹{settlement_amount:.2f}.",
        )
        return redirect(
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
        )

    try:
        _process_delivery_returns(
            shop_order,
            order,
            shop_orders,
            order_items,
            request.POST,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("rider_dashboard")

    # Collect optional fields
    remark = request.POST.get("remark") or ""
    try:
        rating_val = request.POST.get("rating")
        rating = int(rating_val) if rating_val else None
        if rating is not None and (rating < 1 or rating > 5):
            rating = None
    except (TypeError, ValueError):
        rating = None

    cod_cash_collected = _settle_order_wallet(order, received_amount)
    if order.payment_type.upper() == "COD":
        actual_cod_cash_received = (
            parsed_received_amount
            if received_amount is not None
            else cod_cash_collected
        )
        overpayment_refund = max(
            Decimal("0.00"),
            actual_cod_cash_received - cod_cash_collected,
        )
    else:
        actual_cod_cash_received = Decimal("0.00")
        overpayment_refund = Decimal("0.00")

    if order.payment_type.upper() == "COD" and actual_cod_cash_received > 0:
        RiderCashLedgerEntry.objects.create(
            rider=rider,
            shop=shop_order.shop,
            shop_order=shop_order,
            entry_type="COLLECTION",
            amount=actual_cod_cash_received,
            payment_method=cod_payment_method,
            created_by=request.user,
        )
        if cod_payment_method == "STAFF_UPI":
            staff_wallet, _ = ShopStaffWallet.objects.select_for_update().get_or_create(
                staff=rider,
            )
            staff_wallet.balance += actual_cod_cash_received
            staff_wallet.save(update_fields=["balance", "updated_at"])
            ShopStaffWalletTransaction.objects.create(
                wallet=staff_wallet,
                shop_order=shop_order,
                transaction_type="COD_UPI",
                amount=actual_cod_cash_received,
                balance_after=staff_wallet.balance,
                description=(
                    f"COD received to personal UPI for order "
                    f"{order.order_id}."
                ),
            )
    if overpayment_refund > 0:
        shop_wallet, _ = ShopWallet.objects.select_for_update().get_or_create(
            shop=shop_order.shop,
        )
        shop_wallet.balance -= overpayment_refund
        shop_wallet.save(update_fields=["balance", "updated_at"])
        ShopWalletTransaction.objects.create(
            wallet=shop_wallet,
            transaction_type="DEBIT",
            amount=overpayment_refund,
            balance_after=shop_wallet.balance,
            shop_order=shop_order,
            description=(
                f"Customer overpayment refunded to wallet for order "
                f"{order.order_id}."
            ),
        )
    if order.payment_type.upper() == "RAZORPAY" and ShopOrder.objects.filter(
        order=order,
        razorpay_refund_amount__gt=0,
    ).exists():
        order.payment_status = (
            "REFUNDED"
            if order.finally_received_amount == 0
            else "PARTIALLY_REFUNDED"
        )
        order.save(update_fields=["payment_status", "updated_at"])

    shared_rider_payment = _finish_rider_delivery(
        shop_order,
        order,
        rider,
        remark=remark,
        rating=rating,
        latitude=request.POST.get("latitude"),
        longitude=request.POST.get("longitude"),
    )

    if shop_order.assigned_rider_id and shop_order.assigned_rider.shop_id != shop_order.shop_id:
        if shared_rider_payment:
            messages.success(
                request,
                "Delivery completed. "
                f"₹{shared_rider_payment.amount:.2f} shared-rider pay was transferred "
                f"for {shared_rider_payment.delivery_distance_km:.3f} km.",
            )
        else:
            messages.warning(
                request,
                "Delivery completed, but shared-rider pay was not calculated because "
                "the shop or customer delivery coordinates are unavailable. "
                "Ask the shop owner to correct the location and contact support.",
            )
    elif shop_order.assigned_rider_id and shop_order.assigned_rider.is_public_rider:
        public_rider_fare = PublicRiderWalletTransaction.objects.filter(
            shop_order=shop_order,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
        ).first()
        if public_rider_fare:
            messages.success(
                request,
                "Delivery completed. "
                f"₹{public_rider_fare.amount:.2f} rider fare was credited "
                "to your public rider wallet.",
            )
        else:
            messages.warning(
                request,
                "Delivery completed, but rider fare could not be credited "
                "because this order has no valid saved acceptance estimate. "
                "Contact the shop owner to review the order.",
            )
    else:
        messages.success(request, "Delivery completed successfully.")
    return redirect(
        f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.id}"
    )


@login_required
@transaction.atomic
def request_cod_payment_approval(request, payment_request_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    payment_request = get_object_or_404(
        CODPaymentApprovalRequest.objects.select_for_update().select_related(
            "shop_order",
            "shop_order__order",
            "shop",
            "rider",
            "rider__user",
        ),
        pk=payment_request_id,
        rider__user=request.user,
        rider__is_active=True,
        rider__role="RIDER",
    )
    rider = payment_request.rider
    shop_order = ShopOrder.objects.select_for_update().get(
        pk=payment_request.shop_order_id,
    )
    if payment_request.status == "PENDING":
        messages.info(request, "This payment has already been sent to the shop for approval.")
    elif (
        payment_request.status != "QR_READY"
        or shop_order.status != "OUT_FOR_DELIVERY"
        or shop_order.assigned_rider_id != rider.pk
    ):
        messages.error(request, "This shop UPI payment request is no longer active.")
    else:
        payment_request.status = "PENDING"
        payment_request.save(update_fields=["status", "updated_at"])
        queue_order_update(shop_order)
        _notify_cod_payment_reviewers(payment_request)
        messages.success(
            request,
            "Payment confirmation sent to the shop. Keep the order open until shop staff approves it.",
        )
    return redirect(
        f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
    )


@login_required
@transaction.atomic
def remind_cod_payment_reviewers(request, payment_request_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    payment_request = get_object_or_404(
        CODPaymentApprovalRequest.objects.select_for_update().select_related(
            "shop_order",
            "shop",
            "rider",
            "rider__user",
        ),
        pk=payment_request_id,
        rider__user=request.user,
        rider__is_active=True,
        rider__role="RIDER",
        status="PENDING",
    )
    shop_order = ShopOrder.objects.select_for_update().get(
        pk=payment_request.shop_order_id,
    )
    if (
        shop_order.status != "OUT_FOR_DELIVERY"
        or shop_order.assigned_rider_id != payment_request.rider_id
    ):
        raise PermissionDenied

    now = timezone.now()
    if payment_request.last_reminded_at:
        seconds_since_reminder = (
            now - payment_request.last_reminded_at
        ).total_seconds()
        if seconds_since_reminder < 30:
            wait_seconds = max(1, math.ceil(30 - seconds_since_reminder))
            messages.info(
                request,
                f"You can remind the shop again in {wait_seconds} seconds.",
            )
            return redirect(
                f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
            )

    payment_request.last_reminded_at = now
    payment_request.save(update_fields=["last_reminded_at", "updated_at"])
    _notify_cod_payment_reviewers(payment_request, reminder=True)
    messages.success(request, "Payment reminder sent to the shop.")
    return redirect(
        f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
    )


@login_required
@transaction.atomic
def cancel_cod_payment_qr(request, payment_request_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    payment_request = get_object_or_404(
        CODPaymentApprovalRequest.objects.select_for_update().select_related("rider"),
        pk=payment_request_id,
        rider__user=request.user,
        rider__is_active=True,
        rider__role="RIDER",
        status="QR_READY",
    )
    rider = payment_request.rider
    shop_order = ShopOrder.objects.select_for_update().get(
        pk=payment_request.shop_order_id,
    )
    if (
        shop_order.status != "OUT_FOR_DELIVERY"
        or shop_order.assigned_rider_id != rider.pk
    ):
        raise PermissionDenied
    payment_request.status = "CANCELLED"
    payment_request.save(update_fields=["status", "updated_at"])
    queue_order_update(shop_order)
    messages.success(request, "The unused shop UPI QR was cancelled.")
    return redirect(
        f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}"
    )


def _review_cod_payment_request(request, payment_request_id, *, approve):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    payment_request = get_object_or_404(
        CODPaymentApprovalRequest.objects.select_for_update().select_related(
            "shop",
            "shop_order",
            "shop_order__order",
            "rider",
            "rider__user",
        ),
        pk=payment_request_id,
    )
    if not _can_review_cod_payment(request.user, payment_request.shop):
        raise PermissionDenied

    shop_order = ShopOrder.objects.select_for_update().get(
        pk=payment_request.shop_order_id,
    )
    order = Order.objects.select_for_update().get(pk=shop_order.order_id)
    if payment_request.status != "PENDING":
        messages.error(request, "This COD payment is no longer awaiting approval.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    if not approve:
        payment_request.status = "REJECTED"
        payment_request.reviewed_by = request.user
        payment_request.reviewed_at = timezone.now()
        payment_request.save(update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ])
        queue_order_update(shop_order)
        _notify_cod_payment_rider(
            payment_request,
            "Shop rejected COD payment confirmation",
            (
                f"The shop did not approve the ₹{payment_request.qr_amount:.2f} "
                f"UPI payment for order "
                f"{shop_order.shop_order_number or order.order_id}. "
                "Contact the shop before trying again."
            ),
        )
        messages.warning(request, "Payment confirmation rejected; the rider was notified.")
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    if (
        shop_order.status != "OUT_FOR_DELIVERY"
        or shop_order.assigned_rider_id != payment_request.rider_id
        or order.payment_type.upper() != "COD"
        or order.payment_amount != payment_request.original_cod_amount
    ):
        messages.error(
            request,
            "The order or COD balance changed after the QR was created. Reject this request and have the rider prepare a new QR.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    if not ClientProfile.objects.filter(user__username=order.user).exists():
        messages.error(
            request,
            "The customer's wallet profile is unavailable, so the payment cannot be settled safely.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
        .order_by("id")
    )
    order_items = list(
        OrderItem.objects.select_for_update()
        .filter(shop_order__order=order)
        .select_related("shop_order__shop", "product")
        .order_by("id")
    )
    return_quantities = {
        f"returned_quantity_{item_id}": quantity
        for item_id, quantity in payment_request.return_quantities.items()
    }
    try:
        _process_delivery_returns(
            shop_order,
            order,
            shop_orders,
            order_items,
            return_quantities,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)
    if order.payment_amount != payment_request.settlement_amount:
        messages.error(
            request,
            "The order balance changed while applying the returned quantities. Reject this request and have the rider prepare a new QR.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order.pk)

    applied_amount = _settle_order_wallet(order, payment_request.settlement_amount)
    if (
        applied_amount != payment_request.settlement_amount
        or order.payment_amount != Decimal("0.00")
    ):
        raise RuntimeError(
            "Approved shop UPI payment did not reconcile with the COD balance."
        )
    CODBalancePayment.objects.create(
        shop_order=shop_order,
        amount=payment_request.qr_amount,
        received_by=payment_request.rider.user,
    )
    payment_request.status = "APPROVED"
    payment_request.reviewed_by = request.user
    payment_request.reviewed_at = timezone.now()
    payment_request.save(update_fields=[
        "status",
        "reviewed_by",
        "reviewed_at",
        "updated_at",
    ])
    shared_rider_payment = _finish_rider_delivery(
        shop_order,
        order,
        payment_request.rider,
        remark=payment_request.delivery_remark,
        rating=payment_request.customer_rating,
    )
    _notify_cod_payment_rider(
        payment_request,
        "Shop approved COD payment",
        (
            f"The shop approved ₹{payment_request.qr_amount:.2f} for order "
            f"{shop_order.shop_order_number or order.order_id}; the full "
            f"₹{payment_request.settlement_amount:.2f} balance is settled. "
            "Delivery is complete."
        ),
    )
    if shared_rider_payment:
        messages.success(
            request,
            f"Payment approved and delivery completed. Shared-rider pay of ₹{shared_rider_payment.amount:.2f} was transferred.",
        )
    else:
        messages.success(request, "Payment approved and delivery completed.")
    return redirect("shop_order_detail", shop_order_id=shop_order.pk)


@login_required
@transaction.atomic
def approve_cod_payment(request, payment_request_id):
    return _review_cod_payment_request(
        request,
        payment_request_id,
        approve=True,
    )


@login_required
@transaction.atomic
def reject_cod_payment(request, payment_request_id):
    return _review_cod_payment_request(
        request,
        payment_request_id,
        approve=False,
    )


@login_required
@transaction.atomic
def edit_completed_delivery_location(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("order"),
        pk=shop_order_id,
        assigned_rider__user=request.user,
        assigned_rider__is_active=True,
        assigned_rider__role="RIDER",
        delivery_completed_at__isnull=False,
    )
    if shop_order.status not in {"DELIVERED", "COMPLETED"}:
        raise PermissionDenied

    address = request.POST.get("delivery_address", "").strip()
    latitude_value = request.POST.get("latitude", "").strip()
    longitude_value = request.POST.get("longitude", "").strip()
    if not address:
        messages.error(request, "Enter the corrected delivery address.")
        return redirect(
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
        )
    if len(address) > 1000:
        messages.error(request, "The delivery address must be 1,000 characters or fewer.")
        return redirect(
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
        )
    if bool(latitude_value) != bool(longitude_value):
        messages.error(request, "Enter both coordinates, or leave both blank.")
        return redirect(
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
        )

    coordinates = None
    if latitude_value and longitude_value:
        try:
            latitude = float(latitude_value)
            longitude = float(longitude_value)
        except ValueError:
            messages.error(request, "Enter valid delivery coordinates.")
            return redirect(
                f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
            )
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            messages.error(request, "Delivery coordinates are outside the valid range.")
            return redirect(
                f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
            )
        coordinates = f"{latitude:.7f}, {longitude:.7f}"

    shop_order.order.delivery_address = address
    shop_order.order.delivery_location = coordinates or ""
    shop_order.order.save(update_fields=[
        "delivery_address",
        "delivery_location",
        "updated_at",
    ])
    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="DELIVERED",
        remark="Assigned rider corrected the delivery location after completion.",
    )
    existing_payment = SharedRiderDeliveryPayment.objects.filter(
        shop_order=shop_order,
    ).exists()
    shared_rider_payment = _record_shared_rider_delivery_payment(shop_order)
    if existing_payment:
        messages.success(
            request,
            "Delivery location updated for this order. The previously settled shared-rider payment was not changed.",
        )
    elif shared_rider_payment:
        messages.success(
            request,
            "Delivery location updated for this order. "
            f"₹{shared_rider_payment.amount:.2f} shared-rider pay was transferred.",
        )
    else:
        messages.success(
            request,
            "Delivery address and location were updated for this order only.",
        )
    return redirect(
        f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.pk}"
    )


@login_required
@transaction.atomic
def accept_packer_order(request, shop_order_id):

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        id=shop_order_id,
    )

    packer_membership = ShopStaff.objects.filter(
        shop=shop_order.shop,
        user=request.user,
        is_active=True,
        role="PACKER",
    ).first()

    if not packer_membership:
        raise PermissionDenied

    if shop_order.assigned_packer_id and shop_order.assigned_packer_id != packer_membership.id:
        messages.error(request, "This order is already assigned to another packer.")
        return redirect("packer_dashboard")

    shop_order.assigned_packer = packer_membership
    # assign to this packer and change status to PACKING when accepting
    if shop_order.status in ["PLACED", "CONFIRMED"]:
        shop_order.assigned_packer = packer_membership
        shop_order.status = "PACKING"
        shop_order.save(update_fields=["assigned_packer", "status", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PACKING",
            remark="Packer accepted order."
        )
        if shop_order.order:
            shop_order.order.assigned_packer = packer_membership.user.username
            shop_order.order.status = "PACKING"
            shop_order.order.save(update_fields=["assigned_packer", "status", "updated_at"])
    else:
        shop_order.assigned_packer = packer_membership
        shop_order.save(update_fields=["assigned_packer", "updated_at"])

    messages.success(request, "Packing order accepted and assigned to you.")

    return redirect(
        f"{reverse('packer_dashboard')}?view=active#order-{shop_order.id}"
    )

@login_required
@transaction.atomic
def accept_rider_order(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    requested_order = get_object_or_404(
        ShopOrder.objects.select_related("shop", "order"),
        pk=shop_order_id,
    )
    order_shop_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(order_id=requested_order.order_id)
        .select_related("shop", "order")
        .prefetch_related("order_items")
        .order_by("id")
    )
    shop_order = next(
        child for child in order_shop_orders if child.pk == requested_order.pk
    )

    public_rider = None
    if request.session.get("active_portal") == "RIDER":
        public_rider = PublicRiderApplication.objects.select_for_update().filter(
            user=request.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).first()
        if not public_rider:
            raise PermissionDenied
    if public_rider:
        if not uses_public_rider_network(shop_order):
            raise PermissionDenied
        rider_membership = ShopStaff.objects.select_for_update().filter(
            user=request.user,
            shop=shop_order.shop,
        ).first()
        if rider_membership and (
            not rider_membership.is_active
            or rider_membership.role != "RIDER"
        ):
            raise PermissionDenied
        if rider_membership is None:
            rider_membership = ShopStaff.objects.create(
                user=request.user,
                shop=shop_order.shop,
                role="RIDER",
                mobile_no=public_rider.phone,
                is_active=True,
                is_public_rider=True,
            )
    else:
        rider_memberships = ShopStaff.objects.filter(
            user=request.user,
            is_active=True,
            role="RIDER",
            is_public_rider=False,
        ).select_for_update().select_related("shop")
        rider_membership = (
            rider_memberships.filter(shop_id=shop_order.shop_id).first()
            or rider_memberships.first()
        )
    if not rider_membership:
        raise PermissionDenied
    if (
        public_rider
        and not public_rider.is_online
        and not shop_order.assigned_rider_id
    ):
        messages.info(
            request,
            "You are offline. Go online before accepting new delivery offers.",
        )
        return redirect("rider_dashboard")
    if public_rider and request.POST.get("action") == "decline":
        if shop_order.public_rider_offer_user_id != request.user.id:
            raise PermissionDenied
        public_rider.public_rider_offer_cooldown_until = (
            timezone.now()
            + timedelta(seconds=PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS)
        )
        public_rider.save(update_fields=[
            "public_rider_offer_cooldown_until",
            "updated_at",
        ])
        advance_public_rider_offer(
            shop_order.pk,
            declined_user_id=request.user.id,
        )
        messages.info(
            request,
            "You declined this delivery. Another rider can be notified after "
            f"at least {PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS} seconds if one "
            "is eligible. Each offer has its own randomized acceptance timer.",
        )
        return redirect("rider_dashboard")
    if public_rider and (
        shop_order.public_rider_offer_user_id != request.user.id
        or not shop_order.public_rider_offer_expires_at
        or shop_order.public_rider_offer_expires_at <= timezone.now()
    ):
        advance_public_rider_offer(shop_order.pk)
        messages.info(
            request,
            "This delivery offer expired or is no longer assigned to you. "
            "Another public rider can be notified after the "
            f"{PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS}-second transfer gap.",
        )
        return redirect("rider_dashboard")
    if public_rider and not any(
        rider.user_id == request.user.id
        for rider in eligible_public_riders(shop_order)
    ):
        messages.info(
            request,
            "This order is outside your work area or batch limits, or you have already started pickup.",
        )
        return redirect("rider_dashboard")
    public_rider_route_estimate = None
    if public_rider:
        acceptance_location = _public_rider_acceptance_location(
            request,
            rider_membership,
        )
        if not acceptance_location:
            messages.error(
                request,
                "Allow location access and wait for your location to update before accepting, so the pickup distance and rider pay can be recorded.",
            )
            return redirect("rider_dashboard")
        public_rider_route_estimate = _public_rider_route_estimate(
            shop_order,
            acceptance_location,
            _delivery_sharing_settings(),
        )
        if not public_rider_route_estimate or public_rider_route_estimate["estimated_pay"] is None:
            messages.error(
                request,
                "The local road route or rider location is unavailable. Ask the shop or administrator to check the uploaded road network and order locations before accepting.",
            )
            return redirect("rider_dashboard")
        public_rider_route_estimate["rider_location"] = acceptance_location
        rider_membership.last_location = acceptance_location
        rider_membership.save(update_fields=["last_location", "updated_at"])
    if not public_rider:
        if (
            shop_order.public_rider_network_requested
            and not shop_order.public_rider_fallback_to_staff
        ):
            raise PermissionDenied
        if (
            shop_order.shop.rider_delivery_preference != "STAFF"
            and not shop_order.public_rider_fallback_to_staff
            and shop_order.assigned_rider_id != rider_membership.id
        ):
            raise PermissionDenied

    group_shop_ids = (
        set()
        if public_rider
        else set(_shared_rider_group_shop_ids(rider_membership))
    )
    last_accepted_shop = (
        None
        if public_rider
        else _rider_last_accepted_shop(rider_membership)
    )
    shared_rider_radius_km = _shared_rider_assignment_radius_km()
    if not public_rider and not _rider_can_accept_shop_orders(
        rider_membership,
        shop_order.shop,
        origin_shop=last_accepted_shop,
        group_shop_ids=group_shop_ids,
        radius_km=shared_rider_radius_km,
        customer_order=shop_order.order,
    ):
        raise PermissionDenied
    if shop_order.status != "READY":
        messages.error(request, "Only ready-for-pickup orders can be accepted.")
        return redirect("rider_dashboard")
    if shop_order.assigned_rider_id and shop_order.assigned_rider_id != rider_membership.id:
        raise PermissionDenied
    if not public_rider and (
        shop_order.shop_id != rider_membership.shop_id
        and _shop_order_ready_at(shop_order) + SHARED_RIDER_OFFER_DELAY > timezone.now()
    ):
        messages.info(
            request,
            "This shared delivery is offered to its shop's rider first. "
            "Other riders can accept it after one minute.",
        )
        return redirect("rider_dashboard")

    active_assigned_orders = list(
        ShopOrder.objects.select_for_update().filter(
            Q(assigned_rider__user=request.user)
            if public_rider
            else Q(assigned_rider=rider_membership),
            status__in=["READY", "RIDER_ASSIGNED", "PICKED_UP", "OUT_FOR_DELIVERY"],
            delivery_completed_at__isnull=True,
        ).exclude(
            status__in=["DELIVERED", "COMPLETED", "CANCELLED"],
        ).prefetch_related("order_items")
    )
    active_loads = [
        _shop_order_estimated_load(active_order)
        for active_order in active_assigned_orders
    ]
    has_active_public_orders = bool(active_assigned_orders) if public_rider else False
    if public_rider and has_active_public_orders and (
        not public_rider.public_rider_batch_accepting
        or not public_rider.public_rider_batch_started_at
        or any(
            active.status not in {"READY", "RIDER_ASSIGNED"}
            for active in active_assigned_orders
        )
        or public_rider.public_rider_batch_started_at
        + timedelta(minutes=5) <= timezone.now()
    ):
        messages.info(
            request,
            "Your five-minute order collection window is closed. Finish all active deliveries before accepting new offers.",
        )
        return redirect("rider_dashboard")
    current_weight = sum(
        (load["weight_kg"] for load in active_loads),
        Decimal("0.00"),
    )
    current_volume = sum(
        (load["volume_cubic_ft"] for load in active_loads),
        Decimal("0.00"),
    )
    requested_load = _shop_order_estimated_load(shop_order)
    if not _rider_load_fits_capacity(
        rider_membership,
        current_weight + requested_load["weight_kg"],
        current_volume + requested_load["volume_cubic_ft"],
    ):
        messages.error(
            request,
            "This parcel exceeds your remaining load capacity. Update your capacity "
            "in payment settings or finish an active delivery first.",
        )
        return redirect("rider_dashboard")

    projected_weight = current_weight + requested_load["weight_kg"]
    projected_volume = current_volume + requested_load["volume_cubic_ft"]
    accepted_at = timezone.now()
    orders_to_assign = [shop_order]
    accepted_shop_location = _get_shop_order_pickup_location(shop_order)
    if accepted_shop_location and not public_rider:
        for sibling_order in order_shop_orders:
            if (
                sibling_order.pk == shop_order.pk
                or sibling_order.status != "READY"
                or sibling_order.assigned_rider_id is not None
                or sibling_order.delivery_completed_at is not None
            ):
                continue
            distance = _shop_distance_km(shop_order.shop, sibling_order.shop)
            direction_match = _shop_is_toward_customer(
                shop_order.shop,
                sibling_order.shop,
                shop_order.order,
                cross_track_radius_km=shared_rider_radius_km,
            )
            sibling_load = _shop_order_estimated_load(sibling_order)
            if (
                (
                    distance is not None
                    and distance <= shared_rider_radius_km
                    or direction_match
                )
                and _rider_can_accept_shop_orders(
                    rider_membership,
                    sibling_order.shop,
                    origin_shop=shop_order.shop,
                    group_shop_ids=group_shop_ids,
                    radius_km=shared_rider_radius_km,
                    customer_order=shop_order.order,
                )
                and _rider_load_fits_capacity(
                    rider_membership,
                    projected_weight + sibling_load["weight_kg"],
                    projected_volume + sibling_load["volume_cubic_ft"],
                )
            ):
                orders_to_assign.append(sibling_order)
                projected_weight += sibling_load["weight_kg"]
                projected_volume += sibling_load["volume_cubic_ft"]

    for order_to_assign in orders_to_assign:
        order_to_assign.assigned_rider = rider_membership
        order_to_assign.status = "RIDER_ASSIGNED"
        order_to_assign.rider_accepted_at = (
            order_to_assign.rider_accepted_at or accepted_at
        )
        order_to_assign.public_rider_offer_user = None
        order_to_assign.public_rider_offer_expires_at = None
        order_to_assign.public_rider_offer_retry_at = None
        order_to_assign.public_rider_offer_attempted_user_ids = []
        order_to_assign.public_rider_offer_declined_until = {}
        if public_rider and order_to_assign.pk == shop_order.pk:
            order_to_assign.public_rider_route_estimate = {
                "rider_location": public_rider_route_estimate["rider_location"],
                "rider_to_pickup_km": (
                    str(public_rider_route_estimate["rider_to_pickup_km"])
                    if public_rider_route_estimate["rider_to_pickup_km"] is not None
                    else None
                ),
                "pickup_to_delivery_km": (
                    str(public_rider_route_estimate["pickup_to_delivery_km"])
                    if public_rider_route_estimate["pickup_to_delivery_km"] is not None
                    else None
                ),
                "total_distance_km": (
                    str(public_rider_route_estimate["total_distance_km"])
                    if public_rider_route_estimate["total_distance_km"] is not None
                    else None
                ),
                "straight_line_rider_to_pickup_km": (
                    str(
                        public_rider_route_estimate[
                            "straight_line_rider_to_pickup_km"
                        ],
                    )
                    if public_rider_route_estimate[
                        "straight_line_rider_to_pickup_km"
                    ] is not None
                    else None
                ),
                "straight_line_pickup_to_delivery_km": (
                    str(
                        public_rider_route_estimate[
                            "straight_line_pickup_to_delivery_km"
                        ],
                    )
                    if public_rider_route_estimate[
                        "straight_line_pickup_to_delivery_km"
                    ] is not None
                    else None
                ),
                "estimated_pay_distance_km": str(
                    public_rider_route_estimate["estimated_pay_distance_km"],
                ),
                "pickup_distance_included": (
                    public_rider_route_estimate["pickup_distance_included"]
                ),
                "pickup_distance_pay_percentage": str(
                    public_rider_route_estimate[
                        "pickup_distance_pay_percentage"
                    ],
                ),
                "pickup_threshold_bonus_enabled": (
                    public_rider_route_estimate[
                        "pickup_threshold_bonus_enabled"
                    ]
                ),
                "pickup_threshold_km": str(
                    public_rider_route_estimate["pickup_threshold_km"],
                ),
                "pickup_threshold_rate_per_km": str(
                    public_rider_route_estimate[
                        "pickup_threshold_rate_per_km"
                    ],
                ),
                "pickup_threshold_distance_method": (
                    public_rider_route_estimate[
                        "pickup_threshold_distance_method"
                    ]
                ),
                "pickup_threshold_distance_km": (
                    str(
                        public_rider_route_estimate[
                            "pickup_threshold_distance_km"
                        ],
                    )
                    if public_rider_route_estimate[
                        "pickup_threshold_distance_km"
                    ] is not None
                    else None
                ),
                "pickup_threshold_distance_source": (
                    public_rider_route_estimate[
                        "pickup_threshold_distance_source"
                    ]
                ),
                "pickup_threshold_bonus_distance_km": str(
                    public_rider_route_estimate[
                        "pickup_threshold_bonus_distance_km"
                    ],
                ),
                "pickup_threshold_bonus": str(
                    public_rider_route_estimate["pickup_threshold_bonus"],
                ),
                "estimated_pay_before_threshold_bonus": (
                    str(
                        public_rider_route_estimate[
                            "estimated_pay_before_threshold_bonus"
                        ],
                    )
                    if public_rider_route_estimate[
                        "estimated_pay_before_threshold_bonus"
                    ] is not None
                    else None
                ),
                "distance_calculation_method": (
                    public_rider_route_estimate["distance_calculation_method"]
                ),
                "base_fare": str(public_rider_route_estimate["base_fare"]),
                "rate_per_km": str(public_rider_route_estimate["rate_per_km"]),
                "estimated_pay": str(
                    public_rider_route_estimate["estimated_pay"],
                ),
                "item_count": public_rider_route_estimate["item_count"],
                "weight_kg": str(public_rider_route_estimate["weight_kg"]),
                "volume_cubic_ft": str(
                    public_rider_route_estimate["volume_cubic_ft"],
                ),
                "calculated_at": public_rider_route_estimate["calculated_at"],
            }
        if not public_rider:
            order_to_assign.public_rider_network_requested = False
            order_to_assign.public_rider_fallback_to_staff = False
            order_to_assign.public_rider_route_estimate = {}
        order_to_assign.rider_pickup_otp = ""
        order_to_assign.rider_pickup_otp_expires_at = None
        order_to_assign.rider_pickup_otp_attempts = 0
        order_to_assign.save(update_fields=[
            "assigned_rider",
            "status",
            "rider_accepted_at",
            "public_rider_offer_user",
            "public_rider_offer_expires_at",
            "public_rider_offer_retry_at",
            "public_rider_offer_attempted_user_ids",
            "public_rider_offer_declined_until",
            "public_rider_network_requested",
            "public_rider_fallback_to_staff",
            "public_rider_route_estimate",
            "rider_pickup_otp",
            "rider_pickup_otp_expires_at",
            "rider_pickup_otp_attempts",
            "updated_at",
        ])
        OrderStatusHistory.objects.create(
            shop_order=order_to_assign,
            status="RIDER_ASSIGNED",
            remark=(
                "Rider accepted the delivery and will collect the parcel from the shop."
                if order_to_assign.pk == shop_order.pk
                else "Assigned to the rider who accepted a nearby shop delivery for this customer order."
            ),
        )
    if public_rider and not has_active_public_orders:
        public_rider.public_rider_batch_accepting = True
        public_rider.public_rider_batch_started_at = accepted_at
    if public_rider:
        public_rider.public_rider_offer_cooldown_until = (
            accepted_at
            + timedelta(seconds=PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS)
        )
        public_rider.save(update_fields=[
            "public_rider_batch_accepting",
            "public_rider_batch_started_at",
            "public_rider_offer_cooldown_until",
            "updated_at",
        ])
    if shop_order.order:
        shop_order.order.assigned_rider = rider_membership.user.username
        shop_order.order.rider_contact_no = (
            public_rider.phone if public_rider else rider_membership.mobile_no
        )
        shop_order.order.status = "RIDER_ASSIGNED"
        shop_order.order.save(update_fields=[
            "assigned_rider",
            "rider_contact_no",
            "status",
            "updated_at",
        ])

    assigned_sibling_count = len(orders_to_assign) - 1
    if assigned_sibling_count:
        success_message = (
            f"Delivery accepted with {assigned_sibling_count} nearby shop "
            f"pickup(s) from this order assigned to you."
        )
    else:
        success_message = (
            "Order accepted. Collect its parcel from the shop; customer delivery "
            "for this customer order starts once its parcels are collected."
        )
    if public_rider:
        success_message += (
            f" You can receive another public offer in "
            f"{PUBLIC_RIDER_OFFER_COOLDOWN_SECONDS} seconds."
        )
    messages.success(
        request,
        success_message,
    )

    return redirect(
        f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
    )


@login_required
@transaction.atomic
def rider_pickup_order(request, shop_order_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "shop",
            "order",
            "assigned_rider",
        ),
        pk=shop_order_id,
    )
    is_assigned_rider = bool(
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id == request.user.id
        and shop_order.assigned_rider.is_active
    )
    if not user_can_access_shop(request.user, shop_order.shop) and not is_assigned_rider:
        raise PermissionDenied
    can_confirm_pickup = (
        request.user.is_superuser
        or shop_order.shop.owner_id == request.user.pk
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
        ).filter(
            Q(role__in=["MANAGER", "PACKER"])
            | Q(can_change_order_status=True)
        ).exists()
    )
    if not can_confirm_pickup:
        raise PermissionDenied

    otp = _rider_pickup_otp_from_qr_value(
        request.POST.get("pickup_otp"),
    )
    if len(otp) != 6 or not otp.isascii() or not otp.isdigit():
        return _pickup_otp_response(
            request,
            "Enter the rider's six-digit pickup OTP.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

    now = timezone.now()
    if (
        not shop_order.rider_pickup_otp
        or not shop_order.rider_pickup_otp_expires_at
        or shop_order.rider_pickup_otp_expires_at <= now
    ):
        shop_order.rider_pickup_otp = ""
        shop_order.rider_pickup_otp_expires_at = None
        shop_order.rider_pickup_otp_attempts = 0
        shop_order.save(update_fields=[
            "rider_pickup_otp",
            "rider_pickup_otp_expires_at",
            "rider_pickup_otp_attempts",
            "updated_at",
        ])
        return _pickup_otp_response(
            request,
            "The pickup OTP has expired. Ask the rider to refresh their dashboard for a new code.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

    if shop_order.rider_pickup_otp_attempts >= RIDER_PICKUP_OTP_MAX_ATTEMPTS:
        return _pickup_otp_response(
            request,
            "Too many incorrect attempts. Ask the rider to refresh their dashboard for a new OTP.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

    if not hmac.compare_digest(otp, shop_order.rider_pickup_otp):
        shop_order.rider_pickup_otp_attempts += 1
        if shop_order.rider_pickup_otp_attempts >= RIDER_PICKUP_OTP_MAX_ATTEMPTS:
            shop_order.rider_pickup_otp = ""
            shop_order.rider_pickup_otp_expires_at = None
        shop_order.save(update_fields=[
            "rider_pickup_otp",
            "rider_pickup_otp_expires_at",
            "rider_pickup_otp_attempts",
            "updated_at",
        ])
        return _pickup_otp_response(
            request,
            "Incorrect pickup OTP. Check the rider, order, and current code, then try again.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

    if shop_order.status != "RIDER_ASSIGNED":
        return _pickup_otp_response(
            request,
            "This order is no longer awaiting OTP-verified pickup.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

    rider = ShopStaff.objects.select_for_update().filter(
        pk=shop_order.assigned_rider_id,
        is_active=True,
        role="RIDER",
    ).first()
    if not rider:
        return _pickup_otp_response(
            request,
            "The assigned rider is no longer active.",
            success=False,
            fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
    shop_order.rider_pickup_otp = ""
    shop_order.rider_pickup_otp_expires_at = None
    shop_order.rider_pickup_otp_attempts = 0
    shop_order.status = "PICKED_UP"
    rider_confirmed_parcel = shop_order.rider_order_acknowledged
    if rider_confirmed_parcel:
        shop_order.status = "OUT_FOR_DELIVERY"
    shop_order.save(update_fields=[
        "status",
        "rider_pickup_otp",
        "rider_pickup_otp_expires_at",
        "rider_pickup_otp_attempts",
        "updated_at",
    ])
    if rider.is_public_rider:
        application = PublicRiderApplication.objects.select_for_update().filter(
            user=rider.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).first()
        if application:
            application.public_rider_batch_accepting = False
            application.save(update_fields=[
                "public_rider_batch_accepting",
                "updated_at",
            ])
    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="PICKED_UP",
        remark=(
            f"Shop staff {request.user.username} verified rider "
            f"{rider.user.username} and confirmed parcel pickup by OTP."
        ),
    )
    if rider_confirmed_parcel:
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="OUT_FOR_DELIVERY",
            remark=(
                f"Rider {rider.user.username} scanned the shop's order QR "
                "after shop verification; delivery started automatically."
            ),
        )
    if shop_order.order:
        remaining_pickups = ShopOrder.objects.filter(
            order_id=shop_order.order_id,
            status__in=[
                "PLACED",
                "CONFIRMED",
                "PACKING",
                "READY",
                "RIDER_ASSIGNED",
                "PICKED_UP",
            ],
        ).exists()
        shop_order.order.status = (
            "OUT_FOR_DELIVERY"
            if rider_confirmed_parcel and not remaining_pickups
            else "PICKED_UP"
        )
        shop_order.order.save(update_fields=["status", "updated_at"])

    if rider_confirmed_parcel:
        success_message = (
            f"Rider {rider.user.username} and order "
            f"{shop_order.order.order_id} verified. The parcel is out for delivery."
        )
    else:
        success_message = (
            f"Rider {rider.user.username} verified for order "
            f"{shop_order.order.order_id}. Pickup recorded; the rider must scan "
            "the shop's order QR to start delivery."
        )
    return _pickup_otp_response(
        request,
        success_message,
        success=True,
        shop_order=shop_order,
        fallback_url=reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
    )


@login_required
@transaction.atomic
def rider_scan_pickup_qr(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    public_rider_approved = PublicRiderApplication.objects.filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).exists()
    has_active_shop_rider_membership = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        role="RIDER",
        is_public_rider=False,
    ).exists()
    if not public_rider_approved and not has_active_shop_rider_membership:
        raise PermissionDenied

    qr_value = (request.POST.get("order_qr") or "").strip()
    try:
        scanned_url = urlsplit(qr_value)
        if (
            scanned_url.scheme not in {"http", "https"}
            or scanned_url.netloc.lower() != request.get_host().lower()
            or scanned_url.query
            or scanned_url.fragment
        ):
            raise ValueError("QR URL does not belong to this site.")
        order_match = re.fullmatch(r"/shop/orders/([0-9]+)/?", scanned_url.path)
        if not order_match:
            raise ValueError("QR does not point to a shop order.")
        shop_order_id = int(order_match.group(1))
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "message": "This QR does not contain a valid SellerHelper order link."},
            status=400,
        )

    shop_order = ShopOrder.objects.select_for_update().select_related(
        "order",
        "assigned_rider",
        "shop",
    ).filter(pk=shop_order_id).first()
    accepted_by_user = bool(
        shop_order
        and shop_order.rider_accepted_at
        and shop_order.order.assigned_rider == request.user.username
    )
    active_shop_rider = bool(
        shop_order
        and ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            role="RIDER",
            is_active=True,
            is_public_rider=False,
        ).exists()
    )
    rider_owns_order = bool(
        shop_order
        and (
            (
                shop_order.assigned_rider_id
                and shop_order.assigned_rider.user_id == request.user.id
                and shop_order.assigned_rider.role == "RIDER"
                and shop_order.assigned_rider.is_active
                and (
                    not shop_order.assigned_rider.is_public_rider
                    or public_rider_approved
                )
            )
            or (
                accepted_by_user
                and (active_shop_rider or public_rider_approved)
            )
        )
    )
    if not rider_owns_order:
        return JsonResponse(
            {
                "success": False,
                "message": "This order is neither assigned to nor accepted by your rider account.",
            },
            status=403,
        )

    is_active_order = bool(
        shop_order.status in {
            "READY",
            "RIDER_ASSIGNED",
            "PICKED_UP",
            "OUT_FOR_DELIVERY",
        }
        and shop_order.delivery_completed_at is None
    )
    if not is_active_order:
        return JsonResponse(
            {
                "success": False,
                "assigned_to_you": bool(
                    shop_order.assigned_rider_id
                    and shop_order.assigned_rider.user_id == request.user.id
                ),
                "accepted_by_you": bool(
                    shop_order.rider_accepted_at
                    and shop_order.order.assigned_rider == request.user.username
                ),
                "is_active_order": False,
                "order_status": shop_order.status,
                "message": (
                    f"Order {shop_order.shop_order_number or shop_order.order.order_id} "
                    f"belongs to your account, but it is not in your active orders. "
                    f"Current status: {shop_order.status}."
                ),
            },
            status=409,
        )

    assigned_to_rider = bool(
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id == request.user.id
        and shop_order.assigned_rider.role == "RIDER"
        and shop_order.assigned_rider.is_active
        and (
            not shop_order.assigned_rider.is_public_rider
            or public_rider_approved
        )
    )
    accepted_by_rider = bool(
        shop_order.rider_accepted_at
        and shop_order.order.assigned_rider == request.user.username
    )
    rider_confirmation_recorded = not shop_order.rider_order_acknowledged
    if rider_confirmation_recorded:
        shop_order.rider_order_acknowledged = True
        shop_order.save(update_fields=["rider_order_acknowledged", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status=shop_order.status,
            remark=(
                f"Rider {request.user.username} scanned the shop's printed "
                "order QR and confirmed the parcel."
            ),
        )

    delivery_started = False
    if shop_order.status == "PICKED_UP":
        shop_order.status = "OUT_FOR_DELIVERY"
        shop_order.save(update_fields=["status", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="OUT_FOR_DELIVERY",
            remark=(
                f"Rider {request.user.username} scanned the shop's order QR "
                "after shop verification; delivery started automatically."
            ),
        )
        delivery_started = True
        if shop_order.order:
            remaining_pickups = ShopOrder.objects.filter(
                order_id=shop_order.order_id,
                status__in=[
                    "PLACED",
                    "CONFIRMED",
                    "PACKING",
                    "READY",
                    "RIDER_ASSIGNED",
                    "PICKED_UP",
                ],
            ).exists()
            if not remaining_pickups:
                Order.objects.filter(pk=shop_order.order_id).update(
                    status="OUT_FOR_DELIVERY",
                    updated_at=timezone.now(),
                )

    if assigned_to_rider and accepted_by_rider:
        rider_acceptance = "assigned to and accepted by you"
    elif accepted_by_rider:
        rider_acceptance = "accepted by you"
    else:
        rider_acceptance = "assigned to your rider account"
    return JsonResponse({
        "success": True,
        "order_url": reverse(
            "shop_order_detail",
            kwargs={"shop_order_id": shop_order.pk},
        ),
        "message": (
            (
                f"Both checks are complete. Order "
                f"{shop_order.shop_order_number or shop_order.order.order_id} "
                "is now out for delivery."
            )
            if delivery_started
            else (
                f"Rider-side parcel confirmation recorded for order "
                f"{shop_order.shop_order_number or shop_order.order.order_id}. "
                + (
                    "The shop must verify your pickup before delivery starts."
                    if shop_order.status == "RIDER_ASSIGNED"
                    else (
                        "Shop verification is complete; delivery has started."
                        if shop_order.status == "OUT_FOR_DELIVERY"
                        else f"Current status: {shop_order.status}."
                    )
                )
            )
        ),
        "assigned_to_you": assigned_to_rider,
        "accepted_by_you": accepted_by_rider,
        "is_active_order": True,
        "order_status": shop_order.status,
        "rider_confirmation_recorded": True,
        "delivery_started": delivery_started,
    })


@login_required
@transaction.atomic
def rider_start_active_deliveries(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    public_rider = (
        PublicRiderApplication.objects.select_for_update().filter(
            user=request.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).first()
        if request.session.get("active_portal") == "RIDER"
        else None
    )
    if request.session.get("active_portal") == "RIDER" and not public_rider:
        raise PermissionDenied
    rider = ShopStaff.objects.select_for_update().filter(
        user=request.user,
        is_active=True,
        role="RIDER",
        **({"is_public_rider": False} if not public_rider else {}),
    ).first()
    if not rider and not public_rider:
        raise PermissionDenied

    assigned_filter = (
        Q(assigned_rider__user=request.user)
        | Q(
            rider_accepted_at__isnull=False,
            order__assigned_rider=request.user.username,
        )
        if public_rider
        else Q(assigned_rider=rider)
        | Q(
            rider_accepted_at__isnull=False,
            order__assigned_rider=request.user.username,
        )
    )
    rider_pickup_filters = (
        {"rider_order_acknowledged": True}
        if public_rider
        else {}
    )
    collected_orders = list(
        ShopOrder.objects.select_for_update()
        .filter(
            assigned_filter,
            status="PICKED_UP",
            delivery_completed_at__isnull=True,
            **rider_pickup_filters,
        )
        .select_related("order")
        .order_by("created_at", "id")
    )
    if not collected_orders:
        messages.info(
            request,
            "No pickup has both verifications yet. Confirm the pickup with the shop and scan its order QR. Parcels awaiting pickup stay unchanged.",
        )
        return redirect(f"{reverse('rider_dashboard')}?view=active")

    affected_order_ids = {collected_order.order_id for collected_order in collected_orders}
    for collected_order in collected_orders:
        collected_order.status = "OUT_FOR_DELIVERY"
        collected_order.save(update_fields=["status", "updated_at"])
        OrderStatusHistory.objects.create(
            shop_order=collected_order,
            status="OUT_FOR_DELIVERY",
            remark=(
                f"Rider {request.user.username} started delivery for this "
                "picked-up parcel."
            ),
        )

    for order in Order.objects.select_for_update().filter(
        pk__in=affected_order_ids,
    ):
        has_unverified_parcels = ShopOrder.objects.filter(
            order_id=order.pk,
            status__in=[
                "PLACED",
                "CONFIRMED",
                "PACKING",
                "READY",
                "RIDER_ASSIGNED",
                "PICKED_UP",
            ],
        ).exists()
        if not has_unverified_parcels:
            order.status = "OUT_FOR_DELIVERY"
            order.save(update_fields=["status", "updated_at"])
    messages.success(
        request,
        f"Delivery started for all {len(collected_orders)} picked-up parcel"
        f"{'s' if len(collected_orders) != 1 else ''}.",
    )
    return redirect(f"{reverse('rider_dashboard')}?view=active")


@login_required
@transaction.atomic
def shop_order_action(request, shop_order_id, action):
    """Perform a small shop-order action triggered via a scanned QR URL.

    Allowed actions (for shop staff / owners):
    - mark-open: add a history remark that the order was opened via scan.

    Pickup must be confirmed using the rider's current pickup OTP.

    The endpoint requires login. Shop staff permissions are validated before action.
    """
    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related("shop", "order"),
        pk=shop_order_id,
    )

    # Check permissions: owner, superuser, or shop staff with manager/change-status privilege
    if not (
        request.user.is_superuser
        or shop_order.shop.owner_id == request.user.id
        or ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
        ).filter(Q(role="MANAGER") | Q(can_change_order_status=True)).exists()
    ):
        raise PermissionDenied

    if action == "mark-verified":
        messages.error(
            request,
            "Pickup must be verified with the rider's current six-digit OTP.",
        )
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    if action == "mark-open":
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status=shop_order.status,
            remark=(
                f"Order opened by scan by {request.user.username}."
            ),
        )
        messages.success(request, "Order open event recorded.")
        return redirect("shop_order_detail", shop_order_id=shop_order_id)

    messages.error(request, "Unknown action requested.")
    return redirect("shop_order_detail", shop_order_id=shop_order_id)


@login_required
@transaction.atomic
def verify_rider_pickup_otp(request):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    otp = _rider_pickup_otp_from_qr_value(
        request.POST.get("pickup_otp"),
    )
    if len(otp) != 6 or not otp.isascii() or not otp.isdigit():
        return _pickup_otp_response(
            request,
            "Enter the rider's six-digit pickup OTP.",
            success=False,
            fallback_url=reverse("shop_order_list"),
        )

    if request.user.is_superuser:
        verification_shops = Shop.objects.all()
    else:
        verification_shops = Shop.objects.filter(
            Q(owner=request.user)
            | (
                Q(
                    staff_members__user=request.user,
                    staff_members__is_active=True,
                )
                & (
                    Q(staff_members__role__in=["MANAGER", "PACKER"])
                    | Q(staff_members__can_change_order_status=True)
                )
            )
        ).distinct()

    matching_orders_query = ShopOrder.objects.filter(
            shop__in=verification_shops,
            status="RIDER_ASSIGNED",
            rider_pickup_otp=otp,
            rider_pickup_otp_expires_at__gt=timezone.now(),
            rider_pickup_otp_attempts__lt=RIDER_PICKUP_OTP_MAX_ATTEMPTS,
        )
    matching_orders = list(matching_orders_query.values_list("pk", flat=True)[:2])
    if len(matching_orders) != 1:
        return _pickup_otp_response(
            request,
            "No active order matches that rider OTP in your shops. Check the code or ask the rider to refresh their dashboard.",
            success=False,
            fallback_url=reverse("shop_order_list"),
        )

    return rider_pickup_order(request, shop_order_id=matching_orders[0])


@login_required
@transaction.atomic
def rider_cancel_order(request, shop_order_id):
    if request.method != "POST":
        return redirect("rider_dashboard")
    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "shop",
            "order",
            "assigned_rider",
            "assigned_rider__user",
        ),
        id=shop_order_id,
    )

    rider_staff = _rider_membership_for_order(request.user, shop_order)
    if not rider_staff:
        raise PermissionDenied

    if (
        not shop_order.assigned_rider_id
        or shop_order.assigned_rider.user_id != request.user.id
    ):
        messages.error(request, "Only the rider currently assigned to this delivery can release it.")
        return redirect("rider_dashboard")
    if shop_order.status not in {"READY", "RIDER_ASSIGNED"}:
        messages.error(request, "A delivery can only be released before its parcel is picked up.")
        return redirect("rider_dashboard")
    now = timezone.now()

    reason = (request.POST.get("reason") or "").strip()
    latitude = request.POST.get("latitude")
    longitude = request.POST.get("longitude")

    shop_order.rider_last_location = {}
    if latitude and longitude:
        try:
            shop_order.rider_last_location = {"lat": float(latitude), "lng": float(longitude)}
        except ValueError:
            shop_order.rider_last_location = {}
    if reason:
        shop_order.rider_cancel_reason = reason
    shop_order.rider_cancelled_at = now
    shop_order.assigned_rider = None
    shop_order.rider_accepted_at = None
    shop_order.rider_pickup_otp = ""
    shop_order.rider_pickup_otp_expires_at = None
    shop_order.rider_pickup_otp_attempts = 0
    shop_order.status = "READY"
    shop_order.save(update_fields=[
        "assigned_rider",
        "rider_accepted_at",
        "rider_pickup_otp",
        "rider_pickup_otp_expires_at",
        "rider_pickup_otp_attempts",
        "status",
        "rider_last_location",
        "rider_cancel_reason",
        "rider_cancelled_at",
        "updated_at",
    ])
    OrderStatusHistory.objects.create(
        shop_order=shop_order,
        status="READY",
        remark=(
            f"Rider {rider_staff.user.username} released this delivery before pickup"
            + (f": {reason}" if reason else ".")
        ),
    )

    if shop_order.order:
        other_shop_order_statuses = list(
            ShopOrder.objects.filter(order=shop_order.order)
            .exclude(pk=shop_order.pk)
            .values_list("status", flat=True)
        )
        order_can_return_to_ready = all(
            status in {"PLACED", "CONFIRMED", "PACKING", "READY", "CANCELLED"}
            for status in other_shop_order_statuses
        )
        other_assigned_delivery_exists = ShopOrder.objects.filter(
            order=shop_order.order,
            assigned_rider__isnull=False,
        ).exclude(pk=shop_order.pk).exists()
        if order_can_return_to_ready and not other_assigned_delivery_exists:
            shop_order.order.assigned_rider = ""
            shop_order.order.status = "READY"
            shop_order.order.save(update_fields=[
                "assigned_rider",
                "status",
                "updated_at",
            ])

    messages.success(
        request,
        "Delivery released. The order remains active and is available for another rider.",
    )
    return redirect(f"{reverse('rider_dashboard')}?view=available")

@login_required
def shop_staff_toggle(request, shop_id, staff_id):

    shop = get_object_or_404(
        Shop,
        id=shop_id,
        owner=request.user
    )

    staff = get_object_or_404(
        ShopStaff,
        id=staff_id,
        shop=shop
    )

    if request.method == "POST":

        staff.is_active = not staff.is_active

        staff.save(
            update_fields=["is_active"]
        )

        if staff.is_active:

            messages.success(
                request,
                f"{staff.user.username} has been activated."
            )

        else:

            messages.warning(
                request,
                f"{staff.user.username} has been disabled."
            )

    return redirect(
        "shop_staff_list_shop",
        shop_id=shop.id
    )

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect

from .models import Shop
from .utils import user_can_access_shop

@login_required
@require_POST
@transaction.atomic
def shop_staff_remove(request, shop_id, staff_id):
    shop = get_object_or_404(
        Shop,
        pk=shop_id,
        owner=request.user,
    )
    staff = get_object_or_404(
        ShopStaff.objects.select_for_update().select_related("user"),
        pk=staff_id,
        shop=shop,
    )
    if staff.is_public_rider:
        messages.error(
            request,
            "Public rider memberships are managed by the rider network and cannot be removed here.",
        )
        return redirect("shop_staff_list_shop", shop_id=shop.pk)

    has_order_history = ShopOrder.objects.filter(
        Q(assigned_rider=staff) | Q(assigned_packer=staff),
    ).exists()
    has_payment_history = (
        CODPaymentApprovalRequest.objects.filter(rider=staff).exists()
        or RiderCashLedgerEntry.objects.filter(rider=staff).exists()
        or SharedRiderDeliveryPayment.objects.filter(rider=staff).exists()
    )
    staff_wallet = ShopStaffWallet.objects.filter(staff=staff).first()
    has_wallet_history = bool(
        staff_wallet
        and (
            staff_wallet.balance != 0
            or staff_wallet.transactions.exists()
        )
    )
    has_hr_history = (
        StaffDutySession.objects.filter(staff=staff).exists()
        or StaffLeaveApplication.objects.filter(staff=staff).exists()
        or StaffResignationRequest.objects.filter(staff=staff).exists()
        or StaffPayrollRecord.objects.filter(staff=staff).exists()
    )
    if has_order_history or has_payment_history or has_wallet_history or has_hr_history:
        messages.error(
            request,
            "This staff membership has order, payment, attendance, leave, resignation, or payroll history and cannot be deleted without affecting records. Disable the staff member to remove their access instead.",
        )
        return redirect("shop_staff_list_shop", shop_id=shop.pk)

    username = staff.user.username
    staff.delete()
    messages.success(
        request,
        f"{username} was removed from {shop.name}. Their account and access to other shops were not changed.",
    )
    return redirect("shop_staff_list_shop", shop_id=shop.pk)

@login_required
def shop_staff_list(request, shop_id=None):

    if shop_id is not None:

        shop = get_object_or_404(
            Shop,
            id=shop_id,
        )

        is_owner = shop.owner_id == request.user.id
        is_staff_manager = ShopStaff.objects.filter(
            shop=shop,
            user=request.user,
            is_active=True,
            can_manage_staff=True,
        ).exists()

        if not (is_owner or is_staff_manager):
            raise PermissionDenied

        staff_members = (
            ShopStaff.objects
            .filter(shop=shop)
            .select_related("user")
            .order_by("-created_at")
        )

        return render(
            request,
            "shop/shop_staff_list.html",
            {
                "shop": shop,
                "shops": [shop],
                "staff_members": staff_members,
                "single_shop": True,
                "is_owner": is_owner,
            }
        )

    owned_shops = Shop.objects.filter(owner=request.user).order_by("name")
    managed_shops = Shop.objects.filter(
        staff_members__user=request.user,
        staff_members__is_active=True,
        staff_members__can_manage_staff=True,
    ).order_by("name").distinct()

    shops = list(owned_shops) + [shop for shop in managed_shops if shop not in owned_shops]

    return render(
        request,
        "shop/shop_staff_list.html",
        {
            "shop": None,
            "shops": shops,
            "staff_members": [],
            "single_shop": False,
        }
    )

@login_required
@require_POST
def shop_toggle_status(request, shop_id):

    shop = get_object_or_404(
        Shop,
        id=shop_id,
        owner=request.user
    )

    shop.is_shop_open = not shop.is_shop_open
    shop.save(update_fields=["is_shop_open"])

    if shop.is_shop_open:
        messages.success(
            request,
            f"{shop.name} is now OPEN."
        )
    else:
        messages.warning(
            request,
            f"{shop.name} is now CLOSED."
        )

    return redirect(
        "shop_detail",
        pk=shop.id
    )

@login_required
def shop_settings(request, shop_id):

    shop = get_object_or_404(
        Shop,
        id=shop_id
    )

    if not user_can_access_shop(
        request.user,
        shop
    ):
        messages.error(
            request,
            "You are not authorized to access shop settings."
        )
        return redirect("shop_list")

    has_active_shop_rider = ShopStaff.objects.filter(
        shop=shop,
        role="RIDER",
        is_active=True,
        is_public_rider=False,
    ).exists()
    global_public_rider_enabled = public_rider_service_enabled()
    if global_public_rider_enabled and not has_active_shop_rider and (
        not shop.public_rider_network_enabled
        or shop.rider_delivery_preference != "PUBLIC"
    ):
        shop.public_rider_network_enabled = True
        shop.rider_delivery_preference = "PUBLIC"
        shop.save(update_fields=[
            "public_rider_network_enabled",
            "rider_delivery_preference",
            "updated_at",
        ])

    if request.method == "POST":
        try:
            eta_lead_minutes = int(request.POST.get("delivery_eta_reminder_minutes", "3"))
            nearby_radius_meters = int(request.POST.get("rider_nearby_notification_meters", "100"))
            delivery_fee = Decimal(
                request.POST.get("delivery_fee", str(shop.delivery_fee)).strip() or "0"
            )
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Enter valid numbers for delivery fee, ETA lead time, and rider radius.")
            return redirect("shop_settings", shop_id=shop.id)

        if (
            not delivery_fee.is_finite()
            or delivery_fee < 0
            or delivery_fee > Decimal("999999.99")
            or delivery_fee != delivery_fee.quantize(Decimal("0.01"))
        ):
            messages.error(request, "Delivery fee must be between ₹0.00 and ₹999999.99, with at most 2 decimal places.")
            return redirect("shop_settings", shop_id=shop.id)
        if not 0 <= eta_lead_minutes <= 60:
            messages.error(request, "ETA notification lead time must be between 0 and 60 minutes.")
            return redirect("shop_settings", shop_id=shop.id)
        if not 10 <= nearby_radius_meters <= 5000:
            messages.error(request, "Rider nearby radius must be between 10 and 5000 meters.")
            return redirect("shop_settings", shop_id=shop.id)

        public_rider_network_enabled = not has_active_shop_rider or (
            request.POST.get("public_rider_network_enabled") == "on"
        )
        rider_delivery_preference = "PUBLIC" if not has_active_shop_rider else (
            request.POST.get("rider_delivery_preference") or "STAFF"
        ).upper()
        if rider_delivery_preference not in {"STAFF", "PUBLIC"}:
            messages.error(request, "Choose a valid default rider preference.")
            return redirect("shop_settings", shop_id=shop.id)
        if rider_delivery_preference == "PUBLIC" and not public_rider_network_enabled:
            messages.error(
                request,
                "Enable the public rider network before selecting it as the default.",
            )
            return redirect("shop_settings", shop_id=shop.id)

        delivery_tax_form = None
        if shop.gstin:
            delivery_tax_form = ShopDeliveryTaxSettingsForm(
                request.POST,
                gstin=shop.gstin,
            )
            if not delivery_tax_form.is_valid():
                messages.error(
                    request,
                    " ".join(
                        error
                        for errors in delivery_tax_form.errors.values()
                        for error in errors
                    ),
                )
                return redirect("shop_settings", shop_id=shop.id)

        shop.require_customer_otp_verification = "require_customer_otp_verification" in request.POST
        shop.allow_scheduled_delivery = "allow_scheduled_delivery" in request.POST
        shop.delivery_eta_reminder_minutes = eta_lead_minutes
        shop.rider_nearby_notification_meters = nearby_radius_meters
        shop.delivery_fee = delivery_fee
        shop.public_rider_network_enabled = public_rider_network_enabled
        shop.rider_delivery_preference = rider_delivery_preference
        update_fields = [
            "require_customer_otp_verification",
            "allow_scheduled_delivery",
            "delivery_eta_reminder_minutes",
            "rider_nearby_notification_meters",
            "delivery_fee",
            "public_rider_network_enabled",
            "rider_delivery_preference",
            "updated_at",
        ]
        if delivery_tax_form:
            shop.delivery_hsn_code = delivery_tax_form.cleaned_data["delivery_hsn_code"]
            shop.delivery_gst_rate = delivery_tax_form.cleaned_data["delivery_gst_rate"]
            update_fields.extend(["delivery_hsn_code", "delivery_gst_rate"])
        shop.save(update_fields=update_fields)
        for application in PublicRiderApplication.objects.filter(
            status=PublicRiderApplication.STATUS_APPROVED,
        ).iterator():
            sync_public_rider_shop_memberships(application)
        messages.success(request, "Shop delivery settings updated.")
        return redirect("shop_settings", shop_id=shop.id)

    return render(
        request,
        "seller/shop_settings.html",
        {
            "shop": shop,
            "has_active_shop_rider": has_active_shop_rider,
            "public_rider_service_enabled": global_public_rider_enabled,
        }
    )

@login_required
def verify_customer_otp(request, shop_order_id):
    shop_order = get_object_or_404(
        ShopOrder.objects.select_related(
            "shop",
            "order",
            "assigned_rider__user",
            "assigned_rider__shop",
        ),
        id=shop_order_id,
    )
    rider = _rider_membership_for_order(request.user, shop_order)
    if not rider:
        raise PermissionDenied
    if (
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id != request.user.id
    ):
        messages.error(request, "This order is assigned to another rider.")
        return redirect("rider_dashboard")
    if (
        rider.shop_id != shop_order.shop_id
        and shop_order.assigned_rider_id != rider.id
    ):
        raise PermissionDenied
    if shop_order.status != "OUT_FOR_DELIVERY":
        messages.error(
            request,
            "Verify the customer only after all accepted parcels have been collected.",
        )
        return redirect("rider_dashboard")
    customer = None
    if shop_order.order and shop_order.order.user:
        user = get_user_model().objects.filter(username=shop_order.order.user).first()
        if user and hasattr(user, "client_profile"):
            customer = user.client_profile
    otp = (request.POST.get("otp") or "").strip()
    if not shop_order.shop.require_customer_otp_verification:
        valid = True
    else:
        valid = bool(customer and otp and otp == customer.verification_otp)
    if not valid:
        messages.error(
            request,
            "Incorrect customer OTP. Ask the customer to confirm their 4-digit code and try again.",
        )
        return redirect(
            f"{reverse('rider_dashboard')}?view=active"
            f"&otp_error={shop_order.id}#order-{shop_order.id}"
        )
    lat = request.POST.get("latitude")
    lng = request.POST.get("longitude")
    if lat and lng:
        try:
            shop_order.rider_last_location = {"lat": float(lat), "lng": float(lng)}
        except ValueError:
            pass
    shop_order.customer_otp_verified = True
    shop_order.customer_otp_input = otp
    shop_order.save(update_fields=[
        "customer_otp_verified",
        "customer_otp_input",
        "rider_last_location",
        "updated_at",
    ])
    messages.success(
        request,
        "Customer OTP verified. Record any returned items before completing delivery.",
    )
    return redirect(
        f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}"
    )

import json

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ClientProfileForm
from .models import ClientProfile

User = get_user_model()

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import ClientProfile
from .forms import ClientSelfProfileForm

@login_required
def client_profile(request):

    client, created = ClientProfile.objects.get_or_create(
        user=request.user,
        defaults={
            "name": request.user.get_full_name()
                    or request.user.username,

            "contact_no": "",

            "email": request.user.email or "",

            "is_active": True,
        }
    )

    if (
        request.method == "POST"
        and request.POST.get("action") == "change_verification_otp"
    ):
        has_undelivered_orders = ShopOrder.objects.filter(
            order__user=request.user.username,
        ).exclude(
            status__in=["DELIVERED", "RETURNED", "CANCELLED"],
        ).exists()
        if has_undelivered_orders:
            messages.error(
                request,
                "Your permanent verification code cannot be changed while you have an undelivered order.",
            )
        else:
            try:
                client.verification_otp = generate_otp_code()
            except RuntimeError as error:
                messages.error(request, str(error))
            else:
                client.save(update_fields=["verification_otp"])
                messages.success(
                    request,
                    "Your permanent delivery verification code has been changed.",
                )
        return redirect("client_profile")

    if request.method == "POST":

        form = ClientSelfProfileForm(
            request.POST,
            instance=client
        )

        if form.is_valid():

            client = form.save(
                commit=False
            )

            new_email = form.cleaned_data.get(
                "email",
                ""
            )

            if request.user.email != new_email:

                request.user.email = new_email

                request.user.save(
                    update_fields=["email"]
                )

            client.save()

            messages.success(
                request,
                "Your profile has been updated successfully."
            )

            if _is_b2b_customer(request, client):
                return redirect("marketplace")

            return redirect(
                "client_profile"
            )

    else:

        form = ClientSelfProfileForm(
            instance=client
        )

    has_undelivered_orders = ShopOrder.objects.filter(
        order__user=request.user.username,
    ).exclude(
        status__in=["DELIVERED", "RETURNED", "CANCELLED"],
    ).exists()

    return render(
        request,
        "client/client_form.html",
        {
            "form": form,
            "title": "My Profile",
            "client": client,
            "otp_code": client.verification_otp,
            "has_undelivered_orders": has_undelivered_orders,
            "active_role_label": PORTAL_LABELS.get(
                request.session.get(
                    "active_portal",
                    "ADMIN" if request.user.is_superuser else "CUSTOMER",
                ),
                "Customer",
            ),
        }
    )


@login_required
def customer_wallet_history(request):
    client, _created = ClientProfile.objects.get_or_create(
        user=request.user,
        defaults={
            "name": request.user.get_full_name() or request.user.username,
            "email": request.user.email or "",
            "is_active": True,
        },
    )
    wallet_transactions = Paginator(
        client.wallet_transactions.order_by("-created_at", "-pk"),
        25,
    ).get_page(request.GET.get("wallet_page"))
    cash_collections = RiderCashLedgerEntry.objects.filter(
        entry_type="COLLECTION",
    ).select_related("rider__user", "shop")
    shop_orders = (
        ShopOrder.objects
        .select_related("shop")
        .prefetch_related(
            "cod_balance_payments__received_by",
            Prefetch(
                "cash_ledger_entries",
                queryset=cash_collections,
            ),
        )
        .order_by("pk")
    )
    order_history = Paginator(
        Order.objects
        .filter(user=request.user.username)
        .order_by("-created_at", "-pk")
        .prefetch_related(Prefetch("shop_orders", queryset=shop_orders)),
        25,
    ).get_page(request.GET.get("payment_page"))
    return render(
        request,
        "client/wallet_history.html",
        {
            "client": client,
            "wallet_transactions": wallet_transactions,
            "order_history": order_history,
        },
    )


@login_required
def shop_order_instruction_audio(request, instruction_id):
    instruction = get_object_or_404(
        OrderVoiceInstruction.objects.select_related(
            "shop",
            "shop_order",
            "shop_order__shop",
            "shop_order__assigned_rider__user",
        ),
        pk=instruction_id,
        shop_order__isnull=False,
    )
    shop_order = instruction.shop_order
    is_assigned_rider = bool(
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id == request.user.id
        and shop_order.assigned_rider.is_active
    )
    if instruction.recipient == OrderVoiceInstruction.RECIPIENT_SHOP:
        can_listen = user_can_access_shop(request.user, shop_order.shop)
    elif instruction.recipient == OrderVoiceInstruction.RECIPIENT_RIDER:
        can_listen = is_assigned_rider
    else:
        can_listen = False
    if not can_listen:
        raise PermissionDenied
    if not instruction.audio_file:
        raise Http404
    if (
        shop_order.delivery_completed_at
        and timezone.now()
        >= shop_order.delivery_completed_at + timedelta(hours=1)
    ):
        instruction.audio_file.delete(save=False)
        instruction.audio_content_type = ""
        instruction.save(update_fields=["audio_file", "audio_content_type"])
        raise Http404

    response = FileResponse(
        instruction.audio_file.open("rb"),
        content_type=instruction.audio_content_type or "application/octet-stream",
        as_attachment=False,
        filename="order-instruction",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def client_list(request):

    clients = ClientProfile.objects.select_related(
        "user"
    ).order_by("-id")

    return render(
        request,
        "client/client_list.html",
        {
            "clients": clients
        }
    )

@login_required
def client_add(request):

    if not request.user.is_superuser:

        messages.error(
            request,
            "You do not have permission to add clients."
        )

        return redirect("client_list")

    if request.method == "POST":

        form = ClientProfileForm(
            request.POST
        )

        username = request.POST.get(
            "username",
            ""
        ).strip()

        password = request.POST.get(
            "password",
            ""
        )

        if not username:

            form.add_error(
                None,
                "Username is required."
            )

        elif User.objects.filter(
            username=username
        ).exists():

            form.add_error(
                None,
                "Username already exists."
            )

        if form.is_valid():

            user = User.objects.create_user(
                username=username,
                password=password,
                email=form.cleaned_data.get(
                    "email",
                    ""
                )
            )

            client = form.save(
                commit=False
            )

            client.user = user

            client.save()

            messages.success(
                request,
                "Client created successfully."
            )

            return redirect(
                "client_list"
            )

    else:

        form = ClientProfileForm()

    return render(
        request,
        "client/client_form.html",
        {
            "form": form,
            "title": "Add Client",
            "client": None,
        }
    )

@login_required
def client_edit(request, pk):

    client = get_object_or_404(
        ClientProfile,
        pk=pk
    )

    if request.user.is_superuser:

        pass

    elif client.user_id == request.user.id:

        pass

    else:

        messages.error(
            request,
            "You can only edit your own profile."
        )

        return redirect("client_list")

    if request.method == "POST":

        form = ClientProfileForm(
            request.POST,
            instance=client
        )

        if form.is_valid():

            client = form.save(
                commit=False
            )

            client.user.email = (
                form.cleaned_data.get(
                    "email",
                    ""
                )
            )

            client.user.save()

            client.save()

            messages.success(
                request,
                "Profile updated successfully."
            )

            return redirect(
                "client_profile"
            )

    else:

        form = ClientProfileForm(
            instance=client
        )

    return render(
        request,
        "client/client_form.html",
        {
            "form": form,
            "title": "My Profile",
            "client": client,
        }
    )

@login_required
def client_delete(request, pk):

    if not request.user.is_superuser:

        messages.error(
            request,
            "You do not have permission to delete clients."
        )

        return redirect(
            "client_list"
        )

    client = get_object_or_404(
        ClientProfile,
        pk=pk
    )

    if request.method == "POST":

        name = client.name

        client.delete()

        messages.success(
            request,
            f"Client '{name}' deleted successfully."
        )

    return redirect(
        "client_list"
    )

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from .forms import (
    ProductForm,
    ProductShopQuickEditForm,
    ProductShopAssignmentForm,
)
from .models import Product, ProductShop, Shop

@login_required
def product_list(request):
    if request.method == "POST":
        assignment = get_object_or_404(
            ProductShop.objects.select_related("product", "shop"),
            pk=request.POST.get("assignment_id", ""),
            product__created_by=request.user,
            shop__owner=request.user,
        )
        form = ProductShopQuickEditForm(request.POST, instance=assignment)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                f"Saved price and stock for {assignment.product.product_name} "
                f"at {assignment.shop.name}.",
            )
        else:
            errors = " ".join(
                str(error)
                for field_errors in form.errors.values()
                for error in field_errors
            )
            messages.error(
                request,
                f"Could not save price and stock: {errors}",
            )

        redirect_params = {
            "focus_product": assignment.product_id,
            "focus_shop": assignment.shop_id,
        }
        search_query = request.POST.get("q", "").strip()[:100]
        if search_query:
            redirect_params["q"] = search_query
        selected_shop_id = request.POST.get("shop", "").strip()
        if selected_shop_id.isascii() and selected_shop_id.isdigit():
            if Shop.objects.filter(
                pk=selected_shop_id,
                owner=request.user,
            ).exists():
                redirect_params["shop"] = selected_shop_id
        if request.POST.get("sort") == "stock_low":
            redirect_params["sort"] = "stock_low"
        return redirect(
            f"{reverse('product_list')}?{urlencode(redirect_params)}"
        )

    shops = Shop.objects.filter(owner=request.user).order_by("name")
    selected_shop_id = request.GET.get("shop", "").strip()
    selected_shop = (
        shops.filter(pk=selected_shop_id).first()
        if selected_shop_id.isascii() and selected_shop_id.isdigit()
        else None
    )
    search_query = request.GET.get("q", "").strip()[:100]
    stock_sort = request.GET.get("sort") == "stock_low"
    products = (
        Product.objects
        .filter(
            created_by=request.user
        )
        .annotate(
            total_shop_stock=Coalesce(
                Sum(
                    "shop_assignments__stock",
                    filter=Q(shop_assignments__is_active=True),
                ),
                Decimal("0.0000"),
            ),
        )
        .prefetch_related(
            "shop_assignments__shop"
        )
    )
    if selected_shop:
        products = (
            products
            .filter(
                pk__in=ProductShop.objects.filter(
                    shop=selected_shop,
                ).values("product_id"),
            )
            .annotate(
                selected_shop_stock=Coalesce(
                    Sum(
                        "shop_assignments__stock",
                        filter=Q(
                            shop_assignments__shop=selected_shop,
                            shop_assignments__is_active=True,
                        ),
                    ),
                    Decimal("0.0000"),
                ),
            )
        )
    if search_query:
        products = products.filter(
            Q(product_name__icontains=search_query)
            | Q(product_alias_name__icontains=search_query)
            | Q(product_title__icontains=search_query)
            | Q(product_description__icontains=search_query)
            | Q(product_category__icontains=search_query)
            | Q(product_color__icontains=search_query)
            | Q(product_size__icontains=search_query)
            | Q(sku__icontains=search_query)
        )
    if stock_sort:
        stock_order_field = (
            "selected_shop_stock" if selected_shop else "total_shop_stock"
        )
        products = products.order_by(stock_order_field, "product_name")
    else:
        products = products.order_by("-created_at")

    return render(
        request,
        "seller/product_list.html",
        {
            "products": products,
            "shops": shops,
            "selected_shop": selected_shop,
            "product_search_query": search_query,
            "product_stock_sort": stock_sort,
            "focus_product_id": request.GET.get("focus_product", ""),
            "focus_shop_id": request.GET.get("focus_shop", ""),
        }
    )


@login_required
@transaction.atomic
def add_marketplace_products(request):
    shops = Shop.objects.filter(owner=request.user).order_by("name")
    selected_shop_id = request.POST.get(
        "shop",
        request.GET.get("shop", ""),
    ).strip()
    selected_shop = (
        shops.filter(pk=int(selected_shop_id)).first()
        if selected_shop_id.isascii() and selected_shop_id.isdigit()
        else None
    )
    marketplace_shop_ids = [
        shop.pk
        for shop in Shop.objects.filter(
            Q(is_shop_open=True) | Q(allow_scheduled_delivery=True)
        )
        .filter(
            market_category=(
                selected_shop.market_category if selected_shop else ""
            ),
        )
        if is_shop_currently_open(shop) or shop.allow_scheduled_delivery
    ]
    matching_category_names = (
        ProductCategory.objects.filter(
            Q(market_category=selected_shop.market_category)
            | Q(market_category=""),
            is_active=True,
        ).values_list("name", flat=True)
        if selected_shop
        else []
    )
    search_query = request.GET.get("q", "").strip()[:100]

    if request.method == "POST":
        if selected_shop is None:
            messages.error(request, "Select one of your shops.")
            return redirect("add_marketplace_products")
        selected_ids = list(dict.fromkeys(
            value
            for value in request.POST.getlist("source_listing")
            if value.isdigit()
        ))
        if not selected_ids:
            messages.error(request, "Select at least one available marketplace product.")
            return redirect(
                f"{reverse('add_marketplace_products')}?shop={selected_shop.pk}"
            )

        source_listings = list(
            ProductShop.objects
            .select_for_update()
            .select_related("product", "shop")
            .filter(
                pk__in=selected_ids,
                shop_id__in=marketplace_shop_ids,
                product__product_category__in=matching_category_names,
                is_active=True,
                product__is_active=True,
                stock__gt=0,
            )
            .filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .order_by("pk")
        )
        if len(source_listings) != len(selected_ids):
            messages.error(
                request,
                "One or more selected listings are no longer available. Refresh and select products again.",
            )
            return redirect(
                f"{reverse('add_marketplace_products')}?shop={selected_shop.pk}"
            )

        copy_settings, _ = ProductCloneSettings.objects.get_or_create(pk=1)
        allowed_fields = set(copy_settings.allowed_fields or [])
        copied_count = 0
        already_added_count = 0
        with transaction.atomic():
            for source_listing in source_listings:
                source_product = source_listing.product
                product = Product.objects.filter(
                    created_by=request.user,
                    product_category=source_product.product_category,
                    product_name__iexact=source_product.product_name,
                ).first()

                if product is None:
                    source_price = source_listing.effective_price_for_customer(
                        is_b2b=False,
                    )
                    product_values = {
                        field_name: getattr(source_product, field_name)
                        for field_name in allowed_fields
                    }
                    if "image_url" in allowed_fields:
                        product_values["image_url"] = (
                            source_listing.display_image_url
                            or source_product.image_url
                        )
                    product = Product.objects.create(
                        created_by=request.user,
                        product_category=source_product.product_category,
                        product_name=source_product.product_name,
                        price=source_price,
                        **product_values,
                    )
                else:
                    product.is_active = True
                    product.save(update_fields=["is_active", "updated_at"])

                if ProductShop.objects.filter(
                    shop=selected_shop,
                    product=product,
                ).exists():
                    already_added_count += 1
                    continue

                ProductShop.objects.create(
                    product=product,
                    shop=selected_shop,
                    price=source_listing.effective_price_for_customer(
                        is_b2b=False,
                    ),
                    stock=0,
                    is_active=True,
                    pricing_mode="MANUAL",
                )
                copied_count += 1

        if copied_count:
            messages.success(
                request,
                f"Added {copied_count} product(s) to {selected_shop.name} with zero stock. Update stock before making them available to customers.",
            )
        if already_added_count:
            messages.info(
                request,
                f"{already_added_count} selected product(s) were already assigned to {selected_shop.name}.",
            )
        return redirect(
            f"{reverse('product_list')}?focus_shop={selected_shop.pk}"
        )

    listings = ProductShop.objects.none()
    if selected_shop and matching_category_names:
        listings = (
            ProductShop.objects
            .filter(
                shop_id__in=marketplace_shop_ids,
                shop__market_category=selected_shop.market_category,
                product__product_category__in=matching_category_names,
                is_active=True,
                product__is_active=True,
                stock__gt=0,
            )
            .filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .select_related("product", "shop")
            .order_by(
                "product__product_category",
                "product__product_name",
                "shop__name",
            )
        )
    if search_query:
        listings = listings.filter(
            Q(product__product_name__icontains=search_query)
            | Q(product__product_category__icontains=search_query)
            | Q(shop__name__icontains=search_query)
        )

    return render(
        request,
        "seller/add_marketplace_products.html",
        {
            "shops": shops,
            "selected_shop": selected_shop,
            "listings": Paginator(listings, 30).get_page(
                request.GET.get("page"),
            ),
            "search_query": search_query,
        },
    )


@login_required
@transaction.atomic
def product_create(request):

    shops = Shop.objects.filter(
        owner=request.user
    ).order_by("name")

    if request.method == "POST":

        form = ProductForm(
            request.POST,
            request.FILES,
            user=request.user,
        )

        assignment_forms = []

        for shop in shops:

            assignment_forms.append(
                ProductShopAssignmentForm(
                    request.POST,
                    prefix=f"shop_{shop.id}",
                    owner=request.user,
                )
            )

        if form.is_valid():

            all_assignments_valid = all(
                assignment_form.is_valid()
                for assignment_form in assignment_forms
            )

            if all_assignments_valid:

                product = form.save(
                    commit=False
                )

                product.created_by = request.user

                product.save()

                for assignment_form in assignment_forms:

                    data = assignment_form.cleaned_data

                    shop = data["shop"]

                    assigned = data.get(
                        "assigned",
                        False
                    )

                    if shop.owner != request.user:
                        continue

                    if assigned:

                        shop_price = data.get("price")

                        ProductShop.objects.create(
                            product=product,
                            shop=shop,

                            price=shop_price
                            if shop_price is not None
                            else None,

                            b2b_price=data.get("b2b_price"),
                            b2b_unit=data.get("b2b_unit") or "",
                            bulk_price=data.get("bulk_price"),
                            bulk_unit=data.get("bulk_unit") or "",
                            b2b_bulk_price=data.get("b2b_bulk_price"),
                            b2b_bulk_unit=data.get("b2b_bulk_unit") or "",
                            bulk_min_quantity=data.get("bulk_min_quantity") or 1,

                            stock=data.get("stock") or 0,

                            is_active=data.get(
                                "is_active",
                                True
                            ),
                        )

                messages.success(
                    request,
                    "Product created successfully."
                )
                if not product.search_keywords_translated:
                    if dictionary_is_ready():
                        warning = (
                            "No dictionary translation matched this product; original "
                            "keywords and script-search aliases are still searchable."
                        )
                    else:
                        warning = (
                            "The offline dictionary is not built. Original keywords "
                            "and script-search aliases were generated. Run "
                            "`python manage.py build_product_dictionary` to add "
                            "word translations."
                        )
                    messages.warning(request, warning)

                return redirect("product_list")

    else:

        form = ProductForm(user=request.user)

        assignment_forms = []

        for shop in shops:

            assignment_forms.append(
                ProductShopAssignmentForm(
                    prefix=f"shop_{shop.id}",
                    owner=request.user,
                    initial={
                        "shop": shop,
                        "assigned": False,
                        "price": None,
                        "b2b_price": None,
                                        "b2b_unit": "",
                                        "bulk_price": None,
                                        "bulk_unit": "",
                                        "b2b_bulk_price": None,
                                        "b2b_bulk_unit": "",
                                        "bulk_min_quantity": 1,
                                        "stock": 0,
                                        "is_active": True,
                                    },
                )
            )

    return render(
        request,
        "seller/product_form.html",
        {
            "form": form,
            "assignment_forms": assignment_forms,
            "form_title": "New Product",
            "form_action": "create",
            "shops": shops,
        },
    )

@login_required
@transaction.atomic
def product_edit(request, pk):

    product = get_object_or_404(
        Product,
        pk=pk,
        created_by=request.user,
    )

    shops = Shop.objects.filter(
        owner=request.user
    ).order_by("name")

    if request.method == "POST":

        form = ProductForm(
            request.POST,
            request.FILES,
            instance=product,
            user=request.user,
            product_name_readonly=True,
        )

        assignment_forms = []

        for shop in shops:

            try:

                assignment = ProductShop.objects.get(
                    product=product,
                    shop=shop,
                )

                initial = {
                    "shop": shop,
                    "assigned": True,

                    "price": assignment.price,
                    "b2b_price": assignment.b2b_price,
                    "b2b_unit": assignment.b2b_unit,
                    "bulk_price": assignment.bulk_price,
                    "bulk_unit": assignment.bulk_unit,
                    "b2b_bulk_price": assignment.b2b_bulk_price,
                    "b2b_bulk_unit": assignment.b2b_bulk_unit,
                    "bulk_min_quantity": assignment.bulk_min_quantity,

                    "stock": assignment.stock,

                    "is_active": assignment.is_active,
                }

            except ProductShop.DoesNotExist:

                initial = {
                    "shop": shop,
                    "assigned": False,
                    "price": None,
                    "b2b_price": None,
                    "b2b_unit": "",
                    "bulk_price": None,
                    "bulk_unit": "",
                    "b2b_bulk_price": None,
                    "b2b_bulk_unit": "",
                    "bulk_min_quantity": 1,
                    "stock": 0,
                    "is_active": True,
                }

            assignment_forms.append(
                ProductShopAssignmentForm(
                    request.POST,
                    prefix=f"shop_{shop.id}",
                    owner=request.user,
                    initial=initial,
                )
            )

        if form.is_valid():

            all_assignments_valid = all(
                assignment_form.is_valid()
                for assignment_form in assignment_forms
            )

            if all_assignments_valid:

                product = form.save()

                for assignment_form in assignment_forms:

                    data = assignment_form.cleaned_data

                    shop = data["shop"]

                    assigned = data.get(
                        "assigned",
                        False
                    )

                    if shop.owner != request.user:
                        continue

                    if assigned:

                        shop_price = data.get("price")

                        ProductShop.objects.update_or_create(

                            product=product,

                            shop=shop,

                            defaults={

                                "price": (
                                    shop_price
                                    if shop_price is not None
                                    else None
                                ),

                                "b2b_price": data.get("b2b_price"),
                                "b2b_unit": data.get("b2b_unit") or "",
                                "bulk_price": data.get("bulk_price"),
                                "bulk_unit": data.get("bulk_unit") or "",
                                "b2b_bulk_price": data.get("b2b_bulk_price"),
                                "b2b_bulk_unit": data.get("b2b_bulk_unit") or "",
                                "bulk_min_quantity": data.get("bulk_min_quantity") or 1,

                                "stock": (
                                    data.get("stock") or 0
                                ),

                                "is_active": data.get(
                                    "is_active",
                                    True
                                ),
                            },
                        )

                    else:

                        ProductShop.objects.filter(
                            product=product,
                            shop=shop,
                        ).delete()

                messages.success(
                    request,
                    "Product updated successfully."
                )
                if not product.search_keywords_translated:
                    if dictionary_is_ready():
                        warning = (
                            "No dictionary translation matched this product; original "
                            "keywords and script-search aliases are still searchable."
                        )
                    else:
                        warning = (
                            "The offline dictionary is not built. Original keywords "
                            "and script-search aliases were generated. Run "
                            "`python manage.py build_product_dictionary` to add "
                            "word translations."
                        )
                    messages.warning(request, warning)

                return redirect("product_list")

    else:

        form = ProductForm(
            instance=product,
            user=request.user,
            product_name_readonly=True,
        )

        assignment_forms = []

        for shop in shops:

            try:

                assignment = ProductShop.objects.get(
                    product=product,
                    shop=shop,
                )

                initial = {
                    "shop": shop,
                    "assigned": True,
                    "price": assignment.price,
                    "b2b_price": assignment.b2b_price,
                    "b2b_unit": assignment.b2b_unit,
                    "bulk_price": assignment.bulk_price,
                    "bulk_unit": assignment.bulk_unit,
                    "b2b_bulk_price": assignment.b2b_bulk_price,
                    "b2b_bulk_unit": assignment.b2b_bulk_unit,
                    "bulk_min_quantity": assignment.bulk_min_quantity,
                    "stock": assignment.stock,
                    "is_active": assignment.is_active,
                }

            except ProductShop.DoesNotExist:

                initial = {
                    "shop": shop,
                    "assigned": False,
                    "price": None,
                    "b2b_price": None,
                    "b2b_unit": "",
                    "bulk_price": None,
                    "bulk_unit": "",
                    "b2b_bulk_price": None,
                    "b2b_bulk_unit": "",
                    "bulk_min_quantity": 1,
                    "stock": 0,
                    "is_active": True,
                }

            assignment_forms.append(
                ProductShopAssignmentForm(
                    prefix=f"shop_{shop.id}",
                    owner=request.user,
                    initial=initial,
                )
            )

    return render(
        request,
        "seller/product_form.html",
        {
            "form": form,
            "assignment_forms": assignment_forms,
            "form_title": "Edit Product",
            "form_action": "update",
            "product": product,
            "shops": shops,
        },
    )

@login_required
def shop_storefront(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    is_owner = shop.owner_id == request.user.id
    staff = ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        is_active=True,
    ).first()
    if not is_owner and not staff:
        raise PermissionDenied
    can_edit_products = bool(
        is_owner
        or (
            staff
            and staff.role == "MANAGER"
            and staff.can_manage_products
        )
    )
    assignments = (
        ProductShop.objects
        .filter(shop=shop)
        .select_related("product")
        .order_by("product__product_name")
    )
    for assignment in assignments:
        assignment.storefront_total_stock = (
            assignment.stock + assignment.b2b_stock
        )
    return render(
        request,
        "seller/shop_storefront.html",
        {
            "shop": shop,
            "product_assignments": assignments,
            "can_edit_products": can_edit_products,
        },
    )


def _shop_rider_map_payload(shop):
    latest_by_rider = {}
    active_orders = (
        ShopOrder.objects
        .filter(
            shop=shop,
            assigned_rider__isnull=False,
            assigned_rider__is_active=True,
            assigned_rider__role="RIDER",
            status="OUT_FOR_DELIVERY",
            delivery_completed_at__isnull=True,
        )
        .select_related("assigned_rider__user", "order")
        .order_by("-updated_at")
    )
    for shop_order in active_orders:
        rider = shop_order.assigned_rider
        if rider.id in latest_by_rider:
            continue
        location = shop_order.rider_last_location
        if not _valid_route_coordinates(location):
            continue
        latest_by_rider[rider.id] = {
            "rider_id": rider.id,
            "rider_name": rider.user.get_full_name() or rider.user.username,
            "order_id": shop_order.order.order_id,
            "location": {
                "lat": float(location["lat"]),
                "lng": float(location["lng"]),
            },
            "updated_at": shop_order.updated_at.isoformat(),
        }
    return list(latest_by_rider.values())


@login_required
def shop_rider_map(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    is_owner = shop.owner_id == request.user.id
    is_manager = ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        role="MANAGER",
        is_active=True,
    ).exists()
    if not is_owner and not is_manager:
        raise PermissionDenied

    if request.GET.get("format") == "json":
        return JsonResponse({"riders": _shop_rider_map_payload(shop)})
    return render(
        request,
        "seller/shop_rider_map.html",
        {"shop": shop, "riders": _shop_rider_map_payload(shop)},
    )


@login_required
@transaction.atomic
def storefront_product_edit(request, product_shop_id):
    product_shop = get_object_or_404(
        ProductShop.objects.select_for_update().select_related("shop", "product"),
        pk=product_shop_id,
    )
    is_owner = product_shop.shop.owner_id == request.user.id
    can_manage_products = is_owner or ShopStaff.objects.filter(
        shop=product_shop.shop,
        user=request.user,
        role="MANAGER",
        can_manage_products=True,
        is_active=True,
    ).exists()
    if not can_manage_products:
        raise PermissionDenied

    form = StorefrontProductEditForm(
        request.POST or None,
        instance=product_shop.product,
        product_shop=product_shop,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        old_stock = product_shop.stock
        old_b2b_stock = product_shop.b2b_stock
        product_shop.price = form.cleaned_data["shop_price"]
        product_shop.b2b_price = form.cleaned_data["b2b_price"]
        product_shop.b2b_unit = form.cleaned_data["b2b_unit"]
        product_shop.bulk_price = form.cleaned_data["bulk_price"]
        product_shop.bulk_unit = form.cleaned_data["bulk_unit"]
        product_shop.b2b_bulk_price = form.cleaned_data["b2b_bulk_price"]
        product_shop.b2b_bulk_unit = form.cleaned_data["b2b_bulk_unit"]
        product_shop.bulk_min_quantity = form.cleaned_data["bulk_min_quantity"] or 1
        product_shop.stock = form.cleaned_data["stock"]
        submitted_b2b_stock = form.cleaned_data["b2b_stock"]
        product_shop.b2b_stock = (
            submitted_b2b_stock
            if submitted_b2b_stock is not None
            else old_b2b_stock
        )
        update_fields = [
            "price",
            "b2b_price",
            "b2b_unit",
            "bulk_price",
            "bulk_unit",
            "b2b_bulk_price",
            "b2b_bulk_unit",
            "bulk_min_quantity",
            "stock",
            "b2b_stock",
        ]
        if product_shop.shop.allow_product_image_overrides:
            update_fields.extend(
                (
                    "image_url_override",
                    "image_url_override_2",
                    "image_url_override_3",
                    "image_url_override_4",
                    "image_url_override_5",
                )
            )
        product_shop.save(update_fields=update_fields)
        stock_change = product_shop.stock - old_stock
        if stock_change:
            StockMovement.objects.create(
                product_shop=product_shop,
                movement_type="ADJUSTMENT",
                quantity=stock_change,
                reference=f"Storefront stock edit for {product_shop.product.product_name}",
                created_by=request.user,
            )
        b2b_stock_change = product_shop.b2b_stock - old_b2b_stock
        if b2b_stock_change:
            StockMovement.objects.create(
                product_shop=product_shop,
                movement_type="ADJUSTMENT",
                quantity=b2b_stock_change,
                reference=f"Storefront B2B stock edit for {product_shop.product.product_name}",
                created_by=request.user,
            )
        messages.success(request, "Storefront product details updated.")
        return redirect("shop_storefront", shop_id=product_shop.shop_id)
    return render(
        request,
        "seller/storefront_product_edit.html",
        {
            "form": form,
            "product_shop": product_shop,
        },
    )

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from .utils import *

from .models import (
    ClientProfile,
    ClientDeliveryAddress,
    Shop,
    ProductShop,
)

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.db.models import Q

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.db.models import Q

from .models import (
    ClientProfile,
    ClientDeliveryAddress,
    Shop,
    ProductShop,
)

from .utils import (
    shop_delivers_to_location,
    get_address_pincode,
    shop_delivers_to_pincode,
)

@login_required
def market_product_detail(request, product_shop_id):

    item = get_object_or_404(
        ProductShop.objects.select_related(
            "product",
            "shop"
        ).filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        ),
        id=product_shop_id,
        is_active=True,
        product__is_active=True,
    )

    product = item.product

    name = product.product_name or "Product"

    title = (
        product.product_title
        or name
    )

    description = (
        product.product_description
        or ""
    )

    sku = product.sku or ""

    color = product.product_color or ""

    size = product.product_size or ""

    unit = product.unit or ""

    image_url = item.display_image_url

    if not description:

        generated_parts = []

        generated_parts.append(
            f"{name} is available from {item.shop.name}."
        )

        description = " ".join(generated_parts)

        title_parts = [name]
        if color:
            title_parts.append(color)

        if size:
            title_parts.append(size)

        title = " ".join(title_parts)

    cart, _ = _address_scoped_cart(request)
    is_b2b = _is_b2b_customer(request)

    quantity = _cart_quantity(cart, item.id)
    bulk_quantity = _cart_quantity(cart, item.id, is_bulk_order=True)
    bulk_price = item.effective_bulk_price_for_customer(is_b2b=is_b2b)

    return JsonResponse(
        {
            "id": item.id,
            "name": name,
            "title": title,
            "description": description,
            "sku": sku,
            "color": color,
            "size": size,
            "unit": unit,
            "price": str(item.effective_price_for_customer(is_b2b=is_b2b)),
            "unit": item.unit_for_customer(is_b2b=is_b2b),
            "bulk_price": str(bulk_price) if bulk_price is not None else "",
            "bulk_unit": item.bulk_unit_for_customer(is_b2b=is_b2b),
            "bulk_min_quantity": item.bulk_min_quantity,
            "bulk_available": bulk_price is not None and bool(item.bulk_unit_for_customer(is_b2b=is_b2b)),
            "bulk_quantity": bulk_quantity,
            "stock": _json_stock_quantity(
                item.stock_for_customer(is_b2b=is_b2b)
            ),
            "has_b2b_price": bool(
                is_b2b and item.shop.has_valid_gstin and item.b2b_price is not None
            ),
            "has_b2b_bulk_offer": bool(
                is_b2b
                and item.shop.has_valid_gstin
                and item.b2b_bulk_price is not None
                and item.b2b_bulk_unit
            ),
            "image_url": image_url,
            "image_urls": item.display_image_urls,
            "shop": item.shop.name,
            "quantity": quantity,
        }
    )

from datetime import datetime, time
from django.utils import timezone

def is_shop_currently_open(shop, current_time=None):

    if not shop.is_shop_open:
        return False

    if current_time is None:
        current_time = timezone.localtime().time()

    if not shop.shop_open_time or not shop.shop_close_time:
        return True

    open_time = shop.shop_open_time
    close_time = shop.shop_close_time
    if shop.name=='vision':
        print(f"Shop: {shop.name}, Open: {open_time}, Close: {close_time}, Current: {current_time}")

    if open_time <= close_time:
        if not (open_time <= current_time <= close_time):
            if shop.name=="vision":
                print(f"Shop: {shop.name}, Open: {open_time}, Close: {close_time}, Current: {current_time} - Closed")
            return False

    else:

        if not (
            current_time >= open_time
            or current_time <= close_time
        ):
            return False

    break_list = shop.shop_break_time_list or []

    for break_time in break_list:

        try:
            break_start = datetime.strptime(
                break_time["start"],
                "%H:%M"
            ).time()

            break_end = datetime.strptime(
                break_time["end"],
                "%H:%M"
            ).time()

        except (KeyError, ValueError, TypeError):
            continue

        if break_start <= break_end:

            if break_start <= current_time <= break_end:
                return False

        else:

            if (
                current_time >= break_start
                or current_time <= break_end
            ):
                return False

    return True

@login_required
def notifications(request):
    all_notifications = Notification.objects.filter(user=request.user)
    show_all = request.GET.get("view") == "all"
    items = all_notifications.order_by("-created_at")
    if not show_all:
        items = items.filter(is_read=False)
    unread_count = all_notifications.filter(is_read=False).count()
    return render(
        request,
        "seller/notifications.html",
        {
            "notifications": items,
            "unread_count": unread_count,
            "show_all": show_all,
        },
    )


@login_required
def notifications_api(request):
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    if request.GET.get("view") != "all":
        notifications = notifications.filter(is_read=False)
    items = notifications[:20]
    public_offer_count = 0
    available_delivery_count = 0
    active_delivery_count = 0
    if request.GET.get("notifications_only") != "1":
        public_rider = None
        if request.session.get("active_portal") == "RIDER":
            application = PublicRiderApplication.objects.filter(
                user=request.user,
                status=PublicRiderApplication.STATUS_APPROVED,
            ).first()
            if application:
                public_rider = application
        rider_membership = ShopStaff.objects.filter(
            user=request.user,
            is_active=True,
            role="RIDER",
            is_public_rider=bool(public_rider),
        ).select_related("shop").order_by("shop_id", "pk").first()
        active_order_rows = []
        if rider_membership or public_rider:
            active_order_rows = list(
                ShopOrder.objects.filter(
                    Q(assigned_rider__user=request.user)
                    | Q(
                        rider_accepted_at__isnull=False,
                        order__assigned_rider=request.user.username,
                    ),
                    status__in=[
                        "READY",
                        "RIDER_ASSIGNED",
                        "PICKED_UP",
                        "OUT_FOR_DELIVERY",
                    ],
                    delivery_completed_at__isnull=True,
                )
                .exclude(status__in=["DELIVERED", "COMPLETED", "CANCELLED"])
                .prefetch_related("order_items"),
            )
        active_delivery_count = len(active_order_rows)
        if rider_membership and (not public_rider or public_rider.is_online):
            active_loads = [
                _shop_order_estimated_load(shop_order)
                for shop_order in active_order_rows
            ]
            rider_load_weight = sum(
                (load["weight_kg"] for load in active_loads),
                Decimal("0.00"),
            )
            rider_load_volume = sum(
                (load["volume_cubic_ft"] for load in active_loads),
                Decimal("0.00"),
            )
            available_ids, _, _ = _rider_available_order_ids(
                rider_membership,
                public_rider,
                request.user,
                rider_load_weight,
                rider_load_volume,
            )
            available_delivery_count = len(available_ids)
            if public_rider:
                public_offer_count = available_delivery_count
    payload = {
        "count": notifications.count(),
        "unread_count": notifications.filter(is_read=False).count(),
        "public_offer_count": public_offer_count,
        "available_delivery_count": available_delivery_count,
        "active_delivery_count": active_delivery_count,
        "items": [
            {
                "id": notification.id,
                "title": notification.title,
                "message": notification.message,
                "type": notification.notification_type,
                "url": notification.related_url,
                "read_url": reverse(
                    "mark_notification_read",
                    kwargs={"notification_id": notification.id},
                ),
                "read": notification.is_read,
                "created_at": notification.created_at.isoformat(),
            }
            for notification in items
        ],
    }
    return JsonResponse(payload)


@login_required
def register_notification_device_token(request):
    if request.method == "GET":
        return JsonResponse({"csrf_token": get_token(request)})
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Method not allowed."},
            status=405,
        )

    token = request.POST.get("token", "").strip()
    platform = request.POST.get("platform", "").strip().lower()
    if request.POST.get("action") == "unregister":
        updated_count = NotificationDeviceToken.objects.filter(
            user=request.user,
            token=token,
        ).update(is_active=False)
        return JsonResponse({"success": updated_count > 0})
    if (
        len(token) > 255
        or not token.endswith("]")
        or not token.startswith(("ExpoPushToken[", "ExponentPushToken["))
    ):
        return JsonResponse(
            {"success": False, "message": "A valid Expo push token is required."},
            status=400,
        )
    if platform not in {"android", "ios"}:
        return JsonResponse(
            {"success": False, "message": "Platform must be android or ios."},
            status=400,
        )

    NotificationDeviceToken.objects.update_or_create(
        token=token,
        defaults={
            "user": request.user,
            "platform": platform,
            "is_active": True,
        },
    )
    return JsonResponse({"success": True})


@login_required
def mark_all_notifications_read(request):
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Method not allowed."},
            status=405,
        )
    updated_count = Notification.objects.filter(
        user=request.user,
        is_read=False,
    ).update(is_read=True, updated_at=timezone.now())
    if request.headers.get("accept", "").startswith("application/json"):
        return JsonResponse({"success": True, "updated_count": updated_count})
    messages.success(request, f"Marked {updated_count} notifications as read.")
    return redirect("notifications")


@login_required
def mark_notification_read(request, notification_id):
    if request.method not in {"GET", "POST"}:
        return JsonResponse({"success": False, "message": "Method not allowed."}, status=405)
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save(update_fields=["is_read", "updated_at"])

    if (
        request.method == "POST"
        or request.headers.get("accept", "").startswith("application/json")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    ):
        return JsonResponse({"success": True, "notification_id": notification.id, "read": True})

    if notification.related_url and url_has_allowed_host_and_scheme(
        notification.related_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(notification.related_url)
    return redirect("notifications")

from rapidfuzz.fuzz import ratio

def marketplace(request):
    is_theme_preview = (
        request.GET.get("theme_preview") == "1"
        and request.user.is_superuser
    )
    if not is_theme_preview and request.session.get("active_portal") == "ADMIN":
        return redirect(
            "admin_catalog_dashboard" if request.user.is_superuser else "home"
        )
    if not is_theme_preview and request.session.get("active_portal") == "SHOP_STAFF":
        return redirect("dashboard")
    if not is_theme_preview and request.session.get("active_portal") == "SHOP_OWNER":
        return redirect("home")

    client = (
        ClientProfile.objects.filter(user=request.user).first()
        if request.user.is_authenticated
        else None
    )
    is_b2b = _is_b2b_customer(request, client)
    marketplace_layout_settings = MarketplaceLayoutSettings.objects.first()
    marketplace_theme = SiteThemeSettings.objects.first()
    marketplace_section_names = (
        "hero",
        "search",
        "nearby",
        "category_nav",
        "products",
    )
    marketplace_section_order = (
        marketplace_theme.marketplace_section_order.split(",")
        if marketplace_theme
        else list(marketplace_section_names)
    )
    if (
        len(marketplace_section_order) != len(marketplace_section_names)
        or set(marketplace_section_order) != set(marketplace_section_names)
    ):
        marketplace_section_order = list(marketplace_section_names)
    marketplace_theme_order = {
        name: marketplace_section_order.index(name) + 1
        for name in marketplace_section_names
    }
    category_order = (
        marketplace_theme.marketplace_category_order.split(",")
        if marketplace_theme and marketplace_theme.marketplace_category_order
        else []
    )
    category_order_index = {
        name: index for index, name in enumerate(category_order)
    }
    featured_products = {}
    featured_catalog_ids = (
        marketplace_theme.marketplace_featured_product_ids
        if marketplace_theme else []
    )
    if featured_catalog_ids:
        featured_rank = {
            int(product_id): index
            for index, product_id in enumerate(featured_catalog_ids)
        }
        featured_catalog_products = AdminProductCatalog.objects.filter(
            pk__in=featured_rank,
            is_active=True,
        )
        for catalog_product in featured_catalog_products:
            featured_products[
                (catalog_product.product_category, catalog_product.product_name)
            ] = featured_rank[catalog_product.pk]

    address_id = request.GET.get("address")

    address = None

    if address_id and client:

        address = get_object_or_404(
            ClientDeliveryAddress,
            id=address_id,
            client=client,
            is_active=True,
        )
        _switch_address_scoped_cart(request, address)

    elif client:

        _, address = _address_scoped_cart(request, client=client)

    else:
        _address_scoped_cart(request)

    products = []
    product_groups = []

    lat = None
    lng = None
    pincode = ""

    if address:

        location = address.location or {}

        lat = location.get("lat")
        lng = location.get("lng")
        pincode = address.pincode or ""

    current_lat = request.GET.get("current_lat")
    current_lng = request.GET.get("current_lng")

    if lat is None or lng is None:

        if current_lat and current_lng:

            try:
                lat = float(current_lat)
                lng = float(current_lng)

            except (TypeError, ValueError):

                lat = None
                lng = None

    if not address:
        _cart_current_location(request)

    shops = (
        Shop.objects
        .filter(
            Q(is_shop_open=True) | Q(allow_scheduled_delivery=True)
        )
        .prefetch_related(
            "product_assignments__product"
        )
    )
    if is_theme_preview:
        shops = Shop.objects.filter(
            Q(is_shop_open=True) | Q(allow_scheduled_delivery=True)
        ).prefetch_related("product_assignments__product")

    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(Shop, storefront_slug=storefront_slug)
        if not _storefront_is_active(storefront_shop):
            return public_shop_storefront(request, storefront_slug)
        shops = shops.filter(pk=storefront_shop.pk)

    available_shops = []
    for shop in shops:
        shop.is_currently_open = is_shop_currently_open(shop)
        shop.is_scheduled_delivery = (
            not shop.is_currently_open and shop.allow_scheduled_delivery
        )
        if shop.is_currently_open or shop.is_scheduled_delivery:
            available_shops.append(shop)
    shops = available_shops
    scheduled_delivery_by_shop_id = {
        shop.pk: shop.is_scheduled_delivery
        for shop in shops
    }
    favorite_shop_ids = set() if (
        is_theme_preview or not request.user.is_authenticated
    ) else set(
        FavoriteShop.objects.filter(user=request.user).values_list("shop_id", flat=True)
    )
    favorite_product_ids = set() if (
        is_theme_preview or not request.user.is_authenticated
    ) else set(
        FavoriteProduct.objects.filter(user=request.user).values_list(
            "product_shop_id",
            flat=True,
        )
    )
    for shop in shops:
        shop.is_favorited = shop.pk in favorite_shop_ids
        shop.reputation = _reputation_metrics(shop)

    search = request.GET.get(
        "q",
        ""
    ).strip()
    selected_shop_id = request.GET.get("shop")
    selected_market = request.GET.get("market", "").strip()
    valid_markets = {value for value, _label in Shop.MARKET_CATEGORY_CHOICES}
    if selected_market not in valid_markets:
        selected_market = ""
    hide_scheduled_delivery = request.GET.get("hide_scheduled") == "1"

    if not is_theme_preview:
        UserActivity.objects.create(
            user=request.user if request.user.is_authenticated else None,
            event_type="MARKETPLACE_SEARCH" if search else "MARKETPLACE_VIEW",
            path=request.path,
            search_term=search,
            location={"lat": lat, "lng": lng} if lat is not None and lng is not None else {},
            metadata={"shop_id": selected_shop_id or "", "market": selected_market},
        )

    nearby_shops = []
    deliverable_shops = []

    for shop in shops:
        has_delivery_location = (
            lat is not None and lng is not None
        ) or bool(pincode)
        if has_delivery_location and not shop_delivers_to_location(
            shop,
            lat=lat,
            lng=lng,
            pincode=pincode or None,
            allow_scheduled_delivery=shop.allow_scheduled_delivery,
        ):
            continue

        deliverable_shops.append(shop)
        shop_location = shop.location or {}
        shop_lat = shop_location.get("lat") if isinstance(shop_location, dict) else None
        shop_lng = shop_location.get("lng") if isinstance(shop_location, dict) else None
        distance_km = distance_km_between_points(lat, lng, shop_lat, shop_lng) if lat is not None and lng is not None else None

        nearby_shops.append({
            "shop": shop,
            "distance_km": distance_km,
            "reputation": shop.reputation,
            "is_scheduled_delivery": shop.is_scheduled_delivery,
        })

    search = search.lower()
    search_terms = [term for term in search.split() if term]
    normalized_search_terms = [
        normalize_search_text(term) for term in search_terms
    ]

    def matches_search(values, match_any_term=False):
        searchable_text = normalize_search_text(
            " ".join(str(value or "") for value in values)
        )
        normalized_search = normalize_search_text(search)
        terms_match = (
            any(term in searchable_text for term in normalized_search_terms)
            if match_any_term
            else all(term in searchable_text for term in normalized_search_terms)
        )
        return bool(normalized_search and (
            normalized_search in searchable_text
            or terms_match
        ))

    def shop_search_fields(shop):
        return [
            shop.name,
            shop.alias_name,
            shop.shop_category,
            shop.market_category,
            shop.shop_data,
        ]

    def product_search_fields(assignment):
        product = assignment.product
        return [
            product.product_name,
            product.product_alias_name,
            product.product_title,
            product.product_description,
            product.search_keywords,
            product.product_category,
            product.product_color,
            product.product_size,
            product.sku,
            product.unit,
        ]

    def product_catalog_fields(assignment):
        product = assignment.product
        return [
            product.product_name,
            product.product_alias_name,
            product.product_title,
            product.product_description,
            product.search_keywords,
            product.product_category,
            product.product_color,
            product.product_size,
            product.sku,
            product.unit,
        ]

    matching_shop_ids = set()
    for shop in deliverable_shops:
        if selected_market and (shop.market_category or "DAILY MARKET") != selected_market:
            continue
        if selected_shop_id and str(shop.id) != str(selected_shop_id):
            continue

        assignments = (
            ProductShop.objects
            .select_related(
                "product",
                "shop"
            )
            .filter(
                shop=shop,
                product__is_active=True,
                is_active=True,
            )
            .filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
        )

        shop_identity_match = matches_search([
            shop.name,
            shop.alias_name,
            shop.shop_data,
        ])
        shop_matches = []
        if not search or shop_identity_match:
            shop_matches = [
                assignment for assignment in assignments
                if assignment.stock_for_customer(is_b2b=is_b2b) > 0
            ]
        else:
            for assignment in assignments:
                if assignment.stock_for_customer(is_b2b=is_b2b) <= 0:
                    continue
                fields = product_search_fields(assignment)
                if matches_search(fields, match_any_term=True):
                    shop_matches.append(assignment)
                    continue

                normalized_fields = [
                    normalize_search_text(str(value).strip())
                    for value in product_catalog_fields(assignment)
                    if value
                ]
                best_score = max(
                    (
                        ratio(
                            normalize_search_text(search),
                            field,
                        )
                        for field in normalized_fields
                    ),
                    default=0,
                )
                fuzzy_word_score = max(
                    (
                        ratio(term, candidate)
                        for term in normalized_search_terms
                        if len(term) >= 4
                        for field in normalized_fields
                        for candidate in field.split()
                        if len(candidate) >= 4
                    ),
                    default=0,
                )
                if best_score >= 60 or fuzzy_word_score >= 75:
                    assignment.fuzzy_score = max(best_score, fuzzy_word_score)
                    shop_matches.append(assignment)

        if shop_matches:
            products.extend(shop_matches)
            matching_shop_ids.add(shop.id)

    if hide_scheduled_delivery:
        products = [
            item for item in products
            if not scheduled_delivery_by_shop_id.get(item.shop_id, False)
        ]

    if search:
        nearby_shops = [
            entry for entry in nearby_shops
            if entry["shop"].id in matching_shop_ids
        ]
        # assignments = (
        #     ProductShop.objects
        #     .select_related(
        #         "product",
        #         "shop"
        #     )
        #     .filter(
        #         shop=shop,
        #         product__is_active=True,
        #         is_active=True,
        #         stock__gt=0
        #     )
        # )
        # search = search.strip().lower()
        # if search:
        #     exact_assignments = assignments.filter(
        #         Q(
        #             product__product_name__icontains=search
        #         )
        #         |
        #         Q(
        #             product__product_alias_name__icontains=search
        #         )
        #         |
        #         Q(
        #             product__product_title__icontains=search
        #         )
        #         |
        #         Q(
        #             product__product_description__icontains=search
        #         )
        #         |
        #         Q(
        #             product__sku__icontains=search
        #         )
        #         |
        #         Q(
        #             product__product_color__icontains=search
        #         )
        #     )

        # products.extend(assignments)

        # # ============================================================
        # # APPROXIMATE SEARCH
        # # ============================================================

        # fuzzy_assignments = assignments.exclude(
        #     id__in=exact_assignments.values_list("id", flat=True)
        # )

        # for assignment in fuzzy_assignments:

        #     product = assignment.product

        #     fields = [
        #         product.product_name or "",
        #         product.product_alias_name or "",
        #         product.product_title or "",
        #         product.product_description or "",
        #         product.sku or "",
        #         product.product_color or "",
        #     ]

        #     best_score = 0

        #     for value in fields:

        #         value = str(value).lower()

        #         score = ratio(
        #             search,
        #             value
        #         )

        #         best_score = max(
        #             best_score,
        #             score
        #         )

        #     # 60 = approximate match threshold
        #     if best_score >= 60:

        #         assignment.fuzzy_score = best_score

        #         products.append(
        #             assignment
        #         )

    nearby_shops.sort(key=_nearby_shop_sort_key)
    nearby_shop_order = [entry["shop"].id for entry in nearby_shops]

    market_lookup = {}
    for market_name, _ in Shop.MARKET_CATEGORY_CHOICES:
        market_lookup.setdefault(market_name, [])

    for item in products:
        market_name = item.shop.market_category or "DAILY MARKET"
        market_lookup.setdefault(market_name, []).append(item)

    market_choices = [
        (selected_market, selected_market)
    ] if selected_market else Shop.MARKET_CATEGORY_CHOICES

    for market_name, _ in market_choices:
        items = market_lookup.get(market_name, [])
        subgroups = {}

        for item in items:
            category_name = item.product.product_category or item.shop.shop_category or "OTHER"
            subgroups.setdefault(category_name, []).append(item)

        product_groups.append({
            "market_name": market_name,
            "subcategories": [
                {
                    "name": sub_name,
                    "items": (
                        _interleave_category_products(
                            sorted(
                                [
                                    item for item in sub_items
                                    if (
                                        item.product.product_category,
                                        item.product.product_name,
                                    ) in featured_products
                                ],
                                key=lambda item: featured_products[
                                    (
                                        item.product.product_category,
                                        item.product.product_name,
                                    )
                                ],
                            ),
                            nearby_shop_order,
                        )
                        + _interleave_category_products(
                            [
                                item for item in sub_items
                                if (
                                    item.product.product_category,
                                    item.product.product_name,
                                ) not in featured_products
                            ],
                            nearby_shop_order,
                        )
                    ),
                }
                for sub_name, sub_items in sorted(
                    subgroups.items(),
                    key=lambda item: (
                        category_order_index.get(item[0], len(category_order_index)),
                        item[0],
                    ),
                )
            ],
        })

    for market_group in product_groups:
        for subcategory in market_group["subcategories"]:
            for item in subcategory["items"]:
                item.is_marketplace_featured = (
                    item.product.product_category,
                    item.product.product_name,
                ) in featured_products
                if is_theme_preview:
                    item.is_favorited = False

    cart = _address_scoped_cart(request)[0]

    for item in products:
        product = item.product
        item.cart_quantity = _cart_quantity(cart, item.id)
        item.bulk_cart_quantity = _cart_quantity(
            cart,
            item.id,
            is_bulk_order=True,
        )
        item.marketplace_search_keywords = format_search_keywords_by_language(
            product.search_keywords
        )
        item.shop.is_scheduled_delivery = scheduled_delivery_by_shop_id.get(
            item.shop_id,
            False,
        )
        item.marketplace_stock = item.stock_for_customer(is_b2b=is_b2b)
        item.marketplace_price = item.effective_price_for_customer(is_b2b=is_b2b)
        item.marketplace_unit = item.unit_for_customer(is_b2b=is_b2b)
        item.bulk_marketplace_price = item.effective_bulk_price_for_customer(is_b2b=is_b2b)
        item.bulk_marketplace_unit = item.bulk_unit_for_customer(is_b2b=is_b2b)
        item.has_bulk_offer = (
            item.bulk_marketplace_price is not None
            and bool(item.bulk_marketplace_unit)
        )
        item.has_b2b_price = (
            is_b2b and item.shop.has_valid_gstin and item.b2b_price is not None
        )
        item.has_b2b_bulk_offer = (
            is_b2b
            and item.shop.has_valid_gstin
            and item.b2b_bulk_price is not None
            and bool(item.b2b_bulk_unit)
        )
        name = product.product_name or "Product"
        description = product.product_description or f"{name} is available from {item.shop.name}."
        title = product.product_title or " ".join(
            value for value in (name, product.product_color, product.product_size) if value
        )
        item.marketplace_detail = {
            "id": item.id,
            "name": name,
            "title": title,
            "description": description,
            "sku": product.sku or "",
            "color": product.product_color or "",
            "size": product.product_size or "",
            "unit": item.marketplace_unit or "",
            "price": str(item.marketplace_price),
            "bulk_price": (
                str(item.bulk_marketplace_price)
                if item.bulk_marketplace_price is not None
                else ""
            ),
            "bulk_unit": item.bulk_marketplace_unit or "",
            "bulk_min_quantity": item.bulk_min_quantity,
            "bulk_available": item.has_bulk_offer,
            "has_b2b_price": item.has_b2b_price,
            "has_b2b_bulk_offer": item.has_b2b_bulk_offer,
            "stock": _json_stock_quantity(item.marketplace_stock),
            "image_url": item.display_image_url,
            "image_urls": item.display_image_urls,
            "shop": item.shop.name,
            "scheduled_delivery": item.shop.is_scheduled_delivery,
            "quantity": item.cart_quantity,
            "bulk_quantity": item.bulk_cart_quantity,
            "is_favorited": item.id in favorite_product_ids,
        }
        item.is_favorited = item.id in favorite_product_ids
        item.reputation = _reputation_metrics(item.shop, item)
        item.marketplace_detail_script_id = f"market-product-data-{item.id}"

    cart_count = sum(
        int(quantity)
        for quantity in cart.values()
    )

    response = render(
        request,
        "market/marketplace.html",
        {
            "client": client,
            "is_theme_preview": is_theme_preview,
            "is_b2b_customer": is_b2b,
            "address": address,
            "products": products,
            "product_groups": product_groups,
            "marketplace_products_per_row": (
                marketplace_layout_settings.products_per_row
                if marketplace_layout_settings
                else 4
            ),
            "marketplace_rows_per_category": (
                marketplace_layout_settings.rows_per_category
                if marketplace_layout_settings
                else 2
            ),
            "marketplace_theme": marketplace_theme,
            "marketplace_theme_order": marketplace_theme_order,
            "marketplace_show_hero": (
                marketplace_theme.marketplace_hero_enabled
                if marketplace_theme else True
            ),
            "marketplace_show_nearby_shops": (
                marketplace_theme.marketplace_nearby_shops_enabled
                if marketplace_theme else True
            ),
            "marketplace_show_category_nav": (
                marketplace_theme.marketplace_category_nav_enabled
                if marketplace_theme else True
            ),
            "marketplace_show_products": (
                marketplace_theme.marketplace_products_enabled
                if marketplace_theme else True
            ),
            "nearby_shops": nearby_shops,
            "selected_shop_id": selected_shop_id,
            "selected_market": selected_market,
            "hide_scheduled_delivery": hide_scheduled_delivery,
            "cart_count": cart_count,

            "has_location": (
                lat is not None and
                lng is not None
            ),

            "using_current_location": (
                not address and
                lat is not None and
                lng is not None
            ),
        }
    )
    if is_theme_preview:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def _can_manage_shop_stock(request, shop):
    if shop.owner_id == request.user.id:
        return True
    return ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        is_active=True,
        can_manage_stock=True,
    ).exists()


@login_required
@transaction.atomic
def stock_import(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id)
    if not _can_manage_shop_stock(request, shop):
        return HttpResponseForbidden("You do not have stock management permission.")
    if request.method == "GET":
        if request.GET.get("format") == "update":
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="shop-{shop.id}-current-stock.csv"'
            )
            writer = csv.writer(response)
            writer.writerow(["product_shop_id", "sku", "product_name", "stock"])
            assignments = (
                ProductShop.objects
                .filter(shop=shop)
                .select_related("product")
                .order_by("product__product_name", "id")
            )
            for assignment in assignments:
                writer.writerow([
                    assignment.id,
                    assignment.product.sku,
                    assignment.product.product_name,
                    assignment.stock,
                ])
            return response
        if request.GET.get("format") == "sample":
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="{shop.name}-stock-import-sample.csv"'
            )
            writer = csv.writer(response)
            writer.writerow(["product_shop_id", "sku", "product_name", "stock"])
            writer.writerow(["17", "SKU-001", "Potato", "50"])
            return response
        return render(request, "seller/stock_import.html", {"shop": shop})
    if request.method != "POST" or not request.FILES.get("file"):
        return JsonResponse({"success": False, "message": "Upload a CSV file."}, status=400)

    uploaded_file = request.FILES["file"]
    try:
        rows = csv.DictReader(uploaded_file.read().decode("utf-8-sig").splitlines())
    except UnicodeDecodeError:
        return JsonResponse({"success": False, "message": "Use a UTF-8 CSV file."}, status=400)

    updated = 0
    for row in rows:
        product_shop_id = row.get("product_shop_id") or row.get("id")
        sku = row.get("sku")
        product_name = row.get("product_name")
        assignment = ProductShop.objects.filter(shop=shop).select_related("product").filter(
            id=product_shop_id
        ).first() if product_shop_id else None
        if not assignment and sku:
            assignment = ProductShop.objects.filter(shop=shop, product__sku=sku).select_related("product").first()
        if not assignment and product_name:
            assignment = ProductShop.objects.filter(shop=shop, product__product_name__iexact=product_name).select_related("product").first()
        if not assignment:
            continue
        try:
            new_stock = max(0, int(row.get("stock", "0")))
        except (TypeError, ValueError):
            continue
        delta = new_stock - assignment.stock
        assignment.stock = new_stock
        update_fields = ["stock"]
        if new_stock and assignment.product.expiry_management_enabled and assignment.product.shelf_life_days:
            assignment.stocked_at = timezone.now()
            assignment.expires_at = assignment.stocked_at + timedelta(days=assignment.product.shelf_life_days)
            update_fields.extend(["stocked_at", "expires_at"])
        assignment.save(update_fields=update_fields)
        if delta:
            StockMovement.objects.create(
                product_shop=assignment,
                movement_type="IMPORT",
                quantity=delta,
                reference=uploaded_file.name,
                created_by=request.user,
            )
        updated += 1

    return JsonResponse({"success": True, "updated": updated})


@login_required
def shop_order_report(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id)
    if shop.owner_id != request.user.id:
        raise PermissionDenied

    today = timezone.localdate()
    start_value = request.GET.get("date_from", today.isoformat())
    end_value = request.GET.get("date_to", today.isoformat())
    errors = []
    try:
        date_from = date.fromisoformat(start_value)
    except (TypeError, ValueError):
        date_from = today
        errors.append("Enter a valid start date.")
    try:
        date_to = date.fromisoformat(end_value)
    except (TypeError, ValueError):
        date_to = today
        errors.append("Enter a valid end date.")
    if date_from > date_to:
        errors.append("The start date must be on or before the end date.")

    active_orders = ShopOrder.objects.filter(shop=shop).exclude(status__iexact="CANCELLED")

    def summarize(queryset):
        summary = queryset.aggregate(
            order_count=Count("id"),
            total_amount=Sum("total_amount"),
        )
        total_amount = summary["total_amount"] or Decimal("0.00")
        order_count = summary["order_count"]
        return {
            "order_count": order_count,
            "total_amount": total_amount,
            "average_order_value": (
                total_amount / Decimal(order_count)
                if order_count
                else Decimal("0.00")
            ),
        }

    today_summary = summarize(active_orders.filter(created_at__date=today))
    if errors:
        period_summary = {"order_count": 0, "total_amount": Decimal("0.00"), "average_order_value": Decimal("0.00")}
        report_orders = ShopOrder.objects.none()
    else:
        period_orders = active_orders.filter(created_at__date__range=(date_from, date_to))
        period_summary = summarize(period_orders)
        report_orders = period_orders.select_related("order").order_by("-created_at")[:100]

    if request.GET.get("format") == "gstr1":
        if errors:
            return HttpResponse("; ".join(errors), status=400, content_type="text/plain")
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="{shop.name}-gstr1-{date_from}-{date_to}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow([
            "Invoice number", "Invoice date", "Supplier GSTIN", "Recipient GSTIN",
            "Place of supply", "Description", "HSN/SAC", "Quantity", "Unit",
            "GST rate", "Taxable value", "CGST", "SGST", "IGST", "Invoice status",
        ])
        for shop_order in period_orders.select_related("order").prefetch_related("order_items__product").order_by("created_at"):
            invoice_date = timezone.localtime(shop_order.created_at).date().isoformat()
            for line in shop_order.order_items.all():
                writer.writerow([
                    shop_order.invoice_number,
                    invoice_date,
                    shop_order.seller_gstin,
                    shop_order.buyer_gstin,
                    shop_order.buyer_state_code,
                    line.product_name,
                    line.hsn_code,
                    line.final_weight if line.is_approximate_weight and line.final_weight else line.quantity,
                    (
                        _weight_display_unit(
                            line.unit or (line.product.unit if line.product_id else ""),
                        )
                        if line.is_approximate_weight
                        else line.unit or (line.product.unit if line.product_id else "")
                    ),
                    line.gst_rate,
                    line.taxable_amount,
                    line.cgst_amount,
                    line.sgst_amount,
                    line.igst_amount,
                    shop_order.status,
                ])
            delivery_taxable = max(
                Decimal("0.00"),
                shop_order.taxable_amount
                - sum(
                    (line.taxable_amount for line in shop_order.order_items.all()),
                    Decimal("0.00"),
                ),
            )
            if delivery_taxable:
                local_delivery_tax = (
                    shop_order.seller_state_code
                    and shop_order.seller_state_code == shop_order.buyer_state_code
                )
                delivery_cgst = (
                    (shop_order.delivery_gst_amount / 2).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    if local_delivery_tax else Decimal("0.00")
                )
                writer.writerow([
                    shop_order.invoice_number,
                    invoice_date,
                    shop_order.seller_gstin,
                    shop_order.buyer_gstin,
                    shop_order.buyer_state_code,
                    "Delivery service",
                    shop_order.delivery_hsn_code,
                    1,
                    "service",
                    shop_order.delivery_gst_rate,
                    delivery_taxable,
                    delivery_cgst,
                    shop_order.delivery_gst_amount - delivery_cgst if local_delivery_tax else Decimal("0.00"),
                    Decimal("0.00") if local_delivery_tax else shop_order.delivery_gst_amount,
                    shop_order.status,
                ])
        return response

    if request.GET.get("format") == "csv":
        if errors:
            return HttpResponse("; ".join(errors), status=400, content_type="text/plain")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{shop.name}-order-report-{date_from}-{date_to}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Shop", shop.name])
        writer.writerow(["Period", date_from.isoformat(), date_to.isoformat()])
        writer.writerow(["Orders", "Total order value", "Average order value"])
        writer.writerow([
            period_summary["order_count"],
            period_summary["total_amount"],
            period_summary["average_order_value"],
        ])
        writer.writerow([])
        writer.writerow(["Order ID", "Date/time", "Status", "Order amount"])
        for shop_order in period_orders.select_related("order").order_by("-created_at"):
            writer.writerow([
                shop_order.order.order_id,
                timezone.localtime(shop_order.created_at).isoformat(),
                shop_order.status,
                shop_order.total_amount,
            ])
        return response

    return render(
        request,
        "seller/shop_order_report.html",
        {
            "shop": shop,
            "today": today,
            "today_summary": today_summary,
            "period_summary": period_summary,
            "date_from": start_value,
            "date_to": end_value,
            "errors": errors,
            "report_orders": report_orders,
        },
    )


def _razorpayx_request(method, path, *, payload=None, headers=None):
    if not (
        settings.RAZORPAYX_KEY_ID
        and settings.RAZORPAYX_KEY_SECRET
        and settings.RAZORPAYX_ACCOUNT_NUMBER
    ):
        raise RuntimeError("RazorpayX payout configuration is incomplete.")
    response = requests.request(
        method,
        f"https://api.razorpay.com/v1/{path.lstrip('/')}",
        auth=(settings.RAZORPAYX_KEY_ID, settings.RAZORPAYX_KEY_SECRET),
        json=payload,
        headers=headers or {},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("RazorpayX returned an invalid response.")
    return data


@transaction.atomic
def _release_shop_payout(payout_id, *, status, failure_reason):
    payout = ShopPayout.objects.select_for_update().select_related("shop").get(pk=payout_id)
    if payout.status in {"FAILED", "REVERSED"} or (
        payout.status == "PROCESSED" and status != "REVERSED"
    ):
        return payout
    wallet = ShopWallet.objects.select_for_update().get(shop=payout.shop)
    if not ShopWalletTransaction.objects.filter(
        payout=payout,
        transaction_type="REVERSAL",
    ).exists():
        wallet.balance += payout.amount
        wallet.save(update_fields=["balance", "updated_at"])
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            payout=payout,
            transaction_type="REVERSAL",
            amount=payout.amount,
            balance_after=wallet.balance,
            description=f"Funds returned for payout {payout.reference_id}.",
        )
    payout.status = status
    payout.failure_reason = failure_reason[:2000]
    payout.save(update_fields=["status", "failure_reason", "updated_at"])
    return payout


@transaction.atomic
def _set_shop_payout_status(payout_id, status, *, failure_reason=""):
    payout = ShopPayout.objects.select_for_update().get(pk=payout_id)
    if payout.status in {"FAILED", "REVERSED"} or (
        payout.status == "PROCESSED" and status != "REVERSED"
    ):
        return payout
    if status in {"FAILED", "REVERSED"}:
        return _release_shop_payout(
            payout.pk,
            status=status,
            failure_reason=failure_reason,
        )
    payout.status = status
    payout.save(update_fields=["status", "updated_at"])
    return payout


@login_required
def shop_wallet(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id)
    is_owner = shop.owner_id == request.user.id
    can_view_financials = is_owner or ShopStaff.objects.filter(
        shop=shop,
        user=request.user,
        role="MANAGER",
        can_view_reports=True,
        is_active=True,
    ).exists()
    if not can_view_financials:
        raise PermissionDenied
    if request.method == "POST" and not is_owner:
        raise PermissionDenied

    wallet, _ = ShopWallet.objects.get_or_create(shop=shop)
    site_wallet, _ = SiteWallet.objects.get_or_create(
        key="SITE",
        defaults={"commission_rate_percent": Decimal("3.00")},
    )
    payout_configured = bool(
        settings.RAZORPAYX_KEY_ID
        and settings.RAZORPAYX_KEY_SECRET
        and settings.RAZORPAYX_ACCOUNT_NUMBER
    )

    if request.method == "POST":
        if not payout_configured:
            messages.error(request, "Shop withdrawals are not configured. Contact the platform administrator.")
            return redirect("shop_wallet", shop_id=shop.pk)
        try:
            amount = Decimal(request.POST.get("amount", "").strip())
            if not amount.is_finite() or amount.quantize(Decimal("0.01")) != amount:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, "Enter a valid withdrawal amount with up to two decimal places.")
            return redirect("shop_wallet", shop_id=shop.pk)
        if amount < Decimal("1.00"):
            messages.error(request, "The minimum withdrawal is ₹1.00.")
            return redirect("shop_wallet", shop_id=shop.pk)

        try:
            fund_account_id = shop.razorpay_fund_account_id
            if not fund_account_id:
                holder_name = request.POST.get("account_holder", "").strip()
                account_number = request.POST.get("account_number", "").strip()
                ifsc = request.POST.get("ifsc", "").strip().upper()
                contact_phone = re.sub(r"\D", "", request.POST.get("contact_phone", ""))
                contact_email = request.POST.get("contact_email", "").strip()
                if (
                    len(holder_name) < 2
                    or not re.fullmatch(r"\d{8,34}", account_number)
                    or not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc)
                    or not re.fullmatch(r"\d{10,15}", contact_phone)
                    or "@" not in contact_email
                ):
                    messages.error(request, "Enter a valid account holder, bank account number, IFSC, phone number, and email.")
                    return redirect("shop_wallet", shop_id=shop.pk)

                contact = _razorpayx_request(
                    "POST",
                    "contacts",
                    payload={
                        "name": holder_name[:100],
                        "email": contact_email[:100],
                        "contact": contact_phone,
                        "type": "vendor",
                        "reference_id": f"SHOP{shop.pk}C{uuid.uuid4().hex[:16]}",
                    },
                )
                contact_id = contact.get("id")
                if not isinstance(contact_id, str) or not contact_id.startswith("cont_"):
                    raise ValueError("RazorpayX did not return a valid contact.")
                fund_account = _razorpayx_request(
                    "POST",
                    "fund_accounts",
                    payload={
                        "contact_id": contact_id,
                        "account_type": "bank_account",
                        "bank_account": {
                            "name": holder_name[:100],
                            "ifsc": ifsc,
                            "account_number": account_number,
                        },
                    },
                )
                fund_account_id = fund_account.get("id")
                if not isinstance(fund_account_id, str) or not fund_account_id.startswith("fa_"):
                    raise ValueError("RazorpayX did not return a valid fund account.")
                Shop.objects.filter(pk=shop.pk).update(
                    razorpay_contact_id=contact_id,
                    razorpay_fund_account_id=fund_account_id,
                )
                shop.razorpay_contact_id = contact_id
                shop.razorpay_fund_account_id = fund_account_id

            with transaction.atomic():
                locked_wallet = ShopWallet.objects.select_for_update().get(pk=wallet.pk)
                if amount > locked_wallet.balance:
                    messages.error(request, "Withdrawal amount exceeds your available wallet balance.")
                    return redirect("shop_wallet", shop_id=shop.pk)
                reference_id = f"S{shop.pk}{uuid.uuid4().hex[:24]}"
                payout = ShopPayout.objects.create(
                    shop=shop,
                    amount=amount,
                    status="PROCESSING",
                    razorpay_fund_account_id=fund_account_id,
                    reference_id=reference_id,
                )
                locked_wallet.balance -= amount
                locked_wallet.save(update_fields=["balance", "updated_at"])
                ShopWalletTransaction.objects.create(
                    wallet=locked_wallet,
                    payout=payout,
                    transaction_type="DEBIT",
                    amount=amount,
                    balance_after=locked_wallet.balance,
                    description=f"Withdrawal initiated ({reference_id}).",
                )

            try:
                payout_response = _razorpayx_request(
                    "POST",
                    "payouts",
                    payload={
                        "account_number": settings.RAZORPAYX_ACCOUNT_NUMBER,
                        "fund_account_id": fund_account_id,
                        "amount": int(amount * 100),
                        "currency": "INR",
                        "mode": request.POST.get("mode", "IMPS")
                        if request.POST.get("mode") in {"IMPS", "NEFT", "RTGS"}
                        else "IMPS",
                        "purpose": "payout",
                        "queue_if_low_balance": True,
                        "reference_id": reference_id,
                        "narration": f"Shop payout {shop.pk}"[:30],
                    },
                    headers={"X-Payout-Idempotency": str(payout.idempotency_key)},
                )
                razorpay_payout_id = payout_response.get("id", "")
                payout_status = (payout_response.get("status") or "pending").lower()
                if not isinstance(razorpay_payout_id, str) or not razorpay_payout_id.startswith("pout_"):
                    raise ValueError("RazorpayX did not return a valid payout.")
                payout.razorpay_payout_id = razorpay_payout_id
                payout.save(update_fields=["razorpay_payout_id", "updated_at"])
                if payout_status in {"failed", "rejected", "reversed", "cancelled"}:
                    _release_shop_payout(
                        payout.pk,
                        status="FAILED" if payout_status != "reversed" else "REVERSED",
                        failure_reason=payout_response.get("status_details", {}).get("description", payout_status),
                    )
                    messages.error(request, "RazorpayX rejected the payout; the funds were returned to your shop wallet.")
                else:
                    payout.status = {
                        "processed": "PROCESSED",
                        "queued": "QUEUED",
                        "processing": "PROCESSING",
                        "pending": "PENDING",
                    }.get(payout_status, "UNKNOWN")
                    payout.save(update_fields=["status", "updated_at"])
                    messages.success(request, f"Withdrawal of ₹{amount:.2f} submitted to RazorpayX.")
            except requests.HTTPError as error:
                if error.response is not None and 400 <= error.response.status_code < 500:
                    _release_shop_payout(
                        payout.pk,
                        status="FAILED",
                        failure_reason=str(error),
                    )
                    logger.warning("RazorpayX rejected shop payout %s: %s", payout.reference_id, error)
                    messages.error(request, "RazorpayX rejected the payout. The amount was returned to your shop wallet.")
                else:
                    payout.status = "UNKNOWN"
                    payout.failure_reason = "Payout submission status is unknown; awaiting RazorpayX confirmation."
                    payout.save(update_fields=["status", "failure_reason", "updated_at"])
                    logger.exception("RazorpayX payout status is unknown for payout %s.", payout.reference_id)
                    messages.warning(request, "Payout submission could not be confirmed. Funds remain reserved while awaiting RazorpayX status; do not retry the withdrawal.")
            except (requests.RequestException, ValueError):
                payout.status = "UNKNOWN"
                payout.failure_reason = "Payout submission status is unknown; awaiting RazorpayX confirmation."
                payout.save(update_fields=["status", "failure_reason", "updated_at"])
                logger.exception("RazorpayX payout status is unknown for payout %s.", payout.reference_id)
                messages.warning(request, "Payout submission could not be confirmed. Funds remain reserved while awaiting RazorpayX status; do not retry the withdrawal.")
        except (requests.RequestException, ValueError, RuntimeError):
            logger.exception("Could not prepare RazorpayX bank recipient for shop %s.", shop.pk)
            messages.error(request, "Could not register the bank account with RazorpayX. Check the bank details or contact the platform administrator.")
        return redirect("shop_wallet", shop_id=shop.pk)

    wallet.refresh_from_db()
    transactions = wallet.transactions.select_related(
        "shop_order__order",
        "payout",
    )
    return render(
        request,
        "seller/shop_wallet.html",
        {
            "shop": shop,
            "wallet": wallet,
            "commission_rate_percent": site_wallet.commission_rate_percent,
            "transactions": wallet.transactions.select_related("shop_order__order", "payout")[:50],
            "payouts": shop.payouts.all()[:20],
            "payout_configured": payout_configured,
            "bank_account_registered": bool(shop.razorpay_fund_account_id),
            "can_withdraw": is_owner,
            "total_order_credits": transactions.filter(
                transaction_type="CREDIT",
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
            "total_commissions": SiteWalletTransaction.objects.filter(
                shop_order__shop=shop,
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
            "total_withdrawals": transactions.filter(
                transaction_type="DEBIT",
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        },
    )


@csrf_exempt
def razorpayx_payout_webhook(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if not settings.RAZORPAYX_WEBHOOK_SECRET:
        return JsonResponse({"error": "Webhook is not configured."}, status=503)
    supplied_signature = request.headers.get("X-Razorpay-Signature", "")
    expected_signature = hmac.new(
        settings.RAZORPAYX_WEBHOOK_SECRET.encode(),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        return JsonResponse({"error": "Invalid webhook signature."}, status=400)
    try:
        event = json.loads(request.body)
        payout_data = event["payload"]["payout"]["entity"]
        payout_id = payout_data["id"]
        gateway_status = payout_data["status"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({"error": "Invalid payout event."}, status=400)

    payout = ShopPayout.objects.filter(razorpay_payout_id=payout_id).first()
    if not payout:
        return JsonResponse({"received": True, "ignored": True})
    normalized_status = str(gateway_status).lower()
    payout_status = {
        "processed": "PROCESSED",
        "queued": "QUEUED",
        "processing": "PROCESSING",
        "pending": "PENDING",
        "failed": "FAILED",
        "rejected": "FAILED",
        "cancelled": "FAILED",
        "reversed": "REVERSED",
    }.get(normalized_status, "UNKNOWN")
    _set_shop_payout_status(
        payout.pk,
        payout_status,
        failure_reason=payout_data.get("status_details", {}).get("description", normalized_status),
    )
    return JsonResponse({"received": True})


@login_required
@transaction.atomic
def stock_report(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id)
    if not _can_manage_shop_stock(request, shop):
        return HttpResponseForbidden("You do not have stock report permission.")

    now = timezone.now()
    expired_assignments = (
        ProductShop.objects
        .select_for_update()
        .filter(
            shop=shop,
            is_active=True,
            product__expiry_management_enabled=True,
        )
        .filter(
            Q(stock__gt=0) | Q(b2b_stock__gt=0),
            expires_at__lte=now,
        )
        .select_related("product")
    )
    for assignment in expired_assignments:
        expired_quantity = assignment.stock
        expired_b2b_quantity = assignment.b2b_stock
        assignment.stock = 0
        assignment.b2b_stock = 0
        assignment.save(update_fields=["stock", "b2b_stock"])
        for quantity, stock_type in (
            (expired_quantity, "regular"),
            (expired_b2b_quantity, "B2B"),
        ):
            if quantity:
                StockMovement.objects.create(
                    product_shop=assignment,
                    movement_type="EXPIRY",
                    quantity=-quantity,
                    reference=f"Expired {stock_type} stock removed from available inventory",
                    created_by=request.user,
                )

    report = []
    today = timezone.localdate()
    history_start = today - timedelta(days=364)
    recent_start = today - timedelta(days=29)
    history_days = [
        history_start + timedelta(days=offset)
        for offset in range((today - history_start).days + 1)
    ]

    def season_for(month):
        if month in (12, 1, 2):
            return "Winter"
        if month in (3, 4, 5):
            return "Summer"
        if month in (6, 7, 8, 9):
            return "Monsoon"
        return "Post-monsoon"

    current_season = season_for(today.month)

    for assignment in (
        ProductShop.objects
        .filter(shop=shop, is_active=True)
        .select_related("product")
    ):
        daily_sales = defaultdict(Decimal)
        sales_movements = assignment.stock_movements.filter(
            movement_type="SALE",
            created_at__date__gte=history_start,
        ).values_list("quantity", "created_at")
        for quantity, created_at in sales_movements:
            sale_day = timezone.localtime(created_at).date()
            daily_sales[sale_day] += abs(Decimal(quantity))

        recent_days = [day for day in history_days if day >= recent_start]
        recent_daily_average = (
            sum((daily_sales[day] for day in recent_days), Decimal("0"))
            / Decimal(len(recent_days))
        )
        weekday_average = {}
        next_weekday_average = Decimal("0")
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for weekday in range(7):
            weekdays = [day for day in history_days if day.weekday() == weekday]
            average = (
                sum((daily_sales[day] for day in weekdays), Decimal("0"))
                / Decimal(len(weekdays))
                if weekdays else Decimal("0")
            )
            weekday_average[weekday_names[weekday]] = str(average.quantize(Decimal("0.01")))
        for offset in range(7):
            weekday = (today.weekday() + offset) % 7
            next_weekday_average += Decimal(weekday_average[weekday_names[weekday]])
        next_weekday_average /= Decimal("7")

        seasonal_days = [day for day in history_days if season_for(day.month) == current_season]
        seasonal_average = (
            sum((daily_sales[day] for day in seasonal_days), Decimal("0"))
            / Decimal(len(seasonal_days))
            if seasonal_days else Decimal("0")
        )
        peak_day_sales = sorted(
            (daily_sales[day] for day in history_days),
            reverse=True,
        )
        peak_day_count = max(1, math.ceil(len(peak_day_sales) * 0.1)) if peak_day_sales else 0
        festival_average = (
            sum(peak_day_sales[:peak_day_count], Decimal("0")) / Decimal(peak_day_count)
            if peak_day_count else Decimal("0")
        )
        daily_average = max(recent_daily_average, next_weekday_average)
        demand_average = max(daily_average, seasonal_average, festival_average)
        recommended_stock = Decimal(
            math.ceil(demand_average * Decimal("7"))
        )

        forecast, _ = ShopProductForecast.objects.update_or_create(
            product_shop=assignment,
            defaults={
                "daily_average": daily_average,
                "weekday_average": weekday_average,
                "festival_average": festival_average.quantize(Decimal("0.01")),
                "recommended_stock": recommended_stock,
            },
        )
        report.append({
            "assignment": assignment,
            "daily_average": forecast.daily_average,
            "weekday_average": forecast.weekday_average,
            "season": current_season,
            "seasonal_average": seasonal_average.quantize(Decimal("0.01")),
            "festival_average": forecast.festival_average,
            "recommended_stock": forecast.recommended_stock,
            "shortage": max(Decimal("0.00"), forecast.recommended_stock - assignment.stock),
            "expires_at": assignment.expires_at,
        })

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{shop.name}-stock-report-excel.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Product", "SKU", "Current stock", "Recent daily average",
            *[f"{day} avg." for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]],
            "Season", "Seasonal average", "Peak/festival proxy average",
            "Recommended stock (7 days)", "Shortage", "Expires at",
        ])
        for entry in report:
            writer.writerow([
                entry["assignment"].product.product_name,
                entry["assignment"].product.sku,
                entry["assignment"].stock,
                entry["daily_average"],
                *[
                    entry["weekday_average"].get(day, "0")
                    for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                ],
                entry["season"],
                entry["seasonal_average"],
                entry["festival_average"],
                entry["recommended_stock"],
                entry["shortage"],
                entry["expires_at"].isoformat() if entry["expires_at"] else "",
            ])
        return response

    return render(request, "seller/stock_report.html", {"shop": shop, "report": report})


@login_required
def packing_slip(request, shop_order_id):
    shop_order = get_object_or_404(
        ShopOrder.objects.select_related("shop", "order").prefetch_related("order_items"),
        id=shop_order_id,
    )
    if shop_order.shop.owner_id != request.user.id and not ShopStaff.objects.filter(
        shop=shop_order.shop, user=request.user, is_active=True
    ).exists():
        return HttpResponseForbidden("You are not authorized to print this packing slip.")
    if not shop_order.packing_slip_number:
        shop_order.packing_slip_number = f"PS-{shop_order.order.order_id}-{shop_order.id}"
        shop_order.save(update_fields=["packing_slip_number"])
    order_items = list(shop_order.order_items.select_related("product").all())
    for item in order_items:
        item.tax_amount = item.cgst_amount + item.sgst_amount + item.igst_amount
        unit = item.unit or (item.product.unit if item.product_id else "")
        item.quantity_unit = _quantity_unit_total(item.quantity, unit)
        item.quantity_unit_calculation = _quantity_unit_calculation(
            item.quantity,
            unit,
        )
        if item.is_approximate_weight:
            item.weight_display_unit = _weight_display_unit(unit)
            item.estimated_weight_calculation = item.quantity_unit_calculation
    invoice_total = (
        shop_order.taxable_amount
        + shop_order.cgst_amount
        + shop_order.sgst_amount
        + shop_order.igst_amount
    )
    order_url = request.build_absolute_uri(
        reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id})
    )
    qr_code_data_uri = _qr_code_data_uri(order_url)
    return render(
        request,
        "seller/packing_slip.html",
        {
            "shop_order": shop_order,
            "order_items": order_items,
            "invoice_total": invoice_total,
            "is_tax_invoice": bool(shop_order.seller_gstin),
            "qr_code_data_uri": qr_code_data_uri,
        },
    )

@login_required
def cart_add(request, product_shop_id):

    if request.method != "POST":
        return JsonResponse(
            {"success": False},
            status=405
        )

    if request.POST.get("offer", "retail") not in {"retail", "bulk"}:
        return JsonResponse({"success": False, "message": "Choose a valid product offer."}, status=400)

    item = get_object_or_404(
        ProductShop.objects.select_related(
            "product",
            "shop"
        ).filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        ),
        id=product_shop_id,
        is_active=True,
        product__is_active=True
    )
    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(
            Shop,
            storefront_slug=storefront_slug,
            storefront_subscription_active=True,
        )
        if not _storefront_is_active(storefront_shop) or item.shop_id != storefront_shop.pk:
            raise Http404("This product is not available in this shop storefront.")

    cart, active_delivery_address = _address_scoped_cart(request)
    current_location = _cart_current_location(request)
    if not _shop_can_deliver_to_address(
        item.shop,
        active_delivery_address,
        current_location=current_location,
    ):
        return JsonResponse(
            {
                "success": False,
                "message": "This product is not available in your delivery location.",
            },
            status=400,
        )

    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(Shop, storefront_slug=storefront_slug)
        existing_assignment_ids = {
            parsed[0]
            for key in cart
            if (parsed := _parse_cart_line_key(key)) is not None
        }
        if ProductShop.objects.filter(
            pk__in=existing_assignment_ids,
        ).exclude(shop_id=storefront_shop.pk).exists():
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Your cart contains products from another shop. "
                        "Remove those items before adding products from this shop."
                    ),
                },
                status=409,
            )

    is_bulk_order = request.POST.get("offer") == "bulk"
    is_b2b = _is_b2b_customer(request)
    available_stock = item.stock_for_customer(is_b2b=is_b2b)
    bulk_price = item.effective_bulk_price_for_customer(is_b2b=is_b2b)
    if is_bulk_order and (bulk_price is None or not item.bulk_unit_for_customer(is_b2b=is_b2b)):
        return JsonResponse({
            "success": False,
            "message": "This bulk offer is not available for your account.",
        }, status=400)

    line_key = _cart_line_key(product_shop_id, is_bulk_order=is_bulk_order)
    current_quantity = _cart_quantity(cart, product_shop_id, is_bulk_order=is_bulk_order)

    if is_bulk_order:
        try:
            adding_quantity = int(request.POST.get("quantity", item.bulk_min_quantity))
        except (TypeError, ValueError):
            adding_quantity = 0
        if adding_quantity < item.bulk_min_quantity:
            return JsonResponse({
                "success": False,
                "message": f"Bulk orders require at least {item.bulk_min_quantity} {item.bulk_unit_for_customer(is_b2b=is_b2b)}.",
                "quantity": current_quantity,
            }, status=400)
    else:
        adding_quantity = 1

    other_quantity = _cart_quantity(cart, product_shop_id, is_bulk_order=not is_bulk_order)
    if current_quantity + other_quantity + adding_quantity > available_stock:

        return JsonResponse(
            {
                "success": False,
                "message": "Maximum available stock reached.",
                "quantity": current_quantity,
            }
        )

    current_quantity += adding_quantity

    cart[line_key] = current_quantity

    _save_address_scoped_cart(request, cart)

    cart_count = sum(
        int(q)
        for q in cart.values()
    )

    return JsonResponse(
        {
            "success": True,
            "quantity": current_quantity,
            "cart_count": cart_count,
        }
    )

@login_required
def cart(request):
    client = ClientProfile.objects.filter(user=request.user).first()
    is_b2b = _is_b2b_customer(request, client)

    cart_data, active_delivery_address = _address_scoped_cart(request, client=client)
    current_location = _cart_current_location(request)

    parsed_lines = {
        key: parsed
        for key in cart_data
        if (parsed := _parse_cart_line_key(key)) is not None
    }
    assignment_ids = {parsed[0] for parsed in parsed_lines.values()}
    assignments = (
        ProductShop.objects
        .select_related(
            "product",
            "shop"
        )
        .filter(
            id__in=assignment_ids,
            product__is_active=True,
            is_active=True,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
    )
    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(Shop, storefront_slug=storefront_slug)
        if not _storefront_is_active(storefront_shop):
            return public_shop_storefront(request, storefront_slug)
        if assignments.exclude(shop_id=storefront_shop.pk).exists():
            messages.warning(
                request,
                "Your cart has items from another shop. Review your complete cart on SellerHelper before checking out.",
            )
            return redirect(_storefront_main_site_url(request, reverse("cart")))

    items = []

    subtotal = Decimal("0.00")

    assignments_by_id = {assignment.pk: assignment for assignment in assignments}
    unavailable_cart_lines = []
    cart_quantities_changed = False
    for line_key, (assignment_id, is_bulk_order) in parsed_lines.items():
        assignment = assignments_by_id.get(assignment_id)
        if assignment is None:
            unavailable_cart_lines.append(line_key)
            continue
        try:
            quantity = max(0, int(cart_data[line_key]))
        except (TypeError, ValueError):
            quantity = 0
        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        quantity = min(quantity, available_stock)
        available_for_delivery = _shop_can_deliver_to_address(
            assignment.shop,
            active_delivery_address,
            current_location=current_location,
        )
        is_scheduled_delivery = (
            available_for_delivery
            and assignment.shop.allow_scheduled_delivery
            and not is_shop_currently_open(assignment.shop)
        )
        if not available_for_delivery and quantity:
            quantity = 0
            cart_data[line_key] = 0
            cart_quantities_changed = True

        bulk_price = (
            assignment.effective_bulk_price_for_customer(is_b2b=is_b2b)
            if is_bulk_order
            else None
        )
        unit = (
            assignment.bulk_unit_for_customer(is_b2b=is_b2b)
            if is_bulk_order
            else assignment.unit_for_customer(is_b2b=is_b2b)
        )
        if is_bulk_order and (bulk_price is None or not unit):
            unavailable_cart_lines.append(line_key)
            continue
        price = bulk_price if is_bulk_order else assignment.effective_price_for_customer(is_b2b=is_b2b)
        total = price * quantity
        subtotal += total
        items.append({
            "assignment": assignment,
            "quantity": quantity,
            "available_for_delivery": available_for_delivery,
            "is_scheduled_delivery": is_scheduled_delivery,
            "availability_label": (
                ""
                if available_for_delivery
                else "Not available in this delivery location"
            ),
            "price": price,
            "unit": unit,
            "total_unit": _quantity_unit_display(quantity, unit),
            "quantity_unit": (
                _quantity_unit_display(quantity, unit)
                if re.match(r"^\s*\d+(?:\.\d+)?\s*\D", unit or "")
                else f"{quantity} {unit}".strip()
            ),
            "unit_calculation": _quantity_unit_calculation(quantity, unit),
            "bulk_minimum_unit": _quantity_unit_display(
                assignment.bulk_min_quantity,
                unit,
            ) if is_bulk_order else unit,
            "is_bulk_order": is_bulk_order,
            "line_key": line_key,
            "bulk_min_quantity": assignment.bulk_min_quantity if is_bulk_order else 1,
            "bulk_minimum_satisfied": (
                not is_bulk_order
                or quantity == 0
                or quantity >= assignment.bulk_min_quantity
            ),
            "max_quantity": max(
                0,
                available_stock
                - _cart_quantity(cart_data, assignment.pk, is_bulk_order=not is_bulk_order),
            ),
            "total": total,
        })

    if unavailable_cart_lines or cart_quantities_changed:
        for line_key in unavailable_cart_lines:
            cart_data.pop(line_key, None)
        _save_address_scoped_cart(request, cart_data)
    if unavailable_cart_lines:
        messages.warning(
            request,
            "Unavailable cart offer(s) were removed. Review your remaining cart items.",
        )

    items.sort(key=lambda item: (
        item["available_for_delivery"],
        not item["is_scheduled_delivery"],
    ))

    saved_baskets = (
        SavedBasket.objects.filter(user=request.user)
        .annotate(item_count=Count("items"))
        .prefetch_related("items")
    )
    cart_shop_delivery_fees = {}
    for item in items:
        if item["quantity"] <= 0:
            continue
        shop = item["assignment"].shop
        cart_shop_delivery_fees[shop.pk] = {
            "shop_name": shop.name,
            "delivery_fee": shop.delivery_fee,
            "is_scheduled_delivery": (
                shop.allow_scheduled_delivery
                and not is_shop_currently_open(shop)
            ),
        }
    cart_shop_summary = list(cart_shop_delivery_fees.values())
    delivery_charge = sum(
        (row["delivery_fee"] for row in cart_shop_summary),
        Decimal("0.00"),
    )

    grand_total = (
        subtotal +
        delivery_charge
    )

    return render(
        request,
        "market/cart.html",
        {
            "items": items,
            "subtotal": subtotal,
            "delivery_charge": delivery_charge,
            "grand_total": grand_total,
            "cart_shop_summary": cart_shop_summary,
            "active_item_count": sum(item["quantity"] for item in items),
            "saved_baskets": saved_baskets,
            "is_b2b_customer": is_b2b,
            "delivery_addresses": (
                client.delivery_addresses.filter(is_active=True).order_by("-is_default", "id")
                if client
                else []
            ),
            "active_delivery_address": active_delivery_address,
            "current_delivery_location": current_location,
        }
    )


@login_required
def cart_select_delivery_address(request):
    if request.method != "POST":
        return redirect("cart")

    client = get_object_or_404(ClientProfile, user=request.user)
    address = get_object_or_404(
        ClientDeliveryAddress,
        pk=request.POST.get("address_id"),
        client=client,
        is_active=True,
    )
    _switch_address_scoped_cart(request, address)
    return redirect("checkout" if request.POST.get("next") == "checkout" else "cart")


@login_required
@transaction.atomic
def cart_basket_save(request):
    if request.method != "POST":
        return redirect("cart")

    name = (request.POST.get("name") or "").strip()
    if not name or len(name) > 80:
        messages.error(request, "Enter a basket name of 1 to 80 characters.")
        return redirect("cart")
    if SavedBasket.objects.filter(user=request.user, name__iexact=name).exists():
        messages.error(request, "You already have a basket with that name.")
        return redirect("cart")

    cart_data = _address_scoped_cart(request)[0]
    quantities = {}
    for line_key, raw_quantity in cart_data.items():
        parsed = _parse_cart_line_key(line_key)
        if parsed is None:
            continue
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            quantities[parsed] = quantity
    if not quantities:
        messages.error(request, "Add at least one product to your cart before saving a basket.")
        return redirect("cart")

    assignments = (
        ProductShop.objects.select_related("product", "shop")
        .filter(
            pk__in={assignment_id for assignment_id, _ in quantities},
            is_active=True,
            product__is_active=True,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
    )
    is_b2b = _is_b2b_customer(request)
    basket = SavedBasket.objects.create(user=request.user, name=name)
    saved_items = []
    for assignment in assignments:
        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        if available_stock <= 0:
            continue
        for is_bulk_order in (False, True):
            if (assignment.pk, is_bulk_order) in quantities:
                saved_items.append(
                    SavedBasketItem(
                        basket=basket,
                        product_shop=assignment,
                        product_name=assignment.product.product_name,
                        shop_name=assignment.shop.name,
                        quantity=min(
                            quantities[(assignment.pk, is_bulk_order)],
                            available_stock,
                        ),
                        is_bulk_order=is_bulk_order,
                    )
                )
    if not saved_items:
        basket.delete()
        messages.error(request, "No available products in the cart could be saved.")
        return redirect("cart")

    SavedBasketItem.objects.bulk_create(saved_items)
    messages.success(request, f"Saved {len(saved_items)} product(s) in “{name}”.")
    return redirect("cart")


@login_required
@transaction.atomic
def cart_basket_add_item(request, product_shop_id):
    if request.method != "POST":
        return redirect("cart")

    basket = get_object_or_404(
        SavedBasket.objects.select_for_update(),
        pk=request.POST.get("basket_id"),
        user=request.user,
    )
    assignment = get_object_or_404(
        ProductShop.objects.select_related("product", "shop").filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        ),
        pk=product_shop_id,
        is_active=True,
        product__is_active=True,
    )
    is_b2b = _is_b2b_customer(request)
    available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
    if available_stock <= 0:
        messages.error(request, "This product is out of stock for your account.")
        return redirect("cart")

    cart = _address_scoped_cart(request)[0]
    is_bulk_order = request.POST.get("offer") == "bulk"
    line_key = _cart_line_key(assignment.pk, is_bulk_order=is_bulk_order)
    try:
        cart_quantity = max(0, int(cart.get(line_key, 0)))
    except (TypeError, ValueError):
        cart_quantity = 0
    if cart_quantity == 0:
        messages.error(request, "Set this product's cart quantity above zero before adding it to a basket.")
        return redirect("cart")
    if is_bulk_order:
        if (
            assignment.effective_bulk_price_for_customer(
                is_b2b=is_b2b,
            ) is None
            or cart_quantity < assignment.bulk_min_quantity
        ):
            messages.error(request, "This bulk offer is unavailable or below its minimum quantity.")
            return redirect("cart")

    basket_item, created = SavedBasketItem.objects.get_or_create(
        basket=basket,
        product_shop=assignment,
        is_bulk_order=is_bulk_order,
        defaults={
            "product_name": assignment.product.product_name,
            "shop_name": assignment.shop.name,
            "quantity": min(cart_quantity, available_stock),
        },
    )
    requested_basket_quantity = cart_quantity
    if not created:
        requested_basket_quantity = basket_item.quantity + cart_quantity
        basket_item.quantity = min(requested_basket_quantity, available_stock)
        basket_item.product_name = assignment.product.product_name
        basket_item.shop_name = assignment.shop.name
        basket_item.is_bulk_order = is_bulk_order
        basket_item.save(
            update_fields=[
                "quantity",
                "product_name",
                "shop_name",
                "is_bulk_order",
            ]
        )

    if requested_basket_quantity > available_stock:
        messages.warning(
            request,
            f"Basket quantity for {assignment.product.product_name} is limited to current stock ({available_stock}).",
        )
    else:
        messages.success(
            request,
            f"Added {assignment.product.product_name} × {cart_quantity} to “{basket.name}”.",
        )
    return redirect("cart")


@login_required
@transaction.atomic
def cart_basket_restore(request, basket_id):
    if request.method != "POST":
        return redirect("cart")
    basket = get_object_or_404(
        SavedBasket.objects.prefetch_related("items"),
        pk=basket_id,
        user=request.user,
    )
    cart = _address_scoped_cart(request)[0]

    is_b2b = _is_b2b_customer(request)
    unavailable = []
    added_count = 0
    for saved_item in basket.items.all():
        assignment = (
            ProductShop.objects.select_related("product", "shop")
            .filter(
                pk=saved_item.product_shop_id,
                is_active=True,
                product__is_active=True,
            )
            .filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
            .first()
            if saved_item.product_shop_id
            else None
        )
        if not assignment:
            unavailable.append(saved_item.product_name)
            continue
        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        if available_stock <= 0:
            unavailable.append(saved_item.product_name)
            continue
        is_bulk_order = saved_item.is_bulk_order
        if is_bulk_order and (
            assignment.effective_bulk_price_for_customer(
                is_b2b=is_b2b,
            ) is None
            or saved_item.quantity < assignment.bulk_min_quantity
        ):
            unavailable.append(saved_item.product_name)
            continue
        key = _cart_line_key(assignment.pk, is_bulk_order=is_bulk_order)
        try:
            current_quantity = max(0, int(cart.get(key, 0)))
        except (TypeError, ValueError):
            current_quantity = 0
        current_total = current_quantity + _cart_quantity(
            cart,
            assignment.pk,
            is_bulk_order=not is_bulk_order,
        )
        quantity_to_add = min(saved_item.quantity, max(0, available_stock - current_total))
        if is_bulk_order and 0 < quantity_to_add < assignment.bulk_min_quantity:
            quantity_to_add = 0
        if quantity_to_add:
            cart[key] = current_quantity + quantity_to_add
            added_count += quantity_to_add
        if quantity_to_add < saved_item.quantity:
            unavailable.append(
                f"{saved_item.product_name} (only {quantity_to_add} additional available)"
            )

    if added_count:
        _save_address_scoped_cart(request, cart)
        messages.success(
            request,
            f"Added {added_count} available item(s) from “{basket.name}” to your cart.",
        )
    if unavailable:
        messages.warning(
            request,
            "Some saved basket products are unavailable or limited: "
            + ", ".join(unavailable),
        )
    if not added_count and not unavailable:
        messages.info(request, "This basket has no products to add.")
    return redirect("cart")


@login_required
@transaction.atomic
def cart_basket_delete(request, basket_id):
    if request.method != "POST":
        return redirect("cart")
    basket = get_object_or_404(SavedBasket, pk=basket_id, user=request.user)
    basket.delete()
    messages.success(request, "Saved basket deleted.")
    return redirect("cart")

# @login_required
# def cart_update(request, product_shop_id):

#     if request.method != "POST":
#         return JsonResponse(
#             {"success": False},
#             status=405
#         )

#     item = get_object_or_404(
#         ProductShop,
#         id=product_shop_id,
#         is_active=True,
#         product__is_active=True
#     )

#     try:

#         quantity = int(
#             request.POST.get(
#                 "quantity",
#                 0
#             )
#         )

#     except (TypeError, ValueError):

#         quantity = 0

#     quantity = max(
#         0,
#         min(
#             quantity,
#             item.stock
#         )
#     )

#     cart = request.session.get(
#         "cart",
#         {}
#     )

#     key = str(product_shop_id)

#     if quantity <= 0:

#         cart.pop(
#             key,
#             None
#         )

#         quantity = 0

#     else:

#         cart[key] = quantity

#     request.session["cart"] = cart

#     request.session.modified = True

#     cart_count = sum(
#         int(q)
#         for q in cart.values()
#     )

#     return JsonResponse(
#         {
#             "success": True,
#             "quantity": quantity,
#             "cart_count": cart_count,
#         }
#     )


@login_required
def cart_update(request, product_shop_id):

    if request.method != "POST":
        return JsonResponse(
            {"success": False},
            status=405
        )

    if request.POST.get("offer", "retail") not in {"retail", "bulk"}:
        return JsonResponse({"success": False, "message": "Choose a valid product offer."}, status=400)

    item = get_object_or_404(
        ProductShop.objects.filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        ),
        id=product_shop_id,
        is_active=True,
        product__is_active=True
    )

    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(
            Shop,
            storefront_slug=storefront_slug,
            storefront_subscription_active=True,
        )
        if (
            not _storefront_is_active(storefront_shop)
            or item.shop_id != storefront_shop.pk
        ):
            raise Http404("This product is not available in this shop storefront.")

    try:
        quantity = int(
            request.POST.get("quantity", 0)
        )
    except (TypeError, ValueError):
        quantity = 0

    is_bulk_order = request.POST.get("offer") == "bulk"
    is_b2b = _is_b2b_customer(request)
    if is_bulk_order:
        if (
            item.effective_bulk_price_for_customer(is_b2b=is_b2b) is None
            or not item.bulk_unit_for_customer(is_b2b=is_b2b)
        ):
            return JsonResponse({"success": False, "message": "This bulk offer is no longer available."}, status=400)
        if quantity > 0 and quantity < item.bulk_min_quantity:
            return JsonResponse({
                "success": False,
                "message": f"Bulk orders require at least {item.bulk_min_quantity} {item.bulk_unit_for_customer(is_b2b=is_b2b)}.",
            }, status=400)

    available_stock = item.stock_for_customer(is_b2b=is_b2b)
    quantity = max(0, min(quantity, available_stock))

    cart, active_delivery_address = _address_scoped_cart(request)
    if quantity > 0 and not _shop_can_deliver_to_address(
        item.shop,
        active_delivery_address,
        current_location=_cart_current_location(request),
    ):
        return JsonResponse(
            {
                "success": False,
                "message": "This product is not available in your delivery location.",
            },
            status=400,
        )

    key = _cart_line_key(product_shop_id, is_bulk_order=is_bulk_order)
    other_quantity = _cart_quantity(cart, product_shop_id, is_bulk_order=not is_bulk_order)
    if quantity + other_quantity > available_stock:
        return JsonResponse({
            "success": False,
            "message": "The combined retail and bulk quantities exceed available stock.",
        }, status=400)

    cart[key] = quantity

    _save_address_scoped_cart(request, cart)

    cart_count = sum(
        max(0, int(q))
        for q in cart.values()
        if str(q).lstrip("-").isdigit()
    )

    # AJAX request → JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        is_b2b = _is_b2b_customer(request)
        current_line_price = (
            item.effective_bulk_price_for_customer(is_b2b=is_b2b)
            if is_bulk_order
            else item.effective_price_for_customer(is_b2b=is_b2b)
        )
        assignment_ids = {
            parsed[0]
            for line_key in cart
            if (parsed := _parse_cart_line_key(line_key)) is not None
        }
        assignments = {
            assignment.pk: assignment
            for assignment in ProductShop.objects.select_related(
                "product",
                "shop",
            ).filter(
                pk__in=assignment_ids,
                is_active=True,
                product__is_active=True,
            ).filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            )
        }
        subtotal = Decimal("0.00")
        active_item_count = 0
        delivery_shops = {}
        for line_key, raw_quantity in cart.items():
            parsed = _parse_cart_line_key(line_key)
            if parsed is None:
                continue
            assignment_id, line_is_bulk = parsed
            assignment = assignments.get(assignment_id)
            if assignment is None:
                continue
            try:
                line_quantity = max(
                    0,
                    min(
                        int(raw_quantity),
                        assignment.stock_for_customer(is_b2b=is_b2b),
                    ),
                )
            except (TypeError, ValueError):
                continue
            if not line_quantity:
                continue
            line_price = (
                assignment.effective_bulk_price_for_customer(is_b2b=is_b2b)
                if line_is_bulk
                else assignment.effective_price_for_customer(is_b2b=is_b2b)
            )
            if line_price is None:
                continue
            subtotal += line_price * line_quantity
            active_item_count += line_quantity
            delivery_shops[assignment.shop_id] = {
                "shop_name": assignment.shop.name,
                "delivery_fee": assignment.shop.delivery_fee,
            }

        delivery_charge = sum(
            (shop["delivery_fee"] for shop in delivery_shops.values()),
            Decimal("0.00"),
        )
        line_unit = (
            item.bulk_unit_for_customer(is_b2b=is_b2b)
            if is_bulk_order
            else item.unit_for_customer(is_b2b=is_b2b)
        )

        return JsonResponse({
            "success": True,
            "quantity": quantity,
            "cart_count": cart_count,
            "line_total": format(current_line_price * quantity, ".2f"),
            "unit_calculation": _quantity_unit_calculation(quantity, line_unit),
            "bulk_minimum_satisfied": (
                not is_bulk_order
                or quantity == 0
                or quantity >= item.bulk_min_quantity
            ),
            "active_item_count": active_item_count,
            "subtotal": format(subtotal, ".2f"),
            "delivery_charge": format(delivery_charge, ".2f"),
            "grand_total": format(subtotal + delivery_charge, ".2f"),
            "delivery_by_shop": [
                {
                    "shop_name": shop["shop_name"],
                    "delivery_fee": format(shop["delivery_fee"], ".2f"),
                }
                for shop in delivery_shops.values()
            ],
        })

    # Normal browser form → redirect back to cart
    return redirect("cart")
@login_required
def cart_remove(request, product_shop_id):

    if request.method != "POST":
        return redirect("cart")

    cart = _address_scoped_cart(request)[0]

    is_bulk_order = request.POST.get("offer") == "bulk"
    cart.pop(_cart_line_key(product_shop_id, is_bulk_order=is_bulk_order), None)

    _save_address_scoped_cart(request, cart)

    return redirect("cart")

from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    ClientProfile,
    ClientDeliveryAddress,
    ProductShop,
    Order,
    OrderItem,
)

from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import (
    ClientProfile,
    ClientDeliveryAddress,
    ProductShop,
    Order,
    ShopOrder,
    OrderItem,
    OrderStatusHistory,
)

def generate_shop_order_number(shop, now=None):
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    locked_shop = Shop.objects.select_for_update().get(pk=shop.pk)
    if locked_shop.daily_order_counter_date != local_now.date():
        locked_shop.daily_order_counter_date = local_now.date()
        locked_shop.daily_order_counter = 0
    locked_shop.daily_order_counter += 1
    locked_shop.save(update_fields=[
        "daily_order_counter_date",
        "daily_order_counter",
    ])
    shop_prefix = slugify(locked_shop.name).upper()[:50].rstrip("-")
    if not shop_prefix:
        shop_prefix = f"SHOP-{locked_shop.pk}"
    if any(
        slugify(name).upper()[:50].rstrip("-") == shop_prefix
        for name in Shop.objects.exclude(pk=locked_shop.pk).values_list("name", flat=True)
    ):
        shop_prefix = f"{shop_prefix}-{locked_shop.pk}"
    return (
        f"{shop_prefix}-ORD-{local_now:%d%m%Y-%H%M%S}-"
        f"{locked_shop.daily_order_counter}"
    )


def _generate_unique_tracking_code():
    for _attempt in range(100):
        tracking_code = secrets.token_hex(3).upper()
        if not Order.objects.filter(tracking_code=tracking_code).exists():
            return tracking_code
    raise RuntimeError("Could not allocate a unique six-character order tracking code.")


def _checkout_delivery_issues(items, address):
    if not address:
        return []

    shops = {item["assignment"].shop_id: item["assignment"].shop for item in items}
    return [
        shop.name
        for shop in shops.values()
        if not _shop_can_deliver_to_address(shop, address)
    ]


def _checkout_failure_response(request, message, *, redirect_to="checkout", status=400):
    messages.error(request, message)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {"success": False, "message": message},
            status=status,
        )
    return redirect(redirect_to)


def _shop_can_deliver_to_address(shop, address, *, current_location=None):
    location = (
        address.location
        if address and isinstance(address.location, dict)
        else current_location or {}
    )
    latitude = (
        address.latitude
        if address and address.latitude is not None
        else location.get("lat")
    )
    longitude = (
        address.longitude
        if address and address.longitude is not None
        else location.get("lng")
    )
    pincode = (
        address.pincode
        if address and address.pincode
        else location.get("pincode")
        or location.get("postal_code")
        or location.get("postcode")
        or location.get("zip")
    )
    has_customer_location = (
        latitude is not None
        and longitude is not None
    ) or bool(pincode)
    if not has_customer_location:
        return bool(shop.is_shop_open or shop.allow_scheduled_delivery)
    return shop_delivers_to_location(
            shop,
            lat=latitude,
            lng=longitude,
            pincode=pincode,
            allow_scheduled_delivery=shop.allow_scheduled_delivery,
        )


@login_required
def checkout(request):
    if request.GET.get("payment_error") == "razorpay_unavailable":
        messages.error(request, "Razorpay checkout could not load. Please try again or choose Cash on Delivery.")

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )
    is_b2b = _is_b2b_customer(request, client)

    cart_data, cart_delivery_address = _address_scoped_cart(request, client=client)

    if request.method == "POST":
        posted_checkout_key = request.POST.get("checkout_attempt_key", "")
        checkout_key = (
            posted_checkout_key
            if isinstance(posted_checkout_key, str)
            and re.fullmatch(r"[0-9a-f]{32}", posted_checkout_key)
            else request.session.get("checkout_attempt_key")
        )
        if isinstance(checkout_key, str) and re.fullmatch(r"[0-9a-f]{32}", checkout_key):
            completed_attempt = (
                CheckoutAttempt.objects
                .filter(
                    customer=request.user,
                    checkout_key=checkout_key,
                    order__isnull=False,
                )
                .select_related("order")
                .first()
            )
            if completed_attempt:
                _clear_address_scoped_carts(request, cart_delivery_address)
                request.session.pop("checkout_attempt_key", None)
                request.session.modified = True
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({
                        "success": True,
                        "message": "This order was already placed.",
                        "order_id": completed_attempt.order.order_id,
                        "redirect_url": reverse(
                            "order_list",
                        ),
                    })
                return redirect(
                    "order_list",
                )

    if not isinstance(cart_data, dict):
        cart_data = {}
    removed_empty_lines = False
    for line_key, raw_quantity in list(cart_data.items()):
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            cart_data.pop(line_key, None)
            removed_empty_lines = True
    if removed_empty_lines:
        _save_address_scoped_cart(request, cart_data)

    if not cart_data:
        messages.warning(
            request,
            "Your cart is empty."
        )
        return redirect("marketplace")

    parsed_lines = {
        key: parsed
        for key in cart_data
        if (parsed := _parse_cart_line_key(key)) is not None
    }
    assignment_ids = {parsed[0] for parsed in parsed_lines.values()}

    assignments = (
        ProductShop.objects
        .select_related(
            "product",
            "shop"
        )
        .filter(
            id__in=assignment_ids,
            product__is_active=True,
            is_active=True,
        )
        .filter(
            Q(product__expiry_management_enabled=False)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=timezone.now())
        )
    )
    storefront_slug = _storefront_slug_from_host(request)
    if storefront_slug:
        storefront_shop = get_object_or_404(Shop, storefront_slug=storefront_slug)
        if not _storefront_is_active(storefront_shop):
            return public_shop_storefront(request, storefront_slug)
        if assignments.exclude(shop_id=storefront_shop.pk).exists():
            messages.error(
                request,
                "This shop's checkout only accepts products from its own catalog. Review your complete cart on SellerHelper.",
            )
            return redirect(_storefront_main_site_url(request, reverse("cart")))

    items = []
    subtotal = Decimal("0.00")

    assignments_by_id = {assignment.pk: assignment for assignment in assignments}
    for line_key, (assignment_id, is_bulk_order) in parsed_lines.items():
        assignment = assignments_by_id.get(assignment_id)
        if assignment is None:
            continue
        try:
            quantity = int(cart_data.get(line_key, 0))
        except (TypeError, ValueError):
            quantity = 0

        if quantity <= 0:
            continue

        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        if available_stock < quantity:

            messages.error(
                request,
                f"{assignment.product.product_name} has only "
                f"{available_stock} item(s) available."
            )

            return redirect("cart")

        if is_bulk_order:
            if quantity < assignment.bulk_min_quantity:
                messages.error(
                    request,
                    f"Bulk orders for {assignment.product.product_name} require at least "
                    f"{assignment.bulk_min_quantity} {assignment.bulk_unit_for_customer(is_b2b=is_b2b)}.",
                )
                return redirect("cart")
            price = assignment.effective_bulk_price_for_customer(is_b2b=is_b2b)
            unit = assignment.bulk_unit_for_customer(is_b2b=is_b2b)
            if price is None or not unit:
                messages.error(request, f"The bulk offer for {assignment.product.product_name} is no longer available.")
                return redirect("cart")
        else:
            price = assignment.effective_price_for_customer(is_b2b=is_b2b)
            unit = assignment.unit_for_customer(is_b2b=is_b2b)

        total = price * quantity

        subtotal += total

        items.append({
            "assignment": assignment,
            "quantity": quantity,
            "price": price,
            "unit": unit,
            "quantity_unit": (
                _quantity_unit_display(quantity, unit)
                if re.match(r"^\s*\d+(?:\.\d+)?\s*\D", unit or "")
                else f"{quantity} {unit}".strip()
            ),
            "unit_calculation": _quantity_unit_calculation(quantity, unit),
            "is_bulk_order": is_bulk_order,
            "line_key": line_key,
            "bulk_min_quantity": assignment.bulk_min_quantity if is_bulk_order else 1,
            "total": total,
        })

    if not items:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"success": False, "message": "Your cart is empty. Add items and try again."},
                status=400,
            )
        messages.warning(request, "Your cart is empty.")
        return redirect("marketplace")

    scheduled_delivery_shops = {}
    for item in items:
        shop = item["assignment"].shop
        item["is_scheduled_delivery"] = (
            shop.allow_scheduled_delivery
            and not is_shop_currently_open(shop)
        )
        if item["is_scheduled_delivery"]:
            scheduled_delivery_shops[shop.pk] = shop.name
    scheduled_delivery_shop_names = list(scheduled_delivery_shops.values())
    checkout_shops = sorted(
        {
            item["assignment"].shop_id: item["assignment"].shop
            for item in items
        }.values(),
        key=lambda shop: shop.name.casefold(),
    )
    checkout_shop_ids = {shop.pk for shop in checkout_shops}
    order_instruction_drafts = _checkout_order_instruction_drafts(
        request,
        checkout_shop_ids,
    )
    checkout_shop_instructions = [
        {
            "shop": shop,
            "shop_instruction": order_instruction_drafts.get(
                shop.pk,
                {},
            ).get(OrderVoiceInstruction.RECIPIENT_SHOP),
            "rider_instruction": order_instruction_drafts.get(
                shop.pk,
                {},
            ).get(OrderVoiceInstruction.RECIPIENT_RIDER),
        }
        for shop in checkout_shops
    ]
    checkout_rider_instruction = next(
        (
            shop_instruction["rider_instruction"]
            for shop_instruction in checkout_shop_instructions
            if shop_instruction["rider_instruction"]
        ),
        None,
    )

    quantities_by_assignment = {}
    for item in items:
        assignment_id = item["assignment"].pk
        quantities_by_assignment[assignment_id] = (
            quantities_by_assignment.get(assignment_id, 0) + item["quantity"]
        )
    for item in items:
        assignment = item["assignment"]
        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        if quantities_by_assignment[assignment.pk] > available_stock:
            return _checkout_failure_response(
                request,
                f"Combined retail and bulk quantities for {assignment.product.product_name} "
                f"exceed available stock ({available_stock}).",
                redirect_to="cart",
                status=409,
            )

    _track_checkout_attempt(request, items)

    addresses = (
        client.delivery_addresses
        .filter(
            is_active=True
        )
        .order_by(
            "-is_default",
            "id"
        )
    )

    selected_address = (
        addresses.filter(pk=cart_delivery_address.pk).first()
        if cart_delivery_address
        else None
    ) or addresses.filter(is_default=True).first() or addresses.first()
    if request.method == "POST":
        address_id = request.POST.get(
            "address"
        )

        if not address_id:
            return _checkout_failure_response(
                request,
                "Please select a delivery address.",
            )

        address = get_object_or_404(
            ClientDeliveryAddress,
            id=address_id,
            client=client,
            is_active=True,
        )
        if cart_delivery_address is None or address.pk != cart_delivery_address.pk:
            _switch_address_scoped_cart(request, address)
            message = (
                "This address has its own cart. Review the items and total for the selected "
                "address before placing the order."
            )
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {"success": False, "message": message},
                    status=409,
                )
            messages.info(
                request,
                message,
            )
            return redirect("checkout")

        delivery_issues = _checkout_delivery_issues(items, address)
        if delivery_issues:
            return _checkout_failure_response(
                request,
                "Delivery is unavailable at this address from: "
                + ", ".join(delivery_issues)
                + ". Change the delivery address or remove those shops' items.",
                status=409,
            )

        buyer_gstin = (
            address.gstin
            or (client.gstin if is_b2b else "")
        )
        try:
            order_instruction_drafts = _save_checkout_order_instruction(
                request,
                checkout_shops,
            )
        except ValueError as error:
            return _checkout_failure_response(request, str(error))

        coupon_code = request.POST.get("coupon_code", "").strip()
        payment_method = request.POST.get("payment_method", "COD").upper()
        if payment_method == "RAZORPAY":
            coupon = None
            coupon_discount = Decimal("0.00")
            if coupon_code:
                coupon = Coupon.objects.filter(
                    code__iexact=coupon_code,
                    is_active=True,
                    valid_from__lte=timezone.now(),
                    valid_until__gte=timezone.now(),
                ).first()
                if not coupon or subtotal < coupon.minimum_order_amount:
                    return _checkout_failure_response(
                        request,
                        "This coupon is invalid or does not meet the minimum order amount.",
                    )
                if coupon.discount_type == "PERCENT":
                    coupon_discount = subtotal * coupon.discount_value / Decimal("100")
                    if coupon.maximum_discount is not None:
                        coupon_discount = min(coupon_discount, coupon.maximum_discount)
                else:
                    coupon_discount = coupon.discount_value
                coupon_discount = min(coupon_discount, subtotal)

            try:
                gst_breakdown = _build_gst_breakdown(
                    items,
                    address,
                    coupon_discount,
                    require_state=True,
                    buyer_gstin_override=buyer_gstin,
                )
            except ValueError as error:
                return _checkout_failure_response(request, str(error))
            checkout_extra = gst_breakdown["total_gst"] + gst_breakdown["delivery_total"]
            no_wallet_charges = _checkout_charge_totals(
                subtotal,
                coupon_discount,
                Decimal("0.00"),
                "RAZORPAY",
                checkout_extra,
            )
            wallet_limit = max(
                Decimal("0.00"),
                no_wallet_charges["amount_due"] - no_wallet_charges["razorpay_fee"],
            )
            wallet_amount_used = (
                min(client.wallet_balance, wallet_limit)
                if _checkout_uses_wallet_balance(request)
                else Decimal("0.00")
            )
            charges = _checkout_charge_totals(
                subtotal,
                coupon_discount,
                wallet_amount_used,
                "RAZORPAY",
                checkout_extra,
            )
            payable_total = charges["amount_due"]
            if payable_total == Decimal("0.00"):
                return create_order(
                    request=request,
                    client=client,
                    address=address,
                    items=items,
                    subtotal=subtotal,
                    coupon_code=coupon_code,
                    payment_type="RAZORPAY",
                    wallet_amount_override=wallet_amount_used,
                    coupon_discount_override=coupon_discount,
                    platform_charge_override=charges["platform_charge"],
                    razorpay_fee_override=charges["razorpay_fee"],
                    checkout_extra_override=checkout_extra,
                    buyer_gstin_override=buyer_gstin,
                )
            if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
                    return _checkout_failure_response(
                        request,
                        "Online payment is not configured. Please choose Cash on Delivery or contact the shop.",
                    )

            amount_paise = int(
                (payable_total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            try:
                response = requests.post(
                    "https://api.razorpay.com/v1/orders",
                    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                    json={
                        "amount": amount_paise,
                        "currency": "INR",
                        "receipt": f"checkout_{uuid.uuid4().hex[:32]}",
                        "notes": {"customer_id": str(request.user.pk)},
                    },
                    timeout=15,
                )
                response.raise_for_status()
                razorpay_order = response.json()
                if not isinstance(razorpay_order, dict):
                    raise ValueError("Razorpay returned an invalid order response.")
                razorpay_order_id = razorpay_order.get("id")
                if not razorpay_order_id or razorpay_order.get("amount") != amount_paise:
                    raise ValueError("Razorpay returned an invalid order response.")
            except (requests.RequestException, ValueError):
                logger.exception("Could not create Razorpay checkout order for customer %s.", request.user.pk)
                return _checkout_failure_response(
                    request,
                    "Could not start online payment. Please try again or choose Cash on Delivery.",
                )

            request.session["razorpay_checkout_intent"] = {
                "user_id": request.user.pk,
                "razorpay_order_id": razorpay_order_id,
                "amount_paise": amount_paise,
                "order_instruction_draft_ids": (
                    {
                        str(shop_id): {
                            recipient: draft.pk
                            for recipient, draft in recipient_drafts.items()
                        }
                        for shop_id, recipient_drafts in order_instruction_drafts.items()
                    }
                ),
                "address_id": address.pk,
                "coupon_code": coupon_code,
                "coupon_discount": str(coupon_discount),
                "wallet_amount_used": str(wallet_amount_used),
                "platform_charge": str(charges["platform_charge"]),
                "razorpay_fee": str(charges["razorpay_fee"]),
                "gst_amount": str(gst_breakdown["total_gst"]),
                "delivery_amount": str(gst_breakdown["delivery_total"]),
                "buyer_gstin": buyer_gstin,
                "is_b2b_customer": is_b2b,
                "checkout_extra": str(checkout_extra),
                "subtotal": str(subtotal),
                "items": [
                    {
                        "product_shop_id": item["assignment"].pk,
                        "quantity": item["quantity"],
                        "price": str(item["price"]),
                        "unit": item["unit"],
                        "is_bulk_order": item["is_bulk_order"],
                    }
                    for item in items
                ],
            }
            request.session.modified = True
            return render(
                request,
                "market/checkout.html",
                {
                    "client": client,
                    "items": items,
                    "subtotal": subtotal,
                    "addresses": addresses,
                    "selected_address": address,
                    "delivery_issues": [],
                    "razorpay_key_id": settings.RAZORPAY_KEY_ID,
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_amount": amount_paise,
                    "razorpay_name": client.name or request.user.get_username(),
                    "razorpay_email": request.user.email,
                    "razorpay_phone": client.contact_no,
                    "razorpay_checkout": True,
                    "use_wallet_balance": _checkout_uses_wallet_balance(request),
                    "cod_base": no_wallet_charges["amount_due"],
                    "razorpay_base": (
                        no_wallet_charges["amount_due"]
                        - no_wallet_charges["razorpay_fee"]
                    ),
                    "razorpay_fee_percent": settings.RAZORPAY_CUSTOMER_FEE_PERCENT,
                    "platform_charge": charges["platform_charge"],
                    "razorpay_fee": charges["razorpay_fee"],
                    "checkout_total": charges["amount_due"],
                    "cod_due": charges["amount_due"] - charges["razorpay_fee"],
                    "razorpay_due": charges["amount_due"],
                    "wallet_amount_used": wallet_amount_used,
                    "gst_breakdown": gst_breakdown,
                    "gst_amount": gst_breakdown["total_gst"],
                    "delivery_total": gst_breakdown["delivery_total"],
                    "checkout_delivery_address": address,
                    "is_b2b_customer": is_b2b,
                    "buyer_gstin": buyer_gstin,
                    "checkout_shop_summary": _checkout_shop_summary(
                        items,
                        gst_breakdown,
                    ),
                    "scheduled_delivery_shop_names": scheduled_delivery_shop_names,
                    "checkout_shops": checkout_shops,
                    "checkout_shop_instructions": checkout_shop_instructions,
                    "checkout_rider_instruction": checkout_rider_instruction,
                },
            )
        if payment_method != "COD":
            return _checkout_failure_response(
                request,
                "Choose a valid payment method.",
            )

        return create_order(
            request=request,
            client=client,
            address=address,
            items=items,
            subtotal=subtotal,
            coupon_code=coupon_code,
            payment_type="COD",
            use_wallet_balance=_checkout_uses_wallet_balance(request),
            buyer_gstin_override=buyer_gstin,
        )

    checkout_address = selected_address
    buyer_gstin = (
        checkout_address.gstin
        if checkout_address and checkout_address.gstin
        else client.gstin if is_b2b else ""
    )
    gst_preview = _build_gst_breakdown(
        items,
        checkout_address,
        buyer_gstin_override=buyer_gstin,
    )
    delivery_gst_amount = sum(
        (
            row["delivery_gst_amount"]
            for row in gst_preview["shops"].values()
        ),
        Decimal("0.00"),
    )
    checkout_extra = gst_preview["total_gst"] + gst_preview["delivery_total"]
    cod_without_wallet = _checkout_charge_totals(
        subtotal,
        Decimal("0.00"),
        Decimal("0.00"),
        "COD",
        checkout_extra,
    )
    razorpay_without_wallet = _checkout_charge_totals(
        subtotal,
        Decimal("0.00"),
        Decimal("0.00"),
        "RAZORPAY",
        checkout_extra,
    )
    wallet_limit = max(
        Decimal("0.00"),
        cod_without_wallet["amount_due"],
    )
    wallet_amount_used = min(client.wallet_balance, wallet_limit)
    checkout_charges = _checkout_charge_totals(
        subtotal,
        Decimal("0.00"),
        wallet_amount_used,
        "COD",
        checkout_extra,
    )
    razorpay_estimate = _checkout_charge_totals(
        subtotal,
        Decimal("0.00"),
        wallet_amount_used,
        "RAZORPAY",
        checkout_extra,
    )
    return render(
        request,
        "market/checkout.html",
        {
            "client": client,
            "items": items,
            "subtotal": subtotal,
            "addresses": addresses,
            "selected_address": selected_address,
            "delivery_issues": _checkout_delivery_issues(items, selected_address),
            "razorpay_configured": bool(
                settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET
            ),
            "platform_charge": checkout_charges["platform_charge"],
            "razorpay_fee": razorpay_estimate["razorpay_fee"],
            "cod_due": checkout_charges["amount_due"],
            "razorpay_due": razorpay_estimate["amount_due"],
            "cod_base": cod_without_wallet["amount_due"],
            "razorpay_base": (
                razorpay_without_wallet["amount_due"]
                - razorpay_without_wallet["razorpay_fee"]
            ),
            "razorpay_fee_percent": settings.RAZORPAY_CUSTOMER_FEE_PERCENT,
            "use_wallet_balance": True,
            "is_b2b_customer": is_b2b,
            "buyer_gstin": buyer_gstin,
            "wallet_amount_used": wallet_amount_used,
            "gst_amount": gst_preview["total_gst"] - delivery_gst_amount,
            "delivery_gst_amount": delivery_gst_amount,
            "delivery_total": gst_preview["delivery_total"],
            "checkout_delivery_address": checkout_address,
            "checkout_shop_summary": _checkout_shop_summary(
                items,
                gst_preview,
            ),
            "scheduled_delivery_shop_names": scheduled_delivery_shop_names,
            "checkout_shops": checkout_shops,
            "checkout_shop_instructions": checkout_shop_instructions,
            "checkout_rider_instruction": checkout_rider_instruction,
        }
    )
@login_required
@transaction.atomic
def create_order(
    request,
    client,
    address,
    items,
    subtotal,
    coupon_code="",
    payment_type="COD",
    payment_id="",
    wallet_amount_override=None,
    coupon_discount_override=None,
    platform_charge_override=None,
    razorpay_fee_override=None,
    checkout_extra_override=None,
    use_wallet_balance=None,
    buyer_gstin_override=None,
):

    client = ClientProfile.objects.select_for_update().get(pk=client.pk)
    checkout_key = request.session.get("checkout_attempt_key")
    if isinstance(checkout_key, str) and re.fullmatch(r"[0-9a-f]{32}", checkout_key):
        completed_attempt = (
            CheckoutAttempt.objects
            .filter(
                customer=request.user,
                checkout_key=checkout_key,
                order__isnull=False,
            )
            .select_related("order")
            .first()
        )
        if completed_attempt:
            _clear_address_scoped_carts(request, address)
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                messages.info(
                    request,
                    f"Order {completed_attempt.order.order_id} was already placed.",
                )
                return JsonResponse({
                    "success": True,
                    "message": "This order was already placed.",
                    "order_id": completed_attempt.order.order_id,
                    "redirect_url": reverse(
                        "order_list",
                    ),
                })
            return redirect(
                "order_list",
            )

    _switch_address_scoped_cart(request, address)
    delivery_issues = _checkout_delivery_issues(items, address)
    if delivery_issues:
        return _checkout_failure_response(
            request,
            "Delivery is unavailable at this address from: "
            + ", ".join(delivery_issues)
            + ". Change the delivery address or remove those shops' items.",
            status=409,
        )

    is_b2b = _is_b2b_customer(request, client)
    coupon = None
    coupon_discount = Decimal("0.00")

    if coupon_code:
        coupon = Coupon.objects.filter(
            code__iexact=coupon_code,
            is_active=True,
            valid_from__lte=timezone.now(),
            valid_until__gte=timezone.now(),
        ).first()
        if not coupon or subtotal < coupon.minimum_order_amount:
            return _checkout_failure_response(
                request,
                "This coupon is invalid or does not meet the minimum order amount.",
            )
        if coupon_discount_override is None:
            if coupon.discount_type == "PERCENT":
                coupon_discount = subtotal * coupon.discount_value / Decimal("100")
                if coupon.maximum_discount is not None:
                    coupon_discount = min(coupon_discount, coupon.maximum_discount)
            else:
                coupon_discount = coupon.discount_value
            coupon_discount = min(coupon_discount, subtotal)
        else:
            coupon_discount = min(
                max(Decimal("0.00"), Decimal(str(coupon_discount_override))),
                subtotal,
            )
    elif coupon_discount_override not in (None, Decimal("0.00"), "0", "0.00"):
        return _checkout_failure_response(
            request,
            "The coupon used for this payment is no longer valid.",
        )

    discounted_total = max(Decimal("0.00"), subtotal - coupon_discount)
    try:
        gst_breakdown = _build_gst_breakdown(
            items,
            address,
            coupon_discount,
            require_state=True,
            buyer_gstin_override=buyer_gstin_override,
        )
    except ValueError as error:
        return _checkout_failure_response(request, str(error))
    checkout_extra = gst_breakdown["total_gst"] + gst_breakdown["delivery_total"]
    if checkout_extra_override is not None and (
        Decimal(str(checkout_extra_override)) != checkout_extra
    ):
        return _checkout_failure_response(
            request,
            "The tax or delivery total changed during checkout. Please review your updated order.",
            status=409,
        )
    no_wallet_charges = _checkout_charge_totals(
        subtotal,
        coupon_discount,
        Decimal("0.00"),
        payment_type,
        checkout_extra,
    )
    wallet_limit = max(
        Decimal("0.00"),
        no_wallet_charges["amount_due"] - no_wallet_charges["razorpay_fee"],
    )
    if wallet_amount_override is not None:
        requested_wallet_amount = max(
            Decimal("0.00"),
            Decimal(str(wallet_amount_override)),
        )
        if requested_wallet_amount > client.wallet_balance:
            return _checkout_failure_response(
                request,
                "Your wallet balance changed during checkout. Please review the updated total and try again.",
                status=409,
            )
        wallet_amount_used = min(
            requested_wallet_amount,
            wallet_limit,
        )
    elif use_wallet_balance is False:
        wallet_amount_used = Decimal("0.00")
    else:
        wallet_amount_used = min(client.wallet_balance, wallet_limit)
    if wallet_amount_used > client.wallet_balance:
        return _checkout_failure_response(
            request,
            "Your wallet balance changed during checkout. Please contact support about your payment before trying again.",
            status=409,
        )
    charges = _checkout_charge_totals(
        subtotal,
        coupon_discount,
        wallet_amount_used,
        payment_type,
        checkout_extra,
    )
    if platform_charge_override is not None and (
        Decimal(str(platform_charge_override)) != charges["platform_charge"]
    ):
        return _checkout_failure_response(
            request,
            "The platform charge changed during checkout. Please review the updated total.",
            status=409,
        )
    if razorpay_fee_override is not None and (
        Decimal(str(razorpay_fee_override)) != charges["razorpay_fee"]
    ):
        return _checkout_failure_response(
            request,
            "The online payment fee changed during checkout. Please review the updated total.",
            status=409,
        )
    payable_total = charges["amount_due"]

    requested_by_assignment = {}
    for item in items:
        assignment_id = item["assignment"].id
        requested_by_assignment[assignment_id] = (
            requested_by_assignment.get(assignment_id, 0) + item["quantity"]
        )

    locked_assignments = {
        assignment.id: assignment
        for assignment in (
            ProductShop.objects
            .select_for_update()
            .select_related("product", "shop")
            .filter(id__in=requested_by_assignment)
        )
    }
    for assignment_id, quantity in requested_by_assignment.items():
        assignment = locked_assignments.get(assignment_id)
        if assignment is None:
            return _checkout_failure_response(
                request,
                "A product in your cart is no longer available. Review your cart and try again.",
                redirect_to="cart",
                status=409,
            )
        available_stock = assignment.stock_for_customer(is_b2b=is_b2b)
        if available_stock < quantity:
            return _checkout_failure_response(
                request,
                f"{assignment.product.product_name} "
                f"is no longer available in the requested quantity.",
                redirect_to="cart",
                status=409,
            )

    order_now = timezone.now()
    shops_in_order = {}
    for assignment in locked_assignments.values():
        shops_in_order.setdefault(assignment.shop_id, assignment.shop)
    shop_ids_in_order = list(shops_in_order)
    instruction_draft_ids = request.session.get(
        "order_voice_instruction_draft_ids",
        {},
    )
    if not isinstance(instruction_draft_ids, dict):
        instruction_draft_ids = {}
    if payment_id:
        payment_intent = request.session.get("razorpay_checkout_intent")
        if (
            isinstance(payment_intent, dict)
            and payment_intent.get("user_id") == request.user.pk
        ):
            intent_draft_ids = payment_intent.get(
                "order_instruction_draft_ids",
            )
            if isinstance(intent_draft_ids, dict):
                instruction_draft_ids = intent_draft_ids
    legacy_instruction_draft_id = request.session.get(
        "order_voice_instruction_draft_id",
    )
    if legacy_instruction_draft_id and not instruction_draft_ids:
        legacy_instruction = OrderVoiceInstruction.objects.filter(
            pk=legacy_instruction_draft_id,
            customer=request.user,
            shop_order__isnull=True,
        ).first()
        if legacy_instruction:
            instruction_draft_ids = {
                legacy_instruction.recipient: legacy_instruction.pk,
            }

    normalized_instruction_draft_ids, invalid_instruction_drafts = (
        _normalize_checkout_instruction_draft_ids(
            request,
            set(shops_in_order),
            instruction_draft_ids,
        )
    )
    if invalid_instruction_drafts:
        return _checkout_failure_response(
            request,
            "An order instruction could not be matched. Please review checkout and try again.",
        )

    instruction_drafts = {}
    for shop_id, recipient_ids in normalized_instruction_draft_ids.items():
        parsed_shop_id = int(shop_id)
        if parsed_shop_id not in shops_in_order:
            return _checkout_failure_response(
                request,
                "The selected shop instruction could not be matched to this order. Please review checkout and try again.",
                status=409,
            )
        for recipient, draft_id in recipient_ids.items():
            draft = (
                OrderVoiceInstruction.objects
                .select_for_update()
                .filter(
                    pk=draft_id,
                    customer=request.user,
                    shop_id=parsed_shop_id,
                    shop_order__isnull=True,
                    recipient=recipient,
                )
                .first()
            )
            if draft is None:
                return _checkout_failure_response(
                    request,
                    "The selected shop instruction could not be matched to this order. Please review checkout and try again.",
                    status=409,
                )
            instruction_drafts.setdefault(parsed_shop_id, {})[recipient] = draft
    shop_order_numbers = {
        shop_id: generate_shop_order_number(
            shops_in_order[shop_id],
            order_now,
        )
        for shop_id in sorted(shop_ids_in_order)
    }
    primary_shop_order_number = shop_order_numbers[shop_ids_in_order[0]]

    delivery_address = (
        getattr(
            address,
            "address",
            ""
        ) or ""
    )

    delivery_contact_no = (
        getattr(
            address,
            "contact_no",
            ""
        )
        or getattr(
            client,
            "contact_no",
            ""
        )
        or ""
    )

    location = getattr(
        address,
        "location",
        None
    )

    if isinstance(location, dict):

        lat = location.get("lat")
        lng = location.get("lng")

        if lat is not None and lng is not None:

            delivery_location = (
                f"{lat},{lng}"
            )

        else:

            delivery_location = str(
                location
            )

    else:

        delivery_location = str(
            location or ""
        )

    tracking_code = _generate_unique_tracking_code()
    order_id = f"{tracking_code}-{primary_shop_order_number}"
    order = Order.objects.create(

        order_id=order_id,
        tracking_code=tracking_code,
        is_b2b_order=is_b2b,

        order_time=order_now,

        user=(
            request.user.username
            or str(request.user.id)
        ),

        delivery_address=delivery_address,

        delivery_location=delivery_location,

        delivery_contact_no=delivery_contact_no,

        payment_amount=payable_total,

        payment_id=payment_id,
        payment_type=(
            "WALLET"
            if not payable_total and wallet_amount_used
            else payment_type
        ),

        is_payment_done=payable_total == Decimal("0.00") or payment_type == "RAZORPAY",
        payment_status=(
            "PAID"
            if payable_total == Decimal("0.00") or payment_type == "RAZORPAY"
            else "COD"
            if payment_type == "COD"
            else "PENDING"
        ),
        finally_received_amount=payable_total if payment_type == "RAZORPAY" else Decimal("0.00"),
        placed_location=location if isinstance(location, dict) else {},
        wallet_amount_used=wallet_amount_used,
        coupon_code=coupon.code if coupon else "",
        coupon_discount_amount=coupon_discount,
        platform_charge_amount=charges["platform_charge"],
        razorpay_fee_amount=charges["razorpay_fee"],
        final_billing_amount=(
            discounted_total
            + charges["platform_charge"]
            + charges["razorpay_fee"]
            + checkout_extra
        ),
        gst_amount=gst_breakdown["total_gst"],

        is_valid_order=True,

        status="PLACED",
    )
    # print('dfsdf2 ',order_id)
    if wallet_amount_used:
        client.wallet_balance -= wallet_amount_used
        client.save(update_fields=["wallet_balance", "updated_at"])
        ClientWalletTransaction.objects.create(
            client=client,
            transaction_type="DEBIT",
            amount=wallet_amount_used,
            balance_after=client.wallet_balance,
            reference=order.order_id,
            description="Wallet used for order payment.",
        )

    UserActivity.objects.create(
        user=request.user,
        event_type="ORDER_PLACED",
        path=request.path,
        location=location if isinstance(location, dict) else {},
        metadata={"order_id": order.order_id, "coupon": coupon_code},
    )

    shop_groups = {}

    for item in items:

        assignment = locked_assignments[
            item["assignment"].id
        ]

        shop_id = assignment.shop_id

        if shop_id not in shop_groups:

            shop_groups[shop_id] = {
                "shop": assignment.shop,
                "items": [],
                "total": Decimal("0.00"),
                "product_count": 0,
            }

        shop_groups[shop_id]["items"].append(
            item
        )

        shop_groups[shop_id]["total"] += (
            item["total"]
        )

        shop_groups[shop_id]["product_count"] += (
            item["quantity"]
        )
    # print('dfsdf2 ',shop_groups)
    # {10: {'shop': <Shop: Balakoo>, 'items': [{'assignment': <ProductShop: ProductShop object (16)>, 'quantity': 3, 'price': Decimal('14.00'), 'total': Decimal('42.00')}], 'total': Decimal('42.00'), 'product_count': 3}, 
    #  11: {'shop': <Shop: Bakaloo Dairy>, 'items': [{'assignment': <ProductShop: ProductShop object (17)>, 'quantity': 4, 'price': Decimal('36.00'), 'total': Decimal('144.00')}], 'total': Decimal('144.00'), 'product_count': 4}}
    # try:
    for shop_id, group in shop_groups.items():

        shop = group["shop"]

        shop_total = group["total"]

        product_count = group[
            "product_count"
        ]

        shop_location = getattr(
            shop,
            "location",
            ""
        ) or ""

        shop_delivery_area = getattr(
            shop,
            "delivery_area",
            ""
        ) or ""

        shop_delivery_pincode = getattr(
            shop,
            "delivery_pincode",
            ""
        ) or ""

        packing_minutes = max(5, 3 + product_count * 2)
        rider_eta_minutes = 0
        shop_location = _select_shop_pickup_location(shop, order)
        try:
            destination_lat, destination_lng = (
                float(value.strip())
                for value in delivery_location.split(",", 1)
            )
            shop_lat = float(shop_location["lat"])
            shop_lng = float(shop_location["lng"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            coordinates = (shop_lat, shop_lng, destination_lat, destination_lng)
            if (
                all(math.isfinite(value) for value in coordinates)
                and -90 <= shop_lat <= 90
                and -90 <= destination_lat <= 90
                and -180 <= shop_lng <= 180
                and -180 <= destination_lng <= 180
            ):
                distance_km = distance_km_between_points(
                    shop_lat,
                    shop_lng,
                    destination_lat,
                    destination_lng,
                )
                if distance_km is not None:
                    rider_eta_minutes = max(5, math.ceil(distance_km / 20 * 60) + 5)

        shop_order = ShopOrder.objects.create(

            order=order,

            shop=shop,
            pickup_location=_select_shop_pickup_location(shop, order),
            shop_order_number=shop_order_numbers[shop_id],
            is_scheduled_delivery=(
                shop.allow_scheduled_delivery
                and not is_shop_currently_open(shop)
            ),

            total_amount=shop_total,

            product_count=product_count,

            total_weight_kg=Decimal(
                "0.00"
            ),

            delivery_fee=gst_breakdown["shops"][shop_id]["delivery_fee"],
            invoice_number=(
                f"GST-{order.order_id}-{shop_id}"
                if shop.gstin
                else ""
            ),
            seller_gstin=gst_breakdown["shops"][shop_id]["seller_gstin"],
            buyer_gstin=gst_breakdown["shops"][shop_id]["buyer_gstin"],
            seller_billing_address=gst_breakdown["shops"][shop_id]["seller_billing_address"],
            seller_state_code=gst_breakdown["shops"][shop_id]["seller_state_code"],
            buyer_state_code=gst_breakdown["shops"][shop_id]["buyer_state_code"],
            delivery_hsn_code=gst_breakdown["shops"][shop_id]["delivery_hsn_code"],
            delivery_gst_rate=gst_breakdown["shops"][shop_id]["delivery_gst_rate"],
            taxable_amount=gst_breakdown["shops"][shop_id]["taxable_amount"],
            cgst_amount=gst_breakdown["shops"][shop_id]["cgst_amount"],
            sgst_amount=gst_breakdown["shops"][shop_id]["sgst_amount"],
            igst_amount=gst_breakdown["shops"][shop_id]["igst_amount"],
            delivery_gst_amount=gst_breakdown["shops"][shop_id]["delivery_gst_amount"],

            packing_minutes=packing_minutes,
            rider_eta_minutes=rider_eta_minutes,

            status="PLACED",
        )
        for instruction_draft in instruction_drafts.get(shop_id, {}).values():
            instruction_draft.shop_order = shop_order
            instruction_draft.save(update_fields=["shop_order"])

        shop_items_json = []
        estimated_shop_weight_kg = Decimal("0.000")

        for item in group["items"]:

            assignment = locked_assignments[
                item["assignment"].id
            ]

            product = assignment.product

            quantity = item["quantity"]

            price = item["price"]

            total = item["total"]
            unit = item.get("unit") or product.unit or ""
            estimated_weight = (
                _estimated_weight_for_quantity(quantity, unit)
                if product.is_approximate_weight
                else None
            )

            OrderItem.objects.create(

                shop_order=shop_order,

                product=product,

                product_shop=assignment,

                product_name=(
                    product.product_name
                ),

                sku=(
                    product.sku
                    or ""
                ),

                quantity=quantity,

                is_approximate_weight=product.is_approximate_weight,
                estimated_weight=estimated_weight,
                unit=unit,
                is_bulk_order=item.get("is_bulk_order", False),

                price=price,

                total=total,
                hsn_code=item.get("hsn_code", ""),
                gst_rate=item.get("gst_rate", Decimal("0.00")),
                taxable_amount=item.get("taxable_amount", total),
                cgst_amount=item.get("cgst_amount", Decimal("0.00")),
                sgst_amount=item.get("sgst_amount", Decimal("0.00")),
                igst_amount=item.get("igst_amount", Decimal("0.00")),
            )
            if estimated_weight is not None:
                estimated_shop_weight_kg += _weight_in_kilograms(
                    estimated_weight,
                    unit,
                )

            shop_items_json.append({

                "product_id": product.id,

                "product_shop_id": assignment.id,

                "product_name": (
                    product.product_name
                ),

                "product_title": (
                    product.product_title
                    or ""
                ),

                "sku": (
                    product.sku
                    or ""
                ),

                "quantity": quantity,

                "unit": unit,

                "is_bulk_order": item.get("is_bulk_order", False),

                "price": str(
                    price
                ),

                "total": str(
                    total
                ),

            })

            stock_field = "b2b_stock" if is_b2b else "stock"
            setattr(
                assignment,
                stock_field,
                assignment.stock_for_customer(is_b2b=is_b2b) - quantity,
            )
            assignment.save(update_fields=[stock_field])
            StockMovement.objects.create(
                product_shop=assignment,
                movement_type="SALE",
                quantity=-quantity,
                reference=order.order_id,
                created_by=request.user,
            )

        shop_order.items = (
            shop_items_json
        )
        shop_order.total_weight_kg = estimated_shop_weight_kg.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        shop_order.save(
            update_fields=[
                "items",
                "total_weight_kg",
            ]
        )

        OrderStatusHistory.objects.create(

            shop_order=shop_order,

            status="PLACED",

            remark=(
                (
                    "Order placed successfully. Delivery timing will be scheduled "
                    "with the shop because it is currently closed."
                )
                if shop_order.is_scheduled_delivery
                else "Order placed successfully."
            )
        )

        if shop_order.is_scheduled_delivery:
            Notification.objects.create(
                user=request.user,
                title="Delivery timing needs to be scheduled",
                message=(
                    f"Your order from {shop.name} was placed while the shop is closed. "
                    "The shop will confirm the delivery timing."
                ),
                notification_type="ORDER",
                related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
            )

        staff_user_ids = set(
            ShopStaff.objects.filter(shop=shop, is_active=True).values_list("user_id", flat=True)
        )
        if shop.owner_id:
            staff_user_ids.add(shop.owner_id)
        Notification.objects.bulk_create([
            Notification(
                user_id=user_id,
                title="New shop order received",
                message=f"Order {order.order_id} has been placed and needs shop-team preparation.",
                notification_type="ORDER",
                related_url=f"/shop/orders/{shop_order.id}/",
            )
            for user_id in staff_user_ids
        ])
    # except Exception as e:
    #     print(e)        
        # raise KeyError("adADFasf")
    # print("asdasdga")
    if len(shop_groups) == 1:

        shop = next(
            iter(
                shop_groups.values()
            )
        )["shop"]

        order.shop = shop

        order.shop_location = (
            getattr(
                shop,
                "location",
                ""
            ) or ""
        )

        order.shop_delivery_area = (
            getattr(
                shop,
                "delivery_area",
                ""
            ) or ""
        )

        order.shop_delivery_pincode = (
            getattr(
                shop,
                "delivery_pincode",
                ""
            ) or ""
        )

        order.save(
            update_fields=[
                "shop",
                "shop_location",
                "shop_delivery_area",
                "shop_delivery_pincode",
            ]
        )

    if checkout_key:
        CheckoutAttempt.objects.filter(
            customer=request.user,
            checkout_key=checkout_key,
            order__isnull=True,
        ).update(
            order=order,
            last_activity_at=timezone.now(),
        )
        request.session.pop("checkout_attempt_key", None)

    _clear_address_scoped_carts(request, address)
    request.session.pop("razorpay_checkout_intent", None)
    request.session.pop("order_voice_instruction_draft_ids", None)
    request.session.pop("order_voice_instruction_draft_id", None)

    request.session.modified = True

    messages.success(
        request,
        f"Order {order.order_id} placed successfully!"
    )
    Notification.objects.create(
        user=request.user,
        title="Order placed successfully",
        message=f"Order {order.order_id} has been placed successfully.",
        notification_type="ORDER",
        related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": f"Order {order.order_id} placed successfully!",
            "order_id": order.order_id,
            "redirect_url": reverse("order_list"),
        })

    return redirect("order_list")


@login_required
def verify_razorpay_payment(request):
    if request.method != "POST":
        return redirect("checkout")

    intent = request.session.get("razorpay_checkout_intent")
    razorpay_order_id = request.POST.get("razorpay_order_id", "")
    payment_id = request.POST.get("razorpay_payment_id", "")
    signature = request.POST.get("razorpay_signature", "")
    if (
        not isinstance(intent, dict)
        or intent.get("user_id") != request.user.pk
        or intent.get("razorpay_order_id") != razorpay_order_id
        or not payment_id.startswith("pay_")
        or not payment_id[4:].isalnum()
        or not signature
    ):
        messages.error(request, "Payment verification could not be matched to your checkout. Please contact support if your account was charged.")
        return redirect("checkout")

    signature_payload = f"{razorpay_order_id}|{payment_id}".encode()
    expected_signature = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        signature_payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        logger.warning("Invalid Razorpay payment signature for user %s.", request.user.pk)
        messages.error(request, "Online payment verification failed. Your order was not placed.")
        return redirect("checkout")

    try:
        payment_response = requests.get(
            f"https://api.razorpay.com/v1/payments/{payment_id}",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=15,
        )
        payment_response.raise_for_status()
        payment = payment_response.json()
        if not isinstance(payment, dict):
            raise ValueError("Razorpay returned an invalid payment response.")
        expected_amount = int(intent["amount_paise"])
        if (
            payment.get("order_id") != razorpay_order_id
            or payment.get("amount") != expected_amount
            or payment.get("currency") != "INR"
            or payment.get("status") != "captured"
        ):
            messages.error(request, "Razorpay has not confirmed a captured payment for this checkout. Your order was not placed.")
            return redirect("checkout")
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.exception("Could not confirm Razorpay payment for user %s.", request.user.pk)
        messages.error(request, "Could not confirm your online payment. Your order was not placed; please contact support if your account was charged.")
        return redirect("checkout")

    try:
        address = ClientDeliveryAddress.objects.get(
            pk=intent["address_id"],
            client__user=request.user,
            is_active=True,
        )
        client = ClientProfile.objects.get(user=request.user)
        cart_items = intent["items"]
        assignments = {
            assignment.pk: assignment
            for assignment in ProductShop.objects.filter(
                pk__in=[row["product_shop_id"] for row in cart_items],
                product__is_active=True,
                is_active=True,
            ).filter(
                Q(product__expiry_management_enabled=False)
                | Q(expires_at__isnull=True)
                | Q(expires_at__gt=timezone.now())
            ).select_related("product", "shop")
        }
        items = []
        for row in cart_items:
            assignment = assignments.get(row["product_shop_id"])
            quantity = int(row["quantity"])
            if not assignment or quantity <= 0:
                raise ValueError("An item from this checkout is no longer available.")
            is_bulk_order = bool(row.get("is_bulk_order", False))
            is_b2b = bool(intent.get("is_b2b_customer", False))
            unit = row.get("unit") or (
                assignment.bulk_unit_for_customer(is_b2b=is_b2b)
                if is_bulk_order
                else assignment.unit_for_customer(is_b2b=is_b2b)
            )
            if is_bulk_order and (
                quantity < assignment.bulk_min_quantity
                or assignment.effective_bulk_price_for_customer(is_b2b=is_b2b) is None
                or not unit
            ):
                raise ValueError("A bulk offer in this checkout is no longer available.")
            price = Decimal(row["price"])
            items.append({
                "assignment": assignment,
                "quantity": quantity,
                "price": price,
                "unit": unit,
                "is_bulk_order": is_bulk_order,
                "bulk_min_quantity": assignment.bulk_min_quantity if is_bulk_order else 1,
                "total": price * quantity,
            })
        subtotal = sum((item["total"] for item in items), Decimal("0.00"))
        if subtotal != Decimal(intent["subtotal"]):
            raise ValueError("Checkout item totals changed.")
        rebuilt_gst = _build_gst_breakdown(
            items,
            address,
            Decimal(intent["coupon_discount"]),
            require_state=True,
            buyer_gstin_override=intent.get("buyer_gstin", ""),
        )
        rebuilt_extra = rebuilt_gst["total_gst"] + rebuilt_gst["delivery_total"]
        if rebuilt_extra != Decimal(intent["checkout_extra"]):
            raise ValueError("Checkout tax and delivery totals changed.")
        expected_checkout_amount = (
            subtotal
            - Decimal(intent["coupon_discount"])
            - Decimal(intent["wallet_amount_used"])
            + Decimal(intent["platform_charge"])
            + Decimal(intent["razorpay_fee"])
            + Decimal(intent["checkout_extra"])
        )
        if int((expected_checkout_amount * 100).quantize(Decimal("1"))) != expected_amount:
            raise ValueError("Checkout amount does not match the captured payment.")
    except (ClientDeliveryAddress.DoesNotExist, ClientProfile.DoesNotExist, KeyError, TypeError, ValueError, InvalidOperation):
        logger.exception("Could not rebuild verified Razorpay checkout for user %s.", request.user.pk)
        messages.error(request, "Your payment was captured, but the checkout could not be restored. Contact support for a refund.")
        return redirect("checkout")

    create_response = create_order(
        request=request,
        client=client,
        address=address,
        items=items,
        subtotal=subtotal,
        coupon_code=intent["coupon_code"],
        payment_type="RAZORPAY",
        payment_id=payment_id,
        wallet_amount_override=Decimal(intent["wallet_amount_used"]),
        coupon_discount_override=Decimal(intent["coupon_discount"]),
        platform_charge_override=Decimal(intent["platform_charge"]),
        razorpay_fee_override=Decimal(intent["razorpay_fee"]),
        checkout_extra_override=Decimal(intent["checkout_extra"]),
        buyer_gstin_override=intent.get("buyer_gstin", ""),
    )
    if Order.objects.filter(user=request.user.username, payment_id=payment_id).exists():
        request.session.pop("razorpay_checkout_intent", None)
        request.session.modified = True
        return create_response

    try:
        refund_response = requests.post(
            f"https://api.razorpay.com/v1/payments/{payment_id}/refund",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            json={"amount": expected_amount},
            timeout=15,
        )
        refund_response.raise_for_status()
    except requests.RequestException:
        logger.exception("Could not refund captured payment after order creation failed for user %s.", request.user.pk)
        messages.error(request, "Payment was captured but the order could not be placed, and the automatic refund could not be confirmed. Contact support with your Razorpay payment ID.")
    else:
        request.session.pop("razorpay_checkout_intent", None)
        request.session.modified = True
        messages.error(request, "The order could not be placed because availability changed. Your payment refund has been initiated.")
    return create_response


# @login_required
# def order_tracking(request, order_id):

#     client = get_object_or_404(
#         ClientProfile,
#         user=request.user
#     )

#     order = get_object_or_404(
#         Order.objects.select_related(
#             "shop"
#         ).prefetch_related(
#             "shop_order__shop",
#             "shop_order__order_items__product",
#             "shop_order__order_items__product_shop",
#             "shop_order__status_history",
#         ),
#         order_id=order_id,
#     )

#     if order.user != request.user.username:
#         messages.error(
#             request,
#             "You are not authorized to view this order."
#         )
#         return redirect("marketplace")

#     shop_orders = (
#         ShopOrder.objects
#         .filter(order=order)
#         .select_related("shop")
#         .prefetch_related("order_items", "status_history")
#         .order_by("-created_at")
#     )

#     rider_otp = "----"
#     if shop_orders.exists():
#         assigned_rider = shop_orders.first().assigned_rider
#         if assigned_rider:
#             rider_otp = assigned_rider.verification_otp

#     customer_otp = client.verification_otp if client and client.verification_otp else "----"

#     return render(
#         request,
#         "market/order_tracking.html",
#         {
#             "client": client,
#             "order": order,
#             "shop_order": shop_orders.first(),
#             "shop_orders": shop_orders,
#             "customer_otp": customer_otp,
#             "rider_otp": rider_otp,
#         }
#     )

@login_required
def order_tracking(request, order_id):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    order = get_object_or_404(
        Order.objects.select_related("shop"),
        Q(tracking_code=order_id) | Q(order_id=order_id),
    )

    # Security check
    if order.user != request.user.username:
        messages.error(
            request,
            "You are not authorized to view this order."
        )
        return redirect("marketplace")

    # Get all ShopOrders belonging to this Order
    shop_orders = (
        ShopOrder.objects
        .filter(order=order)
        .select_related(
            "shop",
            "assigned_packer",
            "assigned_rider",
        )
        .prefetch_related(
            "order_items__product",
            "order_items__product_shop",
            "status_history",
        )
        .order_by("-created_at")
    )
    for tracked_shop_order in shop_orders:
        tracked_shop_order.pickup_location_data = _get_shop_order_pickup_location(
            tracked_shop_order
        )
        tracked_shop_order.customer_delivery_otp = (
            client.verification_otp
            if (
                tracked_shop_order.shop.require_customer_otp_verification
                and tracked_shop_order.status == "OUT_FOR_DELIVERY"
                and not tracked_shop_order.customer_otp_verified
            )
            else None
        )
        for item in tracked_shop_order.order_items.all():
            unit = item.unit or (item.product.unit if item.product_id else "")
            item.quantity_unit = _quantity_unit_total(item.quantity, unit)
            item.quantity_unit_calculation = _quantity_unit_calculation(
                item.quantity,
                unit,
            )
            if item.is_approximate_weight:
                item.weight_display_unit = _weight_display_unit(unit)
                item.estimated_weight_calculation = item.quantity_unit_calculation
        eta = _get_customer_eta(tracked_shop_order)
        tracked_shop_order.customer_timers = _get_customer_timers(
            tracked_shop_order,
            timezone.now(),
        )
    order.customer_location_sharing_active = (
        shop_orders.exclude(
            status__in=("DELIVERED", "COMPLETED", "CANCELLED", "RETURNED")
        )
        .filter(delivery_completed_at__isnull=True)
        .exists()
    )

    # First shop order
    first_shop_order = shop_orders.first()

    # Rider OTP
    rider_otp = "----"

    if first_shop_order:
        assigned_rider = first_shop_order.assigned_rider

        if assigned_rider:
            rider_otp = (
                assigned_rider.verification_otp
                or "----"
            )

    # Customer OTP
    customer_otp = (
        client.verification_otp
        if client and client.verification_otp
        else "----"
    )

    return render(
        request,
        "market/order_tracking.html",
        {
            "client": client,
            "order": order,

            "shop_order": first_shop_order,
            "shop_orders": shop_orders,

            "customer_otp": customer_otp,
            "rider_otp": rider_otp,
        },
    )


def _get_rider_route_delivery_targets(rider, shop, now=None):
    now = now or timezone.now()
    active_orders = list(
        ShopOrder.objects.filter(
            assigned_rider=rider,
            status="OUT_FOR_DELIVERY",
            delivery_completed_at__isnull=True,
        )
        .select_related("order", "shop")
        .prefetch_related("status_history")
        .order_by("created_at", "id")
    )
    if len(active_orders) < 2:
        return {}

    stops = []
    for active_order in active_orders:
        coordinates = _get_order_delivery_coordinates(active_order.order)
        if not coordinates:
            return {}
        history = list(active_order.status_history.all())
        delivery_started_at = next(
            (
                row.created_at
                for row in reversed(history)
                if row.status == "OUT_FOR_DELIVERY"
            ),
            active_order.created_at,
        )
        stops.append({
            "shop_order": active_order,
            "coordinates": coordinates,
            "started_at": delivery_started_at,
        })

    origin = next(
        (
            active_order.rider_last_location
            for active_order in active_orders
            if isinstance(active_order.rider_last_location, dict)
            and _valid_route_coordinates(active_order.rider_last_location)
        ),
        None,
    )
    if origin is None:
        origin = _get_shop_coordinates(shop)
    if not origin or not _valid_route_coordinates(origin):
        return {}

    remaining = list(stops)
    ordered_stops = []
    route_origin = {
        "lat": float(origin["lat"]),
        "lng": float(origin["lng"]),
    }
    current_location = route_origin
    while remaining:
        nearest = min(
            remaining,
            key=lambda stop: distance_km_between_points(
                current_location["lat"],
                current_location["lng"],
                stop["coordinates"]["lat"],
                stop["coordinates"]["lng"],
            ),
        )
        remaining.remove(nearest)
        ordered_stops.append(nearest)
        current_location = nearest["coordinates"]

    route_started_at = min(stop["started_at"] for stop in ordered_stops)
    elapsed_route_minutes = 0
    targets = {}
    current_location = route_origin
    for stop in ordered_stops:
        coordinates = stop["coordinates"]
        distance_km = distance_km_between_points(
            current_location["lat"],
            current_location["lng"],
            coordinates["lat"],
            coordinates["lng"],
        )
        if distance_km is None:
            return {}
        elapsed_route_minutes += math.ceil(distance_km / 20 * 60) + 5
        targets[stop["shop_order"].id] = route_started_at + timedelta(
            minutes=elapsed_route_minutes
        )
        current_location = coordinates
    return targets


def _valid_route_coordinates(location):
    try:
        latitude = float(location["lat"])
        longitude = float(location["lng"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _recent_customer_live_location(location):
    if not _valid_route_coordinates(location):
        return False
    try:
        observed_at = datetime.fromisoformat(
            str(location.get("observed_at", "")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    if timezone.is_naive(observed_at):
        observed_at = timezone.make_aware(observed_at)
    now = timezone.now()
    return now - timedelta(minutes=2) <= observed_at <= now + timedelta(seconds=30)


def _recent_public_rider_location(user):
    now_timestamp = timezone.now().timestamp()
    memberships = ShopStaff.objects.filter(
        user=user,
        is_active=True,
        is_public_rider=True,
        role="RIDER",
    ).values_list("last_location", flat=True)
    recent_locations = []
    for location in memberships:
        if not isinstance(location, dict) or not _valid_route_coordinates(location):
            continue
        try:
            observed_at = float(location.get("observed_at"))
        except (TypeError, ValueError):
            continue
        if 0 <= now_timestamp - observed_at <= 600:
            recent_locations.append({
                "lat": float(location["lat"]),
                "lng": float(location["lng"]),
                "observed_at": observed_at,
            })
    return max(
        recent_locations,
        key=lambda location: location["observed_at"],
        default=None,
    )


def _get_customer_eta(shop_order, now=None):
    now = now or timezone.now()
    history = list(
        shop_order.status_history.order_by("created_at").values_list("status", "created_at")
    )
    confirmed_at = next(
        (created_at for status, created_at in reversed(history) if status == "CONFIRMED"),
        None,
    )
    out_for_delivery_at = next(
        (created_at for status, created_at in reversed(history) if status == "OUT_FOR_DELIVERY"),
        None,
    )
    status = (shop_order.status or "").upper()

    route_targets = (
        _get_rider_route_delivery_targets(
            shop_order.assigned_rider,
            shop_order.shop,
            now,
        )
        if shop_order.assigned_rider_id
        and status == "OUT_FOR_DELIVERY"
        else {}
    )

    if status == "OUT_FOR_DELIVERY":
        delivery_started_at = out_for_delivery_at or shop_order.updated_at
        target = (
            route_targets.get(shop_order.id)
            or (
                delivery_started_at + timedelta(minutes=shop_order.rider_eta_minutes)
                if delivery_started_at
                else None
            )
        )
        mode = "delivery"
    elif status in {"CONFIRMED", "PACKING", "READY"}:
        target = (
            confirmed_at + timedelta(
                minutes=shop_order.packing_minutes + shop_order.rider_eta_minutes
            )
            if confirmed_at
            else None
        )
        mode = "full"
    else:
        target = None
        mode = ""

    remaining_seconds = (
        max(0, math.ceil((target - now).total_seconds()))
        if target
        else None
    )
    return {
        "target": target,
        "remaining_seconds": remaining_seconds,
        "mode": mode,
    }


def _format_eta_remaining(remaining_seconds):
    if remaining_seconds is None:
        return ""
    if remaining_seconds <= 0:
        return "Due now"
    hours, remainder = divmod(remaining_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


def _get_customer_timers(shop_order, now=None):
    now = now or timezone.now()
    status = (shop_order.status or "").upper()
    history = list(
        shop_order.status_history.order_by("created_at").values_list("status", "created_at")
    )

    def latest_status_time(target_status):
        return next(
            (created_at for history_status, created_at in reversed(history)
             if history_status == target_status),
            None,
        )

    def stage_metrics(start_status, end_statuses, estimate_minutes):
        start_index = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index][0] == start_status
            ),
            None,
        )
        estimate_seconds = max(1, estimate_minutes) * 60
        if start_index is None:
            return {
                "elapsed_seconds": None,
                "estimated_seconds": estimate_seconds,
                "elapsed_active": False,
            }
        started_at = history[start_index][1]
        finished_at = next(
            (
                created_at
                for history_status, created_at in history[start_index + 1:]
                if history_status in end_statuses
            ),
            None,
        )
        if finished_at is None and status in end_statuses:
            finished_at = (
                shop_order.delivery_completed_at
                or (
                    shop_order.updated_at
                    if status in {"DELIVERED", "COMPLETED", "CANCELLED"}
                    else now
                )
            )
        ended_at = min(now, finished_at) if finished_at else now
        return {
            "elapsed_seconds": max(
                0,
                math.floor((ended_at - started_at).total_seconds()),
            ),
            "estimated_seconds": estimate_seconds,
            "elapsed_active": finished_at is None,
        }

    packing_metrics = stage_metrics(
        "PACKING",
        {
            "READY",
            "RIDER_ASSIGNED",
            "PICKED_UP",
            "OUT_FOR_DELIVERY",
            "DELIVERED",
            "COMPLETED",
            "CANCELLED",
        },
        shop_order.packing_minutes,
    )
    delivery_metrics = stage_metrics(
        "OUT_FOR_DELIVERY",
        {"DELIVERED", "COMPLETED", "CANCELLED"},
        shop_order.rider_eta_minutes,
    )

    if status in {"CANCELLED", "DELIVERED", "COMPLETED"}:
        return {
            "packing": {
                "state": "terminal",
                "remaining_seconds": 0,
                **packing_metrics,
            },
            "delivery": {
                "state": "terminal",
                "remaining_seconds": 0,
                **delivery_metrics,
            },
        }

    packing_status_at = latest_status_time("PACKING")
    if status == "PACKING" and not packing_status_at:
        packing = {
            "state": "unavailable",
            "remaining_seconds": None,
            **packing_metrics,
        }
    elif status == "PACKING":
        packing_delta = (
            packing_status_at + timedelta(minutes=max(1, shop_order.packing_minutes)) - now
        ).total_seconds()
        packing_seconds = (
            math.ceil(packing_delta)
            if packing_delta > 0
            else -max(1, math.ceil(abs(packing_delta)))
        )
        packing = {
            "state": "active",
            "remaining_seconds": packing_seconds,
            **packing_metrics,
        }
    elif status in {"READY", "RIDER_ASSIGNED", "PICKED_UP", "OUT_FOR_DELIVERY"}:
        packing = {
            "state": "complete",
            "remaining_seconds": None,
            **packing_metrics,
        }
    else:
        packing = {
            "state": "waiting",
            "remaining_seconds": None,
            **packing_metrics,
        }

    delivery_status_at = latest_status_time("OUT_FOR_DELIVERY")
    if status == "OUT_FOR_DELIVERY" and not delivery_status_at:
        delivery = {
            "state": "unavailable",
            "remaining_seconds": None,
            **delivery_metrics,
        }
    elif status == "OUT_FOR_DELIVERY":
        route_targets = (
            _get_rider_route_delivery_targets(
                shop_order.assigned_rider,
                shop_order.shop,
                now,
            )
            if shop_order.assigned_rider_id
            else {}
        )
        delivery_target = route_targets.get(shop_order.id) or (
            delivery_status_at + timedelta(minutes=max(1, shop_order.rider_eta_minutes))
        )
        delivery_delta = (
            delivery_target - now
        ).total_seconds()
        delivery_seconds = (
            math.ceil(delivery_delta)
            if delivery_delta > 0
            else -max(1, math.ceil(abs(delivery_delta)))
        )
        delivery = {
            "state": "active",
            "remaining_seconds": delivery_seconds,
            **delivery_metrics,
        }
    else:
        delivery = {
            "state": "waiting",
            "remaining_seconds": None,
            **delivery_metrics,
        }

    return {"packing": packing, "delivery": delivery}


@login_required
@transaction.atomic
def order_tracking_updates(request, order_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    order = get_object_or_404(
        Order,
        (Q(tracking_code=order_id) | Q(order_id=order_id)),
        user=request.user.username,
    )
    now = timezone.now()
    updates = []
    shop_orders = (
        ShopOrder.objects.select_for_update()
        .filter(order=order)
        .select_related("shop")
        .prefetch_related("status_history")
    )
    for shop_order in shop_orders:
        eta = _get_customer_eta(shop_order, now)
        remaining_seconds = eta["remaining_seconds"]
        timers = _get_customer_timers(shop_order, now)
        reminder_minutes = shop_order.shop.delivery_eta_reminder_minutes
        if (
            remaining_seconds is not None
            and reminder_minutes > 0
            and 0 < remaining_seconds <= reminder_minutes * 60
            and shop_order.eta_reminder_sent_at is None
        ):
            Notification.objects.create(
                user=request.user,
                title="Your delivery is almost due",
                message=(
                    f"Your order from {shop_order.shop.name} is estimated to arrive "
                    f"within about {max(1, math.ceil(remaining_seconds / 60))} minutes."
                ),
                notification_type="ORDER",
                related_url=f"/orders/{order.tracking_code or order.order_id}/tracking/",
            )
            shop_order.eta_reminder_sent_at = now
            shop_order.save(update_fields=["eta_reminder_sent_at", "updated_at"])

        updates.append({
            "id": shop_order.id,
            "status": shop_order.status,
            "is_scheduled_delivery": shop_order.is_scheduled_delivery,
            "timers": timers,
        })

    return JsonResponse({"shop_orders": updates})


@login_required
@transaction.atomic
def shop_order_rider_location(request, shop_order_id):
    if request.method != "GET":
        return JsonResponse({"error": "GET required."}, status=405)
    shop_order = get_object_or_404(
        ShopOrder.objects.select_related("shop", "assigned_rider__user"),
        pk=shop_order_id,
    )
    is_assigned_rider = bool(
        shop_order.assigned_rider_id
        and shop_order.assigned_rider.user_id == request.user.pk
        and shop_order.assigned_rider.is_active
        and shop_order.assigned_rider.role == "RIDER"
    )
    accepted_by_rider = bool(
        shop_order.rider_accepted_at
        and shop_order.order.assigned_rider == request.user.username
    )
    approved_public_rider = (
        PublicRiderApplication.objects.filter(
            user=request.user,
            status=PublicRiderApplication.STATUS_APPROVED,
        ).exists()
        if accepted_by_rider
        else False
    )
    active_shop_rider = (
        ShopStaff.objects.filter(
            shop=shop_order.shop,
            user=request.user,
            is_active=True,
            role="RIDER",
        ).exists()
        if accepted_by_rider
        else False
    )
    is_accepted_rider = bool(
        accepted_by_rider
        and (approved_public_rider or active_shop_rider)
    )
    can_view_rider_location = is_assigned_rider or is_accepted_rider
    if not user_can_access_shop(request.user, shop_order.shop) and not can_view_rider_location:
        raise PermissionDenied
    location = shop_order.rider_last_location
    if not _valid_route_coordinates(location):
        location = None
    customer_location = (
        shop_order.order.customer_live_location
        if can_view_rider_location
        and shop_order.status == "OUT_FOR_DELIVERY"
        and _recent_customer_live_location(shop_order.order.customer_live_location)
        else None
    )
    rider_name = (
        shop_order.assigned_rider.user.get_full_name()
        or shop_order.assigned_rider.user.username
        if shop_order.assigned_rider_id
        else request.user.get_full_name() or request.user.username
        if is_accepted_rider
        else ""
    )
    return JsonResponse({
        "location": location,
        "customer_location": customer_location,
        "rider_name": rider_name,
        "updated_at": shop_order.updated_at.isoformat(),
    })


def _save_shop_order_rider_location(shop_order, latitude, longitude):
    shop_order.rider_last_location = {"lat": latitude, "lng": longitude}
    update_fields = ["rider_last_location", "updated_at"]
    distance_km = _order_delivery_distance_km(shop_order.order, latitude, longitude)
    radius_meters = shop_order.shop.rider_nearby_notification_meters
    if (
        distance_km is not None
        and distance_km * 1000 <= radius_meters
        and shop_order.rider_nearby_notified_at is None
    ):
        customer = get_user_model().objects.filter(username=shop_order.order.user).first()
        if customer:
            Notification.objects.create(
                user=customer,
                title="Your rider is nearby",
                message=(
                    f"Your rider is within {radius_meters} meters of your delivery location "
                    f"for order {shop_order.order.order_id}."
                ),
                notification_type="ORDER",
                related_url=(
                    f"/orders/{shop_order.order.tracking_code or shop_order.order.order_id}/tracking/"
                ),
            )
            shop_order.rider_nearby_notified_at = timezone.now()
            update_fields.append("rider_nearby_notified_at")
    shop_order.save(update_fields=update_fields)


@login_required
def update_rider_location(request, shop_order_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    shop_order = get_object_or_404(
        ShopOrder.objects.select_for_update().select_related(
            "shop",
            "order",
            "assigned_rider__user",
            "assigned_rider__shop",
        ),
        id=shop_order_id,
    )
    rider = _rider_membership_for_order(request.user, shop_order)
    if (
        not rider
        or not shop_order.assigned_rider_id
        or shop_order.assigned_rider.user_id != request.user.id
    ):
        raise PermissionDenied
    if shop_order.status != "OUT_FOR_DELIVERY":
        return JsonResponse({"error": "Location updates are only accepted during delivery."}, status=409)

    try:
        latitude = float(request.POST.get("latitude", ""))
        longitude = float(request.POST.get("longitude", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Valid latitude and longitude are required."}, status=400)
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return JsonResponse({"error": "Location coordinates are out of range."}, status=400)

    _save_shop_order_rider_location(shop_order, latitude, longitude)
    return JsonResponse({"success": True})


@login_required
def mobile_location_session(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if request.POST.get("consent") != "true":
        return JsonResponse({"error": "Location sharing consent is required."}, status=400)

    role = request.POST.get("role", "").strip().lower()
    order_ids = []
    destinations = []
    if role == "customer":
        order_reference = request.POST.get("order_id", "").strip()
        order = get_object_or_404(
            Order,
            Q(tracking_code=order_reference) | Q(order_id=order_reference),
            user=request.user.username,
            is_valid_order=True,
        )
        active_shop_orders = ShopOrder.objects.filter(order=order).exclude(
            status__in=("DELIVERED", "COMPLETED", "CANCELLED", "RETURNED")
        ).filter(delivery_completed_at__isnull=True)
        if not active_shop_orders.exists():
            return JsonResponse({"error": "This order is no longer active."}, status=409)
        order_ids = [order.pk]
    elif role == "rider":
        active_shop_orders = (
            ShopOrder.objects.filter(
                assigned_rider__user=request.user,
                assigned_rider__is_active=True,
                assigned_rider__role="RIDER",
                status="OUT_FOR_DELIVERY",
                delivery_completed_at__isnull=True,
            )
            .select_related("order")
            .order_by("pk")
        )
        order_ids = list(active_shop_orders.values_list("pk", flat=True))
        if not order_ids:
            return JsonResponse(
                {"error": "No active deliveries are assigned to this rider."},
                status=409,
            )
        destinations = [
            {
                "latitude": coordinates["lat"],
                "longitude": coordinates["lng"],
                "identifier": f"shop-order-{shop_order.pk}",
            }
            for shop_order in active_shop_orders
            if (
                coordinates := _get_order_delivery_coordinates(shop_order.order)
            )
        ]
        if not destinations:
            return JsonResponse(
                {"error": "Add delivery map coordinates before enabling nearby rider tracking."},
                status=409,
            )
    else:
        return JsonResponse({"error": "Choose a supported location-sharing role."}, status=400)

    token = signing.dumps(
        {
            "user_id": request.user.pk,
            "role": role,
            "order_ids": order_ids,
        },
        salt="sellerhelper.mobile-live-location",
        compress=True,
    )
    return JsonResponse(
        {
            "token": token,
            "role": role,
            "endpoint": request.build_absolute_uri(
                reverse("mobile_live_location_update")
            ),
            "destinations": destinations,
        }
    )


@csrf_exempt
def mobile_live_location_update(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    auth_scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if auth_scheme.lower() != "bearer" or not token:
        return JsonResponse({"error": "A valid location session is required."}, status=401)
    try:
        claims = signing.loads(
            token,
            salt="sellerhelper.mobile-live-location",
            max_age=24 * 60 * 60,
        )
    except signing.SignatureExpired:
        return JsonResponse({"error": "The location session has expired."}, status=401)
    except signing.BadSignature:
        return JsonResponse({"error": "The location session is invalid."}, status=401)

    user = get_user_model().objects.filter(
        pk=claims.get("user_id"),
        is_active=True,
    ).first()
    order_ids = claims.get("order_ids")
    role = claims.get("role")
    if not user or not isinstance(order_ids, list) or not order_ids:
        return JsonResponse({"error": "The location session is invalid."}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {"error": "A valid JSON request body is required."},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"error": "A JSON object is required."},
            status=400,
        )

    if role == "customer" and payload.get("stop") is True:
        Order.objects.filter(
            pk__in=order_ids,
            user=user.username,
        ).update(customer_live_location={}, updated_at=timezone.now())
        return JsonResponse({"success": True, "tracking_active": False})

    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (AttributeError, TypeError, ValueError):
        return JsonResponse(
            {"error": "Valid latitude and longitude are required."},
            status=400,
        )
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return JsonResponse({"error": "Location coordinates are out of range."}, status=400)

    observed_at = timezone.now()
    location = {
        "lat": latitude,
        "lng": longitude,
        "observed_at": observed_at.isoformat(),
    }
    if role == "customer":
        active_orders = Order.objects.filter(
            pk__in=order_ids,
            user=user.username,
            is_valid_order=True,
            shop_orders__delivery_completed_at__isnull=True,
        ).exclude(
            shop_orders__status__in=("DELIVERED", "COMPLETED", "CANCELLED", "RETURNED")
        ).distinct()
        active_order_ids = list(active_orders.values_list("pk", flat=True))
        active_orders.update(customer_live_location=location, updated_at=observed_at)
        tracking_active = bool(active_order_ids)
    elif role == "rider":
        active_shop_orders = (
            ShopOrder.objects.select_for_update()
            .filter(
                pk__in=order_ids,
                assigned_rider__user=user,
                assigned_rider__is_active=True,
                assigned_rider__role="RIDER",
                status="OUT_FOR_DELIVERY",
                delivery_completed_at__isnull=True,
            )
            .select_related("order", "shop", "assigned_rider")
        )
        active_shop_orders = list(active_shop_orders)
        for shop_order in active_shop_orders:
            _save_shop_order_rider_location(shop_order, latitude, longitude)
        tracking_active = bool(active_shop_orders)
    else:
        return JsonResponse({"error": "The location session is invalid."}, status=401)

    return JsonResponse(
        {
            "success": True,
            "tracking_active": tracking_active,
        }
    )


@login_required
def update_public_rider_location(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if request.session.get("active_portal") != "RIDER":
        raise PermissionDenied
    application = PublicRiderApplication.objects.filter(
        user=request.user,
        status=PublicRiderApplication.STATUS_APPROVED,
    ).first()
    if not application:
        raise PermissionDenied

    observed_at = timezone.now()
    try:
        latitude = float(request.POST.get("latitude", ""))
        longitude = float(request.POST.get("longitude", ""))
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "Valid latitude and longitude are required."},
            status=400,
        )
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return JsonResponse(
            {"error": "Location coordinates are out of range."},
            status=400,
        )

    location = {
        "lat": latitude,
        "lng": longitude,
        "observed_at": observed_at.timestamp(),
    }
    updated_memberships = ShopStaff.objects.filter(
        user=request.user,
        is_active=True,
        is_public_rider=True,
        role="RIDER",
    ).update(last_location=location)
    if not updated_memberships:
        return JsonResponse(
            {"error": "No active public rider network membership was found."},
            status=409,
        )
    return JsonResponse({"success": True})


def _order_delivery_distance_km(order, latitude, longitude):
    destination = order.placed_location if isinstance(order.placed_location, dict) else {}
    try:
        destination_latitude = float(destination["lat"])
        destination_longitude = float(destination["lng"])
    except (KeyError, TypeError, ValueError):
        try:
            destination_latitude, destination_longitude = (
                float(value.strip())
                for value in order.delivery_location.split(",", 1)
            )
        except (AttributeError, TypeError, ValueError):
            return None
    if (
        not math.isfinite(destination_latitude)
        or not math.isfinite(destination_longitude)
        or not -90 <= destination_latitude <= 90
        or not -180 <= destination_longitude <= 180
    ):
        return None
    return distance_km_between_points(
        latitude,
        longitude,
        destination_latitude,
        destination_longitude,
    )

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    ClientProfile,
    ClientDeliveryAddress,

)
from .forms import ClientDeliveryAddressForm

@login_required
def delivery_address_list(request):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    addresses = (
        ClientDeliveryAddress.objects
        .filter(client=client)
        .order_by("-is_default", "-id")
    )

    return render(
        request,
        "market/delivery_address_list.html",
        {
            "client": client,
            "addresses": addresses,
        }
    )

@login_required
def delivery_address_add1(request):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    if request.method == "POST":

        label = request.POST.get("label", "").strip()
        address = request.POST.get("address", "").strip()
        lat = request.POST.get("lat")
        lng = request.POST.get("lng")

        if not address:
            messages.error(
                request,
                "Please enter delivery address."
            )

            return redirect("delivery_address_add")

        location = {
            "lat": float(lat) if lat else None,
            "lng": float(lng) if lng else None,
        }

        ClientDeliveryAddress.objects.create(
            client=client,
            label=label,
            address=address,
            location=location,
            is_active=True,
            is_default=not client.delivery_addresses.filter(
                is_default=True
            ).exists(),
        )

        messages.success(
            request,
            "Delivery address added successfully."
        )

        return redirect("delivery_address_list")

    return render(
        request,
        "market/delivery_address_form.html",
        {
            "client": client,
            "form_title": "Add Delivery Address",
        }
    )

@login_required
def delivery_address_add(request):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    if request.method == "POST":

        form = ClientDeliveryAddressForm(
            request.POST
        )
        next_url = (
            request.POST.get("next")
            or request.GET.get("next")
            or ""
        ).strip()

        if form.is_valid():

            active_cart, _ = _address_scoped_cart(request)
            cart_snapshot = dict(active_cart)
            address = form.save(
                commit=False
            )

            address.client = client
            address.is_default = True

            lat = form.cleaned_data.get(
                "latitude"
            )

            lng = form.cleaned_data.get(
                "longitude"
            )

            city = form.cleaned_data.get(
                "city"
            )

            pincode = form.cleaned_data.get(
                "pincode"
            )

            address.location = {
                "lat": float(lat) if lat else None,
                "lng": float(lng) if lng else None,
                "city": city or "",
                "pincode": pincode or "",
            }

            with transaction.atomic():
                ClientDeliveryAddress.objects.filter(
                    client=client,
                    is_default=True,
                ).update(is_default=False)
                address.save()
            _switch_address_scoped_cart(request, address)
            _save_address_scoped_cart(request, cart_snapshot)

            messages.success(
                request,
                "Delivery address saved successfully."
            )

            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
            ):
                return redirect(next_url)
            return redirect("delivery_address_list")

    else:

        form = ClientDeliveryAddressForm()
        next_url = request.GET.get("next", "").strip()

    return render(
        request,
        "market/delivery_address_form.html",
        {
            "form": form,
            "next_url": next_url,
        }
    )


@login_required
@require_POST
def delivery_address_use_current_location(request):
    client = get_object_or_404(ClientProfile, user=request.user)
    try:
        latitude = Decimal(request.POST.get("latitude", ""))
        longitude = Decimal(request.POST.get("longitude", ""))
    except (InvalidOperation, TypeError, ValueError):
        return JsonResponse(
            {"success": False, "message": "Share a valid current location to continue."},
            status=400,
        )
    if (
        not latitude.is_finite()
        or not longitude.is_finite()
        or not Decimal("-90") <= latitude <= Decimal("90")
        or not Decimal("-180") <= longitude <= Decimal("180")
    ):
        return JsonResponse(
            {"success": False, "message": "The shared location is outside valid map coordinates."},
            status=400,
        )

    state_name = request.POST.get("state_name", "").strip()
    normalized_state_name = re.sub(
        r"\b(?:the|state of|union territory of)\b",
        "",
        state_name.casefold(),
    )
    normalized_state_name = re.sub(r"[^a-z]", "", normalized_state_name)
    state_aliases = {
        "nctdelhi": "07",
        "nationalcapitalterritorydelhi": "07",
        "orissa": "21",
        "pondicherry": "34",
        "uttaranchal": "05",
        "andhrapradesh": "37",
    }
    state_code = state_aliases.get(normalized_state_name)
    if not state_code:
        state_code = next(
            (
                code
                for code, name in INDIAN_GST_STATE_CHOICES
                if re.sub(r"[^a-z]", "", name.casefold()) == normalized_state_name
            ),
            "",
        )
    if not state_code:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Could not determine your delivery state from this location. "
                    "Add an address and select its state."
                ),
            },
            status=400,
        )

    with transaction.atomic():
        ClientDeliveryAddress.objects.filter(
            client=client,
            is_default=True,
        ).update(is_default=False)
        address = ClientDeliveryAddress.objects.create(
            client=client,
            label="Current location",
            address="Current device location",
            state_code=state_code,
            latitude=latitude,
            longitude=longitude,
            location={
                "lat": float(latitude),
                "lng": float(longitude),
            },
            is_active=True,
            is_default=True,
        )
    _switch_address_scoped_cart(request, address)
    return JsonResponse({
        "success": True,
        "redirect_url": reverse("checkout"),
    })

@login_required
def delivery_address_edit(request, pk):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    address = get_object_or_404(
        ClientDeliveryAddress,
        pk=pk,
        client=client
    )

    if request.method == "POST":

        form = ClientDeliveryAddressForm(
            request.POST,
            instance=address
        )

        if form.is_valid():

            address = form.save(
                commit=False
            )

            lat = form.cleaned_data.get(
                "latitude"
            )

            lng = form.cleaned_data.get(
                "longitude"
            )

            city = form.cleaned_data.get(
                "city"
            )

            pincode = form.cleaned_data.get(
                "pincode"
            )

            address.location = {
                "lat": float(lat) if lat else None,
                "lng": float(lng) if lng else None,
                "city": city or "",
                "pincode": pincode or "",
            }

            address.save()
            _zero_unavailable_address_cart_items(request, address)

            messages.success(
                request,
                "Delivery address updated successfully."
            )

            return redirect(
                "delivery_address_list"
            )

    else:

        form = ClientDeliveryAddressForm(
            instance=address
        )

    return render(
        request,
        "market/delivery_address_form.html",
        {
            "form": form,
            "object": address,
        }
    )

@login_required
def delivery_address_delete(request, pk):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    address_obj = get_object_or_404(
        ClientDeliveryAddress,
        pk=pk,
        client=client
    )

    if request.method == "POST":

        address_obj.delete()

        messages.success(
            request,
            "Delivery address deleted successfully."
        )

        return redirect("delivery_address_list")

    return render(
        request,
        "market/delivery_address_delete.html",
        {
            "address": address_obj,
        }
    )

@login_required
def delivery_address_default(request, pk):

    client = get_object_or_404(
        ClientProfile,
        user=request.user
    )

    address_obj = get_object_or_404(
        ClientDeliveryAddress,
        pk=pk,
        client=client
    )

    if request.method == "POST":

        ClientDeliveryAddress.objects.filter(
            client=client
        ).update(
            is_default=False
        )

        address_obj.is_default = True
        address_obj.is_active = True
        address_obj.save()

        messages.success(
            request,
            "Default delivery address changed."
        )

    return redirect("delivery_address_list")

@login_required
@transaction.atomic
def product_delete(request, pk):

    product = get_object_or_404(
        Product,
        pk=pk,
        created_by=request.user,
    )

    if request.method == "POST":

        product.delete()

        messages.success(
            request,
            "Product deleted successfully."
        )

        return redirect(
            "product_list"
        )

    return render(
        request,
        "seller/product_delete_confirm.html",
        {
            "product": product,
        },
    )

@login_required
def ecommerce_catalog(request):
    products = Product.objects.select_related('shop').filter(is_active=True).order_by('shop__name', 'name')
    return render(request, 'seller/ecommerce_catalog.html', {'products': products})

@login_required
def ecommerce_cart(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity = int(request.POST.get('quantity', 1) or 1)
        if product_id:
            cart = request.session.get('ecommerce_cart', {})
            cart[str(product_id)] = cart.get(str(product_id), 0) + quantity
            request.session['ecommerce_cart'] = cart
            messages.success(request, 'Product added to cart.')
            return redirect('ecommerce_cart')

    cart = request.session.get('ecommerce_cart', {})
    items = []
    total = Decimal('0')
    for product_id, qty in cart.items():
        product = get_object_or_404(Product, pk=product_id)
        line_total = product.price * qty
        total += line_total
        items.append({'product': product, 'qty': qty, 'line_total': line_total})
    return render(request, 'seller/ecommerce_cart.html', {'items': items, 'total': total})

@login_required
@transaction.atomic
def ecommerce_checkout(request):
    if request.method == 'POST':
        cart = request.session.get('ecommerce_cart', {})
        if not cart:
            messages.warning(request, 'Your cart is empty.')
            return redirect('ecommerce_cart')

        order_items = []
        shop_buckets = {}
        for product_id, qty in cart.items():
            product = get_object_or_404(Product, pk=product_id)
            order_items.append((product, qty))
            shop_buckets.setdefault(product.shop_id, []).append((product, qty))

        order_now = timezone.now()
        shops = {
            shop.pk: shop
            for shop in Shop.objects.filter(pk__in=shop_buckets)
        }
        shop_order_numbers = {
            shop_id: generate_shop_order_number(shops[shop_id], order_now)
            for shop_id in sorted(shop_buckets)
        }
        primary_shop_order_number = shop_order_numbers[next(iter(shop_buckets))]
        tracking_code = _generate_unique_tracking_code()
        order = Order.objects.create(
            order_id=f"{tracking_code}-{primary_shop_order_number}",
            tracking_code=tracking_code,
            payment_type='cash',
            is_payment_done=True,
            payment_amount=sum((product.price * qty) for product, qty in order_items),
            user=request.user.username,
            order_data=json.dumps([{'product_id': product.id, 'quantity': qty} for product, qty in order_items]),
            delivery_address='Checkout from seller helper',
            delivery_location='0,0',
            delivery_contact_no='',
            status='new',
            shop=None,
        )

        for shop_id, entries in shop_buckets.items():
            shop = shops[shop_id]
            items_payload = []
            total_amount = Decimal('0')
            total_count = 0
            total_weight = Decimal('0')
            for product, qty in entries:
                total_amount += product.price * qty
                total_count += qty
                total_weight += (product.price * Decimal('0'))
                items_payload.append({'product_id': product.id, 'name': product.name, 'quantity': qty, 'price': str(product.price)})
            shop_order = ShopOrder.objects.create(
                order=order,
                shop=shop,
                shop_order_number=shop_order_numbers[shop_id],
                items=items_payload,
                total_amount=total_amount,
                product_count=total_count,
                total_weight_kg=Decimal('0'),
                packing_minutes=max(5, 3 + total_count * 2),
                rider_eta_minutes=0,
                delivery_fee=Decimal('0'),
                status='pending',
            )

        request.session['ecommerce_cart'] = {}
        messages.success(request, 'Order placed successfully.')
        return redirect('ecommerce_orders')

    return redirect('ecommerce_cart')

@login_required
def ecommerce_orders(request):
    shop_orders = ShopOrder.objects.select_related('order', 'shop').filter(shop__owner=request.user).order_by('-created_at')
    return render(request, 'seller/ecommerce_orders.html', {'shop_orders': shop_orders})

@login_required
def ecommerce_pack_order(request, pk):
    shop_order = get_object_or_404(ShopOrder, pk=pk)
    if shop_order.shop.owner != request.user:
        messages.error(request, 'You do not have permission to update this shop order.')
        return redirect('ecommerce_orders')
    shop_order.status = 'packed'
    shop_order.save(update_fields=['status'])
    messages.success(request, 'Shop order marked as packed.')
    return redirect('ecommerce_orders')

@login_required
def ecommerce_deliver_order(request, pk):
    shop_order = get_object_or_404(ShopOrder, pk=pk)
    if shop_order.shop.owner != request.user:
        messages.error(request, 'You do not have permission to update this shop order.')
        return redirect('ecommerce_orders')
    shop_order.status = 'delivering'
    shop_order.save(update_fields=['status'])
    messages.success(request, 'Delivery flow started for this shop order.')
    return redirect('ecommerce_orders')

import requests
import urllib.parse

CLIENT_ID = "oG9S1DcVc2sLMRzVXcbhvZh0utsjU8oc"
CLIENT_SECRET = "ar0JFifj1dmP4XbGRQAnotbm9LBY7T4q1zOT7jWA"
REDIRECT_URI = "https://yourdomain.com/callback"  # Must match Dashboard settings
SCOPE = "request"  # Request scope required for booking rides

def get_authorization_url():
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }
    base_url = "https://login.uber.com/oauth/v2/authorize"
    auth_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    return auth_url

print("1. Open the following URL in your browser and log in:")
print(get_authorization_url())
print("-" * 60)

def get_access_token(auth_code):
    token_url = "https://login.uber.com/oauth/v2/token"

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    response = requests.post(token_url, data=payload, headers=headers)

    if response.status_code == 200:
        token_data = response.json()
        print("Successfully retrieved tokens!")
        return token_data
    else:
        print(f"Error ({response.status_code}):", response.text)
        return None

if __name__ == "__main__":
    authorization_code = input("2. Enter the 'code' query parameter from the redirect URL: ").strip()

    if authorization_code:
        tokens = get_access_token(authorization_code)
        if tokens:
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")
            expires_in = tokens.get("expires_in")

            print(f"\nBearer Access Token: {access_token}")
            print(f"Refresh Token: {refresh_token}")
            print(f"Expires In: {expires_in} seconds")

def get_uber_estimates(start_latitude, start_longitude, end_latitude, end_longitude):
    BASE_URL = "https://api.uber.com/v1.2"
    ACCESS_TOKEN = "YOUR_ACCESS_TOKEN_HERE"  # OAuth 2.0 Access Token or Server Token

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept-Language": "en_US",
        "Content-Type": "application/json"
    }

    params = {
        "start_latitude": 37.774929,
        "start_longitude": -122.419416,
        "end_latitude": 37.775393,
        "end_longitude": -122.417546
    }

    price_response = requests.get(f"{BASE_URL}/estimates/price", headers=headers, params=params)

    if price_response.status_code == 200:
        prices = price_response.json().get("prices", [])
        print("--- PRICE & FARE ESTIMATES ---")
        for price in prices:
            print(f"Product: {price.get('display_name')}")
            print(f"  Estimate Price: {price.get('estimate')}")
            print(f"  Currency Code: {price.get('currency_code')}")
            print(f"  Distance: {price.get('distance')} miles")
            print(f"  Duration: {int(price.get('duration') / 60)} minutes")
            print(f"  Fare ID (For booking): {price.get('fare_id')}")
            print(f"  High Estimate: {price.get('high_estimate')}")
            print(f"  Low Estimate: {price.get('low_estimate')}\n")
    else:
        print(f"Price Estimate Error ({price_response.status_code}):", price_response.text)

    time_params = {
        "start_latitude": params["start_latitude"],
        "start_longitude": params["start_longitude"]
    }

    time_response = requests.get(f"{BASE_URL}/estimates/time", headers=headers, params=time_params)

    if time_response.status_code == 200:
        times = time_response.json().get("times", [])
        print("--- PICKUP TIME ESTIMATES (ETA) ---")
        for time_est in times:
            eta_minutes = int(time_est.get("estimate") / 60)
            print(f"Product: {time_est.get('display_name')} -> Pickup ETA: {eta_minutes} mins")
    else:
        print(f"Time Estimate Error ({time_response.status_code}):", time_response.text)

import requests
def create_uber_request(fare_id, product_id, start_latitude, start_longitude, end_latitude, end_longitude):
    url = "https://api.uber.com/v1.2/requests"

    ACCESS_TOKEN = "YOUR_ACCESS_TOKEN_HERE"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "fare_id": "d30e732b8bba22c9cdc10513ee86380087cb4a6f89e37ad21ba2a39f3a1ba960",
        "product_id": "a1111c8c-c720-46c3-8534-2fcdd730040d",
        "start_latitude": 37.761492,
        "start_longitude": -122.423941,
        "end_latitude": 37.775393,
        "end_longitude": -122.417546,
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 202:
        data = response.json()
        print("Ride Request Accepted!")
        print(f"Request ID: {data.get('request_id')}")
        print(f"Status: {data.get('status')}")
        print(f"ETA: {data.get('eta')} minutes")
    else:
        print(f"Failed to create request. Status Code: {response.status_code}")
        print(response.json())