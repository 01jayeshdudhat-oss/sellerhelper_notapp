from django.urls import include, path
from . import views

from django.contrib.auth import views as auth_views

urlpatterns = [
    path("site.webmanifest", views.site_manifest, name="site_manifest"),
    path("about/", views.public_information_page, {"page_slug": "about"}, name="about"),
    path("contact/", views.public_information_page, {"page_slug": "contact"}, name="contact"),
    path("terms/", views.public_information_page, {"page_slug": "terms"}, name="terms"),
    path("privacy/", views.public_information_page, {"page_slug": "privacy"}, name="privacy_policy"),
    path("disclaimer/", views.public_information_page, {"page_slug": "disclaimer"}, name="disclaimer"),
    path("refunds/", views.public_information_page, {"page_slug": "refunds"}, name="refund_policy"),
    path("delivery-policy/", views.public_information_page, {"page_slug": "delivery"}, name="delivery_policy"),
    path("grievance/", views.public_information_page, {"page_slug": "grievance"}, name="grievance_policy"),
    path(
        "admin-panel/catalog/",
        views.admin_catalog_dashboard,
        name="admin_catalog_dashboard",
    ),
    path(
        "admin-panel/theme-builder/",
        views.admin_site_theme_builder,
        name="admin_site_theme_builder",
    ),
    path(
        "admin-panel/catalog/products/new/",
        views.admin_catalog_product_create,
        name="admin_catalog_product_create",
    ),
    path(
        "admin-panel/catalog/products/<int:pk>/edit/",
        views.admin_catalog_product_edit,
        name="admin_catalog_product_edit",
    ),
    path(
        "admin-panel/catalog/products/<int:pk>/toggle/",
        views.admin_catalog_product_toggle,
        name="admin_catalog_product_toggle",
    ),
    path(
        "admin-panel/catalog/categories/<int:pk>/toggle/",
        views.admin_catalog_category_toggle,
        name="admin_catalog_category_toggle",
    ),
    path(
        "admin-panel/catalog/categories/<int:pk>/edit/",
        views.admin_catalog_category_edit,
        name="admin_catalog_category_edit",
    ),
    path(
        "admin-panel/catalog/shops/<int:pk>/image-permission/",
        views.admin_catalog_shop_image_permission,
        name="admin_catalog_shop_image_permission",
    ),
    path(
        "admin-panel/catalog/shops/<int:pk>/shared-rider-groups-permission/",
        views.admin_catalog_shop_group_permission,
        name="admin_catalog_shop_group_permission",
    ),
    path(
        "admin-panel/catalog/shops/<int:pk>/storefront-subscription/",
        views.admin_catalog_shop_storefront_toggle,
        name="admin_catalog_shop_storefront_toggle",
    ),
    path(
        "delivery-sharing/groups/",
        views.shared_rider_groups,
        name="shared_rider_groups",
    ),
    path(
        "delivery-sharing/groups/memberships/<int:membership_id>/exit/",
        views.exit_shared_rider_group,
        name="exit_shared_rider_group",
    ),
    path(
        "delivery-sharing/groups/requests/<int:membership_id>/review/",
        views.review_shared_rider_group_shop,
        name="review_shared_rider_group_shop",
    ),
    path(
        "login/",
        views.PortalLoginView.as_view(),
        name="login"
    ),

    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout"
    ),

    path(
        "shop/",
        views.marketplace,
        name="marketplace"
    ),

    path(
        "cart/",
        views.cart,
        name="cart"
    ),
    path(
        "cart/select-address/",
        views.cart_select_delivery_address,
        name="cart_select_delivery_address",
    ),

    path(
        "cart/add/<int:product_shop_id>/",
        views.cart_add,
        name="cart_add"
    ),

    path(
        "cart/update/<int:product_shop_id>/",
        views.cart_update,
        name="cart_update"
    ),

    path(
        "cart/remove/<int:product_shop_id>/",
        views.cart_remove,
        name="cart_remove"
    ),
    path(
        "cart/baskets/save/",
        views.cart_basket_save,
        name="cart_basket_save",
    ),
    path(
        "cart/baskets/add-item/<int:product_shop_id>/",
        views.cart_basket_add_item,
        name="cart_basket_add_item",
    ),
    path(
        "cart/baskets/<int:basket_id>/restore/",
        views.cart_basket_restore,
        name="cart_basket_restore",
    ),
    path(
        "cart/baskets/<int:basket_id>/delete/",
        views.cart_basket_delete,
        name="cart_basket_delete",
    ),

    path(
        "orders/<str:order_id>/tracking/",
        views.order_tracking,
        name="order_tracking"
    ),
    path(
        "orders/<str:order_id>/tracking/updates/",
        views.order_tracking_updates,
        name="order_tracking_updates",
    ),
    path(
        "mobile/location/session/",
        views.mobile_location_session,
        name="mobile_location_session",
    ),
    path(
        "mobile/location/update/",
        views.mobile_live_location_update,
        name="mobile_live_location_update",
    ),
    path("orders/<str:order_id>/cancel/", views.customer_cancel_order, name="customer_cancel_order"),
    path("orders/<str:order_id>/reorder/", views.customer_reorder, name="customer_reorder"),
    path("placed-orders/<str:order_id>/edit/", views.customer_edit_placed_order, name="customer_edit_placed_order"),
    path("orders/<str:order_id>/delivery-address/", views.customer_change_order_address, name="customer_change_order_address"),
    path("orders/<str:order_id>/feedback/", views.customer_order_feedback, name="customer_order_feedback"),
    path("favorites/", views.customer_favorites, name="customer_favorites"),
    path("favorites/shops/<int:shop_id>/toggle/", views.toggle_favorite_shop, name="toggle_favorite_shop"),
    path("favorites/products/<int:product_shop_id>/toggle/", views.toggle_favorite_product, name="toggle_favorite_product"),

    path(
        "delivery-addresses/",
        views.delivery_address_list,
        name="delivery_address_list",
    ),

    path(
        "delivery-addresses/add/",
        views.delivery_address_add,
        name="delivery_address_add",
    ),

    path(
        "delivery-addresses/<int:pk>/edit/",
        views.delivery_address_edit,
        name="delivery_address_edit",
    ),

    path(
        "delivery-addresses/<int:pk>/delete/",
        views.delivery_address_delete,
        name="delivery_address_delete",
    ),

    path(
        "delivery-addresses/<int:pk>/default/",
        views.delivery_address_default,
        name="delivery_address_default",
    ),

    path(
        "checkout/",
        views.checkout,
        name="checkout"
    ),
    path(
        "checkout/razorpay/verify/",
        views.verify_razorpay_payment,
        name="verify_razorpay_payment",
    ),

    path(
        "order/<uuid:order_id>/",
        views.order_tracking,
        name="order_tracking"
    ),

    path(
        "shop/orders/",
        views.shop_order_list,
        name="shop_order_list"
    ),
    path(
        "shop/orders/abandoned/<int:attempt_id>/remark/",
        views.save_abandoned_checkout_remark,
        name="save_abandoned_checkout_remark",
    ),
    path(
        "shop/orders/verify-rider-pickup-otp/",
        views.verify_rider_pickup_otp,
        name="verify_rider_pickup_otp",
    ),

    path(
        "shop/orders/<int:shop_order_id>/",
        views.shop_order_detail,
        name="shop_order_detail"
    ),
    # QR-triggered shop order actions (requires login and staff/owner permission)
    path(
        "shop/orders/<int:shop_order_id>/action/<str:action>/",
        views.shop_order_action,
        name="shop_order_action",
    ),
    path(
        "shop/order-instructions/<int:instruction_id>/audio/",
        views.shop_order_instruction_audio,
        name="shop_order_instruction_audio",
    ),
    path(
        "shop/orders/<int:shop_order_id>/rider-location/live/",
        views.shop_order_rider_location,
        name="shop_order_rider_location",
    ),
    path(
        "shop/orders/<int:shop_order_id>/edit-items/",
        views.shop_edit_order_items,
        name="shop_edit_order_items",
    ),
    path(
        "shop/orders/<int:shop_order_id>/finalize-weight/",
        views.finalize_approximate_weights,
        name="finalize_approximate_weights",
    ),

    path(
        "shop/orders/<int:shop_order_id>/status/",
        views.shop_order_status,
        name="shop_order_status"
    ),

    path(
        "shop/orders/<int:shop_order_id>/status-update/",
        views.shop_order_status_update,
        name="shop_order_status_update"
    ),

    path(
        "shop/orders/<int:shop_order_id>/assign-packer/",
        views.assign_packer,
        name="assign_packer"
    ),

    path(
        "shop/orders/<int:shop_order_id>/assign-rider/",
        views.assign_rider,
        name="assign_rider"
    ),
    path(
        "shop/orders/<int:shop_order_id>/cancel-rider-assignment/",
        views.cancel_rider_assignment,
        name="cancel_rider_assignment",
    ),

    path(
        "shop/orders/<int:shop_order_id>/accept-rider/",
        views.accept_rider_order,
        name="accept_rider_order"
    ),
    path(
        "shop/orders/<int:shop_order_id>/rider-pickup/",
        views.rider_pickup_order,
        name="rider_pickup_order",
    ),
    path(
        "rider/scan-pickup-order/",
        views.rider_scan_pickup_qr,
        name="rider_scan_pickup_qr",
    ),
    path(
        "rider/start-active-deliveries/",
        views.rider_start_active_deliveries,
        name="rider_start_active_deliveries",
    ),
    path(
        "rider/stop-public-batch-offers/",
        views.stop_public_rider_batch_offers,
        name="stop_public_rider_batch_offers",
    ),

    path(
        "shop/orders/<int:shop_order_id>/accept-packer/",
        views.accept_packer_order,
        name="accept_packer_order"
    ),

    path(
        "shop/orders/<int:shop_order_id>/verify-customer-otp/",
        views.verify_customer_otp,
        name="verify_customer_otp"
    ),
    path(
        "shop/orders/<int:shop_order_id>/complete-delivery/",
        views.complete_delivery,
        name="complete_delivery"
    ),
    path(
        "shop/orders/<int:shop_order_id>/edit-delivery-location/",
        views.edit_completed_delivery_location,
        name="edit_completed_delivery_location",
    ),
    path(
        "shop/orders/<int:shop_order_id>/confirm-returned-items/",
        views.confirm_returned_items_received,
        name="confirm_returned_items_received",
    ),
    path(
        "shop/orders/<int:shop_order_id>/confirm-cash-handover/",
        views.confirm_rider_cash_handover,
        name="confirm_rider_cash_handover",
    ),
    path(
        "rider/cod-upi/<int:payment_request_id>/request-approval/",
        views.request_cod_payment_approval,
        name="request_cod_payment_approval",
    ),
    path(
        "rider/cod-upi/<int:payment_request_id>/cancel/",
        views.cancel_cod_payment_qr,
        name="cancel_cod_payment_qr",
    ),
    path(
        "shop/cod-upi/<int:payment_request_id>/approve/",
        views.approve_cod_payment,
        name="approve_cod_payment",
    ),
    path(
        "shop/cod-upi/<int:payment_request_id>/reject/",
        views.reject_cod_payment,
        name="reject_cod_payment",
    ),
    path(
        "shop/<int:shop_id>/riders/<int:rider_id>/confirm-cod-handover/",
        views.confirm_rider_cod_handover_total,
        name="confirm_rider_cod_handover_total",
    ),
    path(
        "shop/orders/<int:shop_order_id>/edit-rider-cod-collection/",
        views.edit_rider_cod_collection,
        name="edit_rider_cod_collection",
    ),
    path(
        "shop/orders/<int:shop_order_id>/collect-cod-balance/",
        views.collect_cod_balance,
        name="collect_cod_balance",
    ),

    path(
        "shop/staff/",
        views.shop_staff_list,
        name="shop_staff_list",
    ),

    path(
        "shop/staff/<int:shop_id>/",
        views.shop_staff_list,
        name="shop_staff_list_shop",
    ),

    path(
        "shop/staff/<int:shop_id>/<int:staff_id>/edit/",
        views.shop_staff_edit,
        name="shop_staff_edit"
    ),

    path(
        "shop/staff/add/",
        views.shop_staff_create,
        name="shop_staff_create",
    ),

    path(
        "shop/staff/add/<int:shop_id>/",
        views.shop_staff_create,
        name="shop_staff_create_shop",
    ),

    path(
        "shop/staff/<int:shop_id>/<int:staff_id>/toggle/",
        views.shop_staff_toggle,
        name="shop_staff_toggle",
    ),

    path(
        "shop/packer/",
        views.packer_dashboard,
        name="packer_dashboard"
    ),

    path(
        "shop/rider/",
        views.rider_dashboard,
        name="rider_dashboard"
    ),
    path(
        "shop/staff/payment-settings/",
        views.staff_payment_settings,
        name="staff_payment_settings",
    ),

    path(
        "shop/orders/<int:shop_order_id>/rider-cancel/",
        views.rider_cancel_order,
        name="rider_cancel_order",
    ),
    path(
        "shop/orders/<int:shop_order_id>/rider-location/",
        views.update_rider_location,
        name="update_rider_location",
    ),
    path(
        "shop/rider/location/",
        views.update_public_rider_location,
        name="update_public_rider_location",
    ),
    path(
        "shop/rider/availability/",
        views.set_public_rider_availability,
        name="set_public_rider_availability",
    ),
    path(
        "shop/rider/work-area/",
        views.save_public_rider_work_area,
        name="save_public_rider_work_area",
    ),

    path("notifications/", views.notifications, name="notifications"),
    path("notifications/api/", views.notifications_api, name="notifications_api"),
    path(
        "notifications/device-token/",
        views.register_notification_device_token,
        name="register_notification_device_token",
    ),
    path("notifications/read-all/", views.mark_all_notifications_read, name="mark_all_notifications_read"),
    path("notifications/<int:notification_id>/read/", views.mark_notification_read, name="mark_notification_read"),
    path("jobs/", views.job_openings, name="job_openings"),
    path("jobs/my-applications/", views.my_job_applications, name="my_job_applications"),
    path("jobs/<int:job_id>/apply/", views.job_apply, name="job_apply"),
    path("shops/<int:shop_id>/jobs/", views.shop_job_openings, name="shop_job_openings"),
    path("shops/<int:shop_id>/jobs/new/", views.shop_job_opening_create, name="shop_job_opening_create"),
    path("shops/<int:shop_id>/jobs/applications/", views.shop_job_applications, name="shop_job_applications"),
    path("shops/<int:shop_id>/jobs/applications/<int:application_id>/status/", views.shop_job_application_status, name="shop_job_application_status"),

    path('', views.home, name='home'),
    path('market/', views.marketplace, name='market'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path(
        "dashboard/customers/",
        views.shop_customers,
        name="shop_customers",
    ),
    path(
        "dashboard/customers/export/",
        views.shop_customers_export,
        name="shop_customers_export",
    ),
    path('dashboard/statement/', views.owner_statement, name='owner_statement'),
    path('staff-dashboard/', views.staff_dashboard, name='staff_dashboard'),
    path(
        "staff/purchase-manager/<int:shop_id>/",
        views.purchase_manager_dashboard,
        name="purchase_manager_dashboard",
    ),
    path(
        "staff/purchase-manager/<int:shop_id>/products/add/",
        views.purchase_manager_add_product,
        name="purchase_manager_add_product",
    ),
    path('signup/', views.signup, name='signup'),
    path("rider/apply/", views.public_rider_apply, name="public_rider_apply"),
    path('accounts/logout/', views.logout_view, name='logout'),
    path(
        "accounts/login/",
        views.PortalLoginView.as_view(),
        name="login",
    ),
    path('accounts/', include('django.contrib.auth.urls')),

    path('orders/', views.order_list, name='order_list'),
    path('orders/new/', views.order_create, name='order_create'),
    path('orders/<int:pk>/edit/', views.order_edit, name='order_edit'),
    path('orders/<int:pk>/delete/', views.order_delete, name='order_delete'),

    path(
        "shops/",
        views.shop_list,
        name="shop_list"
    ),
    path(
        "shops/storefront-slug-availability/",
        views.shop_storefront_slug_available,
        name="shop_storefront_slug_available",
    ),
    path(
        "storefronts/<slug:slug>/",
        views.public_shop_storefront,
        name="public_shop_storefront",
    ),

    path(
        "shops/<int:pk>/",
        views.shop_detail,
        name="shop_detail"
    ),

    path(
        "shops/add/",
        views.shop_add,
        name="shop_add"
    ),

    path(
        "shops/<int:pk>/edit/",
        views.shop_edit,
        name="shop_edit"
    ),
    path(
        "shops/<int:pk>/delete/",
        views.shop_delete,
        name="shop_delete"
    ),

    path(
        "shop/<int:shop_id>/toggle-status/",
        views.shop_toggle_status,
        name="shop_toggle_status",
    ),
    path(
        "shop/<int:shop_id>/settings/",
        views.shop_settings,
        name="shop_settings",
    ),
    path(
        "shop/orders/",
        views.shop_order_list,
        name="shop_order_list"
    ),
    path(
        "shop/<int:shop_id>/orders/",
        views.shop_order_list,
        name="shop_order_list"
    ),

    path(
        "my-profile/",
        views.client_profile,
        name="client_profile"
    ),
    path(
        "my-profile/",
        views.client_profile,
        name="my_profile"
    ),
    path(
        "my-wallet/",
        views.customer_wallet_history,
        name="customer_wallet_history",
    ),

    path(
        "clients/<int:pk>/edit/",
        views.client_edit,
        name="client_edit"
    ),
    path(
        "clients/",
        views.client_list,
        name="client_list"
    ),

    path(
        "clients/add/",
        views.client_add,
        name="client_add"
    ),

    path(
        "clients/<int:pk>/delete/",
        views.client_delete,
        name="client_delete"
    ),

    path('products/', views.product_list, name='product_list'),
    path('products/new/', views.product_create, name='product_create'),
    path('products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('products/<int:pk>/delete/', views.product_delete, name='product_delete'),
    path('shops/<int:shop_id>/storefront/', views.shop_storefront, name='shop_storefront'),
    path('shops/<int:shop_id>/riders/map/', views.shop_rider_map, name='shop_rider_map'),
    path('shop-products/<int:product_shop_id>/edit/', views.storefront_product_edit, name='storefront_product_edit'),
    path('shops/<int:shop_id>/stock/import/', views.stock_import, name='stock_import'),
    path('shops/<int:shop_id>/stock/report/', views.stock_report, name='stock_report'),
    path('shops/<int:shop_id>/orders/report/', views.shop_order_report, name='shop_order_report'),
    path('shops/<int:shop_id>/wallet/', views.shop_wallet, name='shop_wallet'),
    path('payments/razorpayx/webhook/', views.razorpayx_payout_webhook, name='razorpayx_payout_webhook'),

    path(
        "market/product/<int:product_shop_id>/",
        views.market_product_detail,
        name="market_product_detail"
    ),
    path('shop/orders/<int:shop_order_id>/packing-slip/', views.packing_slip, name='packing_slip'),

    path('ecommerce/', views.ecommerce_catalog, name='ecommerce_catalog'),
    path('ecommerce/cart/', views.ecommerce_cart, name='ecommerce_cart'),
    path('ecommerce/checkout/', views.ecommerce_checkout, name='ecommerce_checkout'),
    path('ecommerce/orders/', views.ecommerce_orders, name='ecommerce_orders'),
    path('ecommerce/orders/<int:pk>/pack/', views.ecommerce_pack_order, name='ecommerce_pack_order'),
    path('ecommerce/orders/<int:pk>/deliver/', views.ecommerce_deliver_order, name='ecommerce_deliver_order'),
]
