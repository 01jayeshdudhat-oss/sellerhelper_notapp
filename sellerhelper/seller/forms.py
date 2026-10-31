
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db.models import Q
from django.utils import timezone

from .models import (
    AdminProductCatalog,
    DeliverySharingSettings,
    MarketplaceLayoutSettings,
    ProductCloneSettings,
    PRODUCT_CLONE_FIELD_CHOICES,
    PublicRiderApplication,
    SiteBrandingSettings,
    SiteThemeSettings,
    Order,
    Product,
    ProductCategory,
    ProductShop,
    SharedRiderGroup,
    Shop,
    ShopStaff,
    StaffLeaveApplication,
    StaffResignationRequest,
    SHOP_STOREFRONT_RESERVED_SLUGS,
    get_product_category_choices,
)


def _split_product_unit(unit):
    unit = (unit or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(\D.*)", unit)
    if not match:
        return None, unit
    return Decimal(match.group(1)), match.group(2).strip()


def _join_product_unit(value, unit_text):
    unit_text = (unit_text or "").strip()
    if value is None:
        return unit_text
    return f"{format(value.normalize(), 'f')}{unit_text}"


def _join_bulk_unit(value, unit_text):
    unit_text = (unit_text or "").strip()
    if value is None:
        return unit_text
    return f"{format(value.normalize(), 'f')} {unit_text}".strip()


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            'order_time',
            'order_suggested_delivery_minute',
            'order_suggested_delivery_time',
            'order_delivered_time',
            'order_id',
            'payment_id',
            'payment_type',
            'is_payment_done',
            'payment_amount',
            'finally_received_amount',
            'user',
            'order_data',
            'delivery_address',
            'delivery_location',
            'delivery_contact_no',
            'is_valid_order',
            'status',
            'shop',
            'shop_location',
            'shop_delivery_area',
            'shop_delivery_pincode',
            'assigned_packer',
            'packer_contact_no',
            'packer_data',
            'delivery_partner',
            'assigned_rider',
            'rider_contact_no',
            'rider_pickup_data',
            'order_pickup_location',
            'rider_drop_data',
            'order_drop_location',
            'order_final_picked_location',
            'order_final_drop_location',
            'user_remark',
            'admin_remark',
            'admin_remark_for_client',
        ]
        widgets = {
            'order_time': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'order_suggested_delivery_time': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'order_delivered_time': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'order_data': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'delivery_address': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'packer_data': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'rider_pickup_data': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'rider_drop_data': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'user_remark': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'admin_remark': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'admin_remark_for_client': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
        }

from django import forms
from .models import Shop


class CustomerSignupForm(UserCreationForm):
    accept_legal_documents = forms.BooleanField(
        required=True,
        label="I have read and accept the Terms and Conditions, Privacy Policy, Disclaimer, Cancellation and Refund Policy, and Delivery Policy.",
    )


class PublicRiderApplicationForm(forms.ModelForm):
    consent_to_verification = forms.BooleanField(
        required=True,
        label=(
            "I consent to admin review of my rider application and the identity "
            "documents I submit."
        ),
    )

    class Meta:
        model = PublicRiderApplication
        fields = (
            "full_name",
            "phone",
            "address",
            "vehicle_type",
            "driving_license_number",
            "license_document",
            "identity_document",
        )
        widgets = {
            "full_name": forms.TextInput(attrs={"autocomplete": "name"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
            "address": forms.Textarea(attrs={"rows": 3, "autocomplete": "street-address"}),
            "driving_license_number": forms.TextInput(attrs={"autocomplete": "off"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("license_document", "identity_document"):
            self.fields[field_name].required = not bool(
                getattr(self.instance, field_name)
            )

    def clean_license_document(self):
        return self._validate_document(self.cleaned_data.get("license_document"))

    def clean_identity_document(self):
        return self._validate_document(self.cleaned_data.get("identity_document"))

    def clean_phone(self):
        phone = re.sub(r"[\s()-]", "", self.cleaned_data["phone"])
        digits = phone.removeprefix("+")
        if not digits.isascii() or not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise ValidationError("Enter a valid phone number.")
        return phone

    @staticmethod
    def _validate_document(upload):
        if not upload or not hasattr(upload, "read"):
            return upload
        if upload.size > 5 * 1024 * 1024:
            raise ValidationError("Each document must be 5 MB or smaller.")

        header = upload.read(8)
        upload.seek(0)
        valid = (
            header.startswith(b"%PDF-")
            or header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
        )
        if not valid:
            raise ValidationError("Upload a PDF, PNG, or JPEG document.")
        return upload


class ShopForm(forms.ModelForm):
    accept_legal_documents = forms.BooleanField(
        required=True,
        label="I have read and accept all platform legal documents.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    storefront_slug = forms.CharField(
        required=True,
        max_length=63,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "choose-a-unique-name",
            "pattern": "[a-z0-9]+(?:-[a-z0-9]+)*",
            "maxlength": 63,
            "autocomplete": "off",
        }),
        error_messages={"required": "Choose a unique shop subdomain."},
    )

    shop_category = forms.ChoiceField(
        required=False,
        choices=(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Shop

        fields = [
            "name",
            "storefront_slug",
            "market_category",
            "shop_category",
            "alias_name",
            "logo_image",
            "cover_image",
            "logo_url",
            "cover_image_url",
            "contact_no",
            "email_no",
            "upi_id",
            "gstin",
            "delivery_fee",
            "delivery_fee_mode",
            "delivery_fee_per_km",
            "free_delivery_minimum",
            "delivery_hsn_code",
            "delivery_gst_rate",
            "gst_state_code",
            "billing_address",
            "is_shop_open",
            "share_riders_with_nearby_shops",
            "shop_open_time",
            "shop_close_time",
            "shop_data",
        ]

    def __init__(self, *args, **kwargs):
        require_legal_acceptance = kwargs.pop("require_legal_acceptance", None)
        super().__init__(*args, **kwargs)
        if require_legal_acceptance is None:
            require_legal_acceptance = not bool(self.instance.pk)
        self.fields["accept_legal_documents"].required = require_legal_acceptance
        for field_name in (
            "delivery_fee_mode",
            "delivery_fee_per_km",
            "free_delivery_minimum",
        ):
            self.fields[field_name].required = False

        category_rows = list(
            ProductCategory.objects.filter(is_active=True)
            .order_by("sort_order", "name")
            .values_list("name", "market_category")
        )
        category_names = {name for name, _market in category_rows}
        choices = [("", "Select a category")]
        choices.extend((name, name) for name, _market in category_rows)
        choices.extend(
            (name, label)
            for name, label in Shop.SHOP_CATEGORY_CHOICES
            if name not in category_names
        )
        current_category = self.instance.shop_category
        if current_category and current_category not in {
            value for value, _label in choices
        }:
            choices.append((current_category, current_category))
        self.fields["shop_category"].choices = choices
        self.category_market_map = {
            name: market for name, market in category_rows
        }

        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Shop name"
            }),
            "storefront_slug": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "your-shop-name",
                "pattern": "[a-z0-9]+(?:-[a-z0-9]+)*",
                "maxlength": 63,
                "autocomplete": "off",
            }),

            "market_category": forms.Select(attrs={
                "class": "form-select"
            }),

            "shop_category": forms.Select(attrs={
                "class": "form-select"
            }),

            "alias_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Alias name"
            }),

            "logo_image": forms.ClearableFileInput(attrs={
                "class": "form-control",
                "accept": "image/jpeg,image/png,image/webp",
            }),
            "cover_image": forms.ClearableFileInput(attrs={
                "class": "form-control",
                "accept": "image/jpeg,image/png,image/webp",
            }),
            "logo_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "Shop logo URL"}),
            "cover_image_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "Shop cover image URL"}),

            "contact_no": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Contact number"
            }),

            "email_no": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "Shop email"
            }),
            "upi_id": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "shop@upi",
                "autocomplete": "off",
            }),
            "gstin": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "maxlength": "15",
                "placeholder": "15-character GSTIN (optional)",
            }),
            "delivery_fee": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "step": "0.01",
                "placeholder": "0.00",
            }),
            "delivery_fee_mode": forms.Select(attrs={"class": "form-select"}),
            "delivery_fee_per_km": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "step": "0.01",
                "placeholder": "0.00",
            }),
            "free_delivery_minimum": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "step": "0.01",
                "placeholder": "0.00",
            }),
            "delivery_hsn_code": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": "8",
                "placeholder": "HSN/SAC code",
            }),
            "delivery_gst_rate": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "max": "100",
                "step": "0.01",
                "placeholder": "Use platform default",
            }),
            "gst_state_code": forms.Select(attrs={"class": "form-select"}),
            "billing_address": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Registered business address for GST invoices",
            }),

            "is_shop_open": forms.CheckboxInput(attrs={
                "class": "form-check-input"
            }),
            "share_riders_with_nearby_shops": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),

            "shop_open_time": forms.TimeInput(attrs={
                "class": "form-control",
                "type": "time"
            }),

            "shop_close_time": forms.TimeInput(attrs={
                "class": "form-control",
                "type": "time"
            }),

            "shop_data": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Additional shop information"
            }),
        }
        for field_name, configured_widget in widgets.items():
            field_widget = self.fields[field_name].widget
            field_widget.attrs.update(configured_widget.attrs)
            configured_input_type = getattr(configured_widget, "input_type", None)
            if configured_input_type:
                field_widget.input_type = configured_input_type

    def _get_validation_exclusions(self):
        exclusions = super()._get_validation_exclusions()
        exclusions.add("shop_category")
        return exclusions

    def clean_shop_category(self):
        category_name = self.cleaned_data["shop_category"]
        if not category_name:
            return category_name

        category = ProductCategory.objects.filter(name=category_name).first()
        if category is None:
            return category_name
        if not category.is_active and category_name != self.instance.shop_category:
            raise ValidationError("Select an active shop category.")

        market_category = self.cleaned_data.get("market_category")
        if category.market_category and category.market_category != market_category:
            raise ValidationError(
                "Select a shop category assigned to the chosen market."
            )
        return category_name

    def clean_storefront_slug(self):
        slug = (self.cleaned_data.get("storefront_slug") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ValidationError(
                "Use lowercase letters, numbers, and single hyphens only."
            )
        if len(slug) > 63:
            raise ValidationError("The shop web address must be 63 characters or fewer.")
        if slug in SHOP_STOREFRONT_RESERVED_SLUGS:
            raise ValidationError("This shop web address is reserved.")
        matching_shops = Shop.objects.filter(storefront_slug=slug)
        if self.instance.pk:
            matching_shops = matching_shops.exclude(pk=self.instance.pk)
        if matching_shops.exists():
            raise ValidationError("This shop web address is already in use.")
        return slug

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
            raise ValidationError("Enter a valid 15-character GSTIN.")
        return gstin

    def clean_upi_id(self):
        return _clean_upi_id(self.cleaned_data.get("upi_id"), "shop")

    def clean(self):
        cleaned_data = super().clean()
        for field_name, default in (
            ("delivery_fee_mode", "FIXED"),
            ("delivery_fee_per_km", Decimal("0.00")),
            ("free_delivery_minimum", Decimal("0.00")),
        ):
            if cleaned_data.get(field_name) is None:
                cleaned_data[field_name] = getattr(self.instance, field_name, default)
        gst_rate = cleaned_data.get("delivery_gst_rate")
        if (
            cleaned_data.get("gstin")
            and gst_rate is not None
            and gst_rate > 0
            and not cleaned_data.get("delivery_hsn_code")
        ):
            self.add_error(
                "delivery_hsn_code",
                "Enter the delivery service HSN/SAC code when delivery GST applies.",
            )
        return cleaned_data


class ShopDeliveryTaxSettingsForm(forms.Form):
    delivery_hsn_code = forms.CharField(
        required=False,
        max_length=8,
        validators=[
            RegexValidator(
                regex=r"^\d{4}(?:\d{2}){0,2}$",
                message="Enter a 4, 6, or 8 digit HSN/SAC code.",
            )
        ],
    )
    delivery_gst_rate = forms.DecimalField(
        required=False,
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
    )

    def __init__(self, *args, gstin="", **kwargs):
        self.gstin = gstin
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        gst_rate = cleaned_data.get("delivery_gst_rate")
        if (
            self.gstin
            and gst_rate is not None
            and gst_rate > 0
            and not cleaned_data.get("delivery_hsn_code")
        ):
            self.add_error(
                "delivery_hsn_code",
                "Enter the delivery service HSN/SAC code when delivery GST applies.",
            )
        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        gstin = cleaned_data.get("gstin")
        state_code = cleaned_data.get("gst_state_code")
        if gstin and not state_code:
            self.add_error("gst_state_code", "Select the state associated with this GSTIN.")
        elif gstin and state_code and gstin[:2] != state_code:
            self.add_error("gstin", "The GSTIN state prefix must match the selected state.")
        return cleaned_data

from django import forms
from django.contrib.auth.models import User

from .models import ShopStaff


def _clean_upi_id(value, account_label):
    upi_id = (value or "").strip()
    if upi_id and not re.fullmatch(
        r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}",
        upi_id,
    ):
        raise ValidationError(f"Enter a valid {account_label} UPI ID.")
    return upi_id


class ShopStaffForm(forms.ModelForm):

    username = forms.CharField(
        max_length=150,
        required=False,
    )

    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
    )

    existing_staff_user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        label="Existing staff account",
    )

    class Meta:
        model = ShopStaff

        fields = [
            "mobile_no",
            "upi_id",
            "role",
            "can_view_orders",
            "can_confirm_orders",
            "can_pack_orders",
            "can_change_order_status",
            "can_manage_stock",
            "can_manage_products",
            "can_manage_staff",
            "can_assign_staff",
            "can_cancel_orders",
            "can_view_reports",
            "can_confirm_rider_payments",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):

        self.shop = kwargs.pop("shop", None)

        super().__init__(
            *args,
            **kwargs
        )

        self.fields["mobile_no"].widget.attrs.update({"class": "form-control", "placeholder": "Staff mobile number"})
        self.fields["upi_id"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "yourname@upi",
            "autocomplete": "off",
        })
        self.fields["existing_staff_user"].widget.attrs.update({
            "class": "form-select",
        })
        self.fields["existing_staff_user"].label_from_instance = (
            lambda user: (
                f"{user.get_full_name() or user.username} "
                f"(@{user.username})"
            )
        )

        if self.instance.pk:

            self.fields["username"].initial = (
                self.instance.user.username
            )

            self.fields[
                "username"
            ].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        existing_staff_user = cleaned_data.get("existing_staff_user")
        username = cleaned_data.get("username")
        password = cleaned_data.get("password")
        if existing_staff_user and username:
            self.add_error(
                "username",
                "Choose an existing staff account or enter a new username, not both.",
            )
        elif not existing_staff_user and not username:
            self.add_error(
                "username",
                "Select an existing staff account or enter a username for a new assignment.",
            )
        elif not self.instance.pk and username:
            user = User.objects.filter(username=username).first()
            if (
                user
                and self.shop
                and ShopStaff.objects.filter(shop=self.shop, user=user).exists()
            ):
                self.add_error(
                    "username",
                    "This account is already assigned to this shop. Edit its existing staff membership instead.",
                )
            elif user is None and not password:
                self.add_error(
                    "password",
                    "Enter a password for a new staff account. Existing usernames can be reused without changing their password.",
                )
        return cleaned_data

    def save(self, commit=True):

        staff = super().save(
            commit=False
        )

        username = self.cleaned_data[
            "username"
        ]

        password = self.cleaned_data.get(
            "password"
        )

        if not staff.pk:

            existing_user = (
                self.cleaned_data.get("existing_staff_user")
                or User.objects.filter(username=username).first()
            )
            if existing_user:
                user = existing_user
            else:
                user = User.objects.create_user(
                    username=username,
                    password=password,
                )

            staff.user = user

        if commit:
            staff.save()

        return staff

    def clean_upi_id(self):
        return _clean_upi_id(self.cleaned_data.get("upi_id"), "staff")


class ShopStaffPaymentSettingsForm(forms.ModelForm):
    class Meta:
        model = ShopStaff
        fields = [
            "upi_id",
            "max_load_weight_kg",
            "max_load_volume_cubic_ft",
        ]
        widgets = {
            "upi_id": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "yourname@upi",
                "autocomplete": "off",
            }),
            "max_load_weight_kg": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0.01",
                "step": "0.01",
                "placeholder": "e.g. 50",
            }),
            "max_load_volume_cubic_ft": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0.01",
                "step": "0.01",
                "placeholder": "e.g. 8",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.role != "RIDER":
            self.fields.pop("max_load_weight_kg")
            self.fields.pop("max_load_volume_cubic_ft")

    def clean_upi_id(self):
        return _clean_upi_id(self.cleaned_data.get("upi_id"), "staff")

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("max_load_weight_kg") and not cleaned_data.get(
            "max_load_volume_cubic_ft"
        ):
            self.add_error(
                "max_load_volume_cubic_ft",
                "Enter both capacity limits, or leave both blank.",
            )
        elif cleaned_data.get("max_load_volume_cubic_ft") and not cleaned_data.get(
            "max_load_weight_kg"
        ):
            self.add_error(
                "max_load_weight_kg",
                "Enter both capacity limits, or leave both blank.",
            )
        return cleaned_data

from django import forms

from .models import ClientProfile

class ClientProfileForm(forms.ModelForm):

    class Meta:

        model = ClientProfile

        fields = [
            "name",
            "contact_no",
            "email",
            "gstin",
            "preferred_delivery_time",
            "is_single_delivery_for_multiple_shop",
            "is_membership_customer",
            "membership_till_date",
            "wallet_balance",
            "eligible_coupon_list_data",
            "date_of_birth",
            "gender",
            "notes",
            "is_active",
        ]

        widgets = {

            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Client name"
                }
            ),

            "contact_no": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Contact number"
                }
            ),

            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Email"
                }
            ),
            "gstin": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "maxlength": 15,
                "placeholder": "15-character GSTIN (optional; required for B2B login)",
            }),

            "preferred_delivery_time": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Example: 10:00-12:00"
                }
            ),

            "membership_till_date": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date"
                }
            ),

            "date_of_birth": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date"
                }
            ),

            "gender": forms.TextInput(
                attrs={
                    "class": "form-control"
                }
            ),

            "wallet_balance": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01"
                }
            ),

            "notes": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3
                }
            ),

            "is_single_delivery_for_multiple_shop":
                forms.CheckboxInput(
                    attrs={
                        "class": "form-check-input"
                    }
                ),

            "is_membership_customer":
                forms.CheckboxInput(
                    attrs={
                        "class": "form-check-input"
                    }
                ),

            "is_active":
                forms.CheckboxInput(
                    attrs={
                        "class": "form-check-input"
                    }
                ),
        }

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
            raise ValidationError("Enter a valid 15-character GSTIN.")
        return gstin

class ClientSelfProfileForm(forms.ModelForm):

    class Meta:

        model = ClientProfile

        fields = [
            "name",
            "contact_no",
            "email",
            "gstin",
            "date_of_birth",
            "gender",
        ]

        widgets = {

            "name": forms.TextInput(
                attrs={
                    "class": "form-control"
                }
            ),

            "contact_no": forms.TextInput(
                attrs={
                    "class": "form-control"
                }
            ),

            "email": forms.EmailInput(
                attrs={
                    "class": "form-control"
                }
            ),
            "gstin": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "maxlength": 15,
                "placeholder": "15-character GSTIN (optional; required for B2B login)",
            }),

            "date_of_birth": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date"
                }
            ),

            "gender": forms.TextInput(
                attrs={
                    "class": "form-control"
                }
            ),
        }

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
            raise ValidationError("Enter a valid 15-character GSTIN.")
        return gstin

from django import forms

from .models import Product, ProductShop, Shop, ClientDeliveryAddress, JobOpening, JobApplication


class JobOpeningForm(forms.ModelForm):
    class Meta:
        model = JobOpening
        fields = [
            "shop",
            "title",
            "role",
            "location",
            "employment_type",
            "description",
            "requirements",
            "openings",
            "is_active",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Store Associate"}),
            "role": forms.TextInput(attrs={"class": "form-control", "placeholder": "Delivery, Sales, Manager"}),
            "location": forms.TextInput(attrs={"class": "form-control", "placeholder": "Shop location"}),
            "employment_type": forms.Select(attrs={"class": "form-select"}, choices=[
                ("Full Time", "Full Time"),
                ("Part Time", "Part Time"),
                ("Contract", "Contract"),
            ]),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "requirements": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "openings": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "shop": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        if owner is not None:
            self.fields["shop"].queryset = Shop.objects.filter(owner=owner)


class JobApplicationForm(forms.ModelForm):
    class Meta:
        model = JobApplication
        fields = [
            "job_opening",
            "full_name",
            "email",
            "phone",
            "job_title",
            "location",
            "experience",
            "cover_letter",
        ]
        widgets = {
            "job_opening": forms.Select(attrs={"class": "form-select"}),
            "full_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Your full name"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "you@example.com"}),
            "phone": forms.TextInput(attrs={"class": "form-control", "placeholder": "+91 98765 43210"}),
            "job_title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Role you are applying for"}),
            "location": forms.TextInput(attrs={"class": "form-control", "placeholder": "Preferred city / area"}),
            "experience": forms.TextInput(attrs={"class": "form-control", "placeholder": "2 years"}),
            "cover_letter": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["job_opening"].required = False
        self.fields["job_title"].required = False
        self.fields["location"].required = False


class ClientDeliveryAddressForm(forms.ModelForm):

    class Meta:
        model = ClientDeliveryAddress

        fields = [
            "label",
            "address",
            "city",
            "pincode",
            "latitude",
            "longitude",
            "is_active",
            "is_default",
        ]

        widgets = {

            "label": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Mumbai - 400001",
                }
            ),

            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Delivery address",
                }
            ),

            "city": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),

            "pincode": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),

            "latitude": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "any",
                    "readonly": "readonly",
                }
            ),

            "longitude": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "any",
                    "readonly": "readonly",
                }
            ),

            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),

            "is_default": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }


from django import forms
from .models import ProductShop

class ProductShopAssignmentForm(forms.Form):

    shop = forms.ModelChoiceField(
        queryset=Shop.objects.none(),
        widget=forms.HiddenInput()
    )

    assigned = forms.BooleanField(
        required=False,
        label="Assign"
    )

    price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "Default price"
            }
        ),
        label="Shop Price"
    )

    stock = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "0.0001",
            }
        )
    )

    is_active = forms.BooleanField(
        required=False,
        initial=True,
        label="Active"
    )

    def __init__(self, *args, owner=None, **kwargs):

        super().__init__(*args, **kwargs)

        self.fields["shop"].queryset = Shop.objects.filter(
            owner=owner
        )


class ProductShopQuickEditForm(forms.ModelForm):
    price = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "form-control form-control-sm", "min": "0", "step": "0.01"}
        ),
    )
    stock = forms.DecimalField(
        required=True,
        min_value=Decimal("0"),
        max_digits=14,
        decimal_places=4,
        widget=forms.NumberInput(
            attrs={"class": "form-control form-control-sm", "min": "0", "step": "0.0001"}
        ),
    )

    class Meta:
        model = ProductShop
        fields = ("price", "stock")


class ShopStaffPayrollSettingsForm(forms.ModelForm):
    class Meta:
        model = ShopStaff
        fields = (
            "salary_method",
            "monthly_base_salary",
            "hourly_rate",
            "completed_order_bonus",
        )
        widgets = {
            "salary_method": forms.Select(attrs={"class": "form-select"}),
            "monthly_base_salary": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"},
            ),
            "hourly_rate": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"},
            ),
            "completed_order_bonus": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"},
            ),
        }


class StaffLeaveApplicationForm(forms.ModelForm):
    class Meta:
        model = StaffLeaveApplication
        fields = ("start_date", "end_date", "reason")
        widgets = {
            "start_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"},
            ),
            "end_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"},
            ),
            "reason": forms.Textarea(
                attrs={"class": "form-control", "rows": 3, "maxlength": 2000},
            ),
        }

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and start_date < timezone.localdate():
            self.add_error("start_date", "Leave cannot start in the past.")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "End date must be on or after the start date.")
        return cleaned_data


class StaffResignationForm(forms.ModelForm):
    class Meta:
        model = StaffResignationRequest
        fields = ("requested_last_working_date", "reason")
        widgets = {
            "requested_last_working_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"},
            ),
            "reason": forms.Textarea(
                attrs={"class": "form-control", "rows": 3, "maxlength": 2000},
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        earliest_date = timezone.localdate() + timedelta(days=15)
        self.fields["requested_last_working_date"].widget.attrs["min"] = (
            earliest_date.isoformat()
        )

    def clean_requested_last_working_date(self):
        requested_date = self.cleaned_data["requested_last_working_date"]
        earliest_date = timezone.localdate() + timedelta(days=15)
        if requested_date < earliest_date:
            raise ValidationError(
                "Resignation requires at least 15 calendar days’ notice.",
            )
        return requested_date


class StorefrontProductEditForm(forms.ModelForm):
    barcode = forms.CharField(
        required=False,
        max_length=128,
        strip=True,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "autocomplete": "off",
            "data-sellerhelper-product-barcode": "true",
            "placeholder": "Scan or enter product barcode",
        }),
        label="Product barcode",
        help_text="Optional barcode used to add this product during a counter sale.",
    )

    unit_value = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=10,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0.001",
            "step": "0.001",
            "placeholder": "e.g. 500",
        }),
        label="Unit value",
        help_text="Amount in one sellable unit, such as 500 for 500 gm.",
    )
    unit_text = forms.CharField(
        required=False,
        max_length=50,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. gm, kg, pcs",
        }),
        label="Unit text",
    )
    shop_price = forms.DecimalField(
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "0.01",
        }),
        label="Price at this shop",
    )
    stock = forms.DecimalField(
        min_value=0,
        max_digits=14,
        decimal_places=4,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "0.0001",
        }),
        label="Available stock",
    )
    b2b_stock = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "0.0001",
        }),
        label="B2B available stock",
        help_text="Separate inventory used for B2B customer orders.",
    )
    b2b_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "0.01",
            "placeholder": "Leave blank to use regular price",
        }),
        label="B2B price before GST",
    )
    b2b_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.HiddenInput(),
    )
    b2b_unit_value = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=10,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0.001",
            "step": "0.001",
            "placeholder": "e.g. 500",
        }),
        label="B2B unit value",
    )
    b2b_unit_text = forms.CharField(
        required=False,
        max_length=30,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. gm, kg, pcs",
        }),
        label="B2B unit text",
        help_text="Leave both fields blank to use the product unit.",
    )
    bulk_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
        label="B2C bulk price before GST",
    )
    bulk_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. case"}),
        label="B2C bulk unit",
    )
    bulk_unit_value = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=10,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0.001",
            "step": "0.001",
            "placeholder": "e.g. 12",
        }),
        label="B2C bulk unit value",
        help_text="Amount in one B2C bulk unit, such as 12 packs.",
    )
    bulk_unit_text = forms.CharField(
        required=False,
        max_length=30,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. packs",
        }),
        label="B2C bulk unit text",
    )
    b2b_bulk_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01"}),
        label="B2B bulk price before GST",
    )
    b2b_bulk_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. case"}),
        label="B2B bulk unit",
    )
    b2b_bulk_unit_value = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=10,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0.001",
            "step": "0.001",
            "placeholder": "e.g. 24",
        }),
        label="B2B bulk unit value",
        help_text="Amount in one B2B bulk unit, such as 24 cartons.",
    )
    b2b_bulk_unit_text = forms.CharField(
        required=False,
        max_length=30,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. cartons",
        }),
        label="B2B bulk unit text",
    )
    bulk_min_quantity = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=32767,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
        label="Minimum bulk quantity",
    )
    shop_image_url = forms.URLField(
        required=False,
        max_length=1000,
        widget=forms.URLInput(attrs={"class": "form-control"}),
        label="Main product image URL for this shop",
        help_text="Shown first for this shop. Leave blank to use the admin catalog images.",
    )
    shop_image_url_2 = forms.URLField(
        required=False,
        max_length=1000,
        widget=forms.URLInput(attrs={"class": "form-control"}),
        label="Additional product image URL 2",
    )
    shop_image_url_3 = forms.URLField(
        required=False,
        max_length=1000,
        widget=forms.URLInput(attrs={"class": "form-control"}),
        label="Additional product image URL 3",
    )
    shop_image_url_4 = forms.URLField(
        required=False,
        max_length=1000,
        widget=forms.URLInput(attrs={"class": "form-control"}),
        label="Additional product image URL 4",
    )
    shop_image_url_5 = forms.URLField(
        required=False,
        max_length=1000,
        widget=forms.URLInput(attrs={"class": "form-control"}),
        label="Additional product image URL 5",
    )

    class Meta:
        model = Product
        fields = [
            "barcode",
            "product_alias_name",
            "product_title",
            "product_color",
            "product_size",
            "product_description",
        ]
        widgets = {
            "barcode": forms.TextInput(attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "Scan or enter product barcode",
            }),
            "product_alias_name": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 200,
                "placeholder": "Alternative or local product name",
            }),
            "product_title": forms.TextInput(attrs={"class": "form-control"}),
            "product_color": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 100,
                "placeholder": "Product color",
            }),
            "product_size": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 100,
                "placeholder": "Product size",
            }),
            "product_description": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 5,
            }),
        }

    def __init__(self, *args, product_shop, **kwargs):
        self.product_shop = product_shop
        super().__init__(*args, **kwargs)
        unit_value, unit_text = _split_product_unit(self.instance.unit)
        self.fields["unit_value"].initial = unit_value
        self.fields["unit_text"].initial = unit_text
        if product_shop.shop.allow_product_image_overrides:
            for field_name, model_field in (
                ("shop_image_url", "image_url_override"),
                ("shop_image_url_2", "image_url_override_2"),
                ("shop_image_url_3", "image_url_override_3"),
                ("shop_image_url_4", "image_url_override_4"),
                ("shop_image_url_5", "image_url_override_5"),
            ):
                self.fields[field_name].initial = getattr(product_shop, model_field)
        else:
            for field_name in (
                "shop_image_url",
                "shop_image_url_2",
                "shop_image_url_3",
                "shop_image_url_4",
                "shop_image_url_5",
            ):
                self.fields.pop(field_name)
        self.fields["shop_price"].initial = (
            product_shop.price
            if product_shop.price is not None
            else product_shop.product.price
        )
        self.fields["stock"].initial = product_shop.stock
        self.fields["b2b_stock"].initial = product_shop.b2b_stock
        self.fields["b2b_price"].initial = product_shop.b2b_price
        self.fields["b2b_unit"].initial = product_shop.b2b_unit
        b2b_unit_value, b2b_unit_text = _split_product_unit(product_shop.b2b_unit)
        self.fields["b2b_unit_value"].initial = b2b_unit_value
        self.fields["b2b_unit_text"].initial = b2b_unit_text
        self.fields["bulk_price"].initial = product_shop.bulk_price
        bulk_unit_value, bulk_unit_text = _split_product_unit(product_shop.bulk_unit)
        self.fields["bulk_unit"].initial = product_shop.bulk_unit
        self.fields["bulk_unit_value"].initial = bulk_unit_value
        self.fields["bulk_unit_text"].initial = bulk_unit_text
        self.fields["b2b_bulk_price"].initial = product_shop.b2b_bulk_price
        b2b_bulk_unit_value, b2b_bulk_unit_text = _split_product_unit(
            product_shop.b2b_bulk_unit
        )
        self.fields["b2b_bulk_unit"].initial = product_shop.b2b_bulk_unit
        self.fields["b2b_bulk_unit_value"].initial = b2b_bulk_unit_value
        self.fields["b2b_bulk_unit_text"].initial = b2b_bulk_unit_text
        self.fields["bulk_min_quantity"].initial = product_shop.bulk_min_quantity

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("unit_value") is not None and not cleaned_data.get("unit_text"):
            self.add_error("unit_text", "Enter the unit when setting a unit value.")
        if (
            cleaned_data.get("b2b_unit_value") is not None
            and not cleaned_data.get("b2b_unit_text")
        ):
            self.add_error("b2b_unit_text", "Enter unit text when setting a unit value.")
        if self.is_bound and "b2b_unit_value" in self.data and "b2b_unit_text" in self.data:
            combined_b2b_unit = _join_product_unit(
                cleaned_data.get("b2b_unit_value"),
                cleaned_data.get("b2b_unit_text"),
            )
            if len(combined_b2b_unit) > 40:
                self.add_error("b2b_unit_text", "The combined B2B unit must be 40 characters or fewer.")
            cleaned_data["b2b_unit"] = combined_b2b_unit
        for price_field, unit_field, value_field, text_field in (
            ("bulk_price", "bulk_unit", "bulk_unit_value", "bulk_unit_text"),
            (
                "b2b_bulk_price",
                "b2b_bulk_unit",
                "b2b_bulk_unit_value",
                "b2b_bulk_unit_text",
            ),
        ):
            unit_value = cleaned_data.get(value_field)
            unit_text = cleaned_data.get(text_field)
            if unit_value is not None and not unit_text:
                self.add_error(text_field, "Enter unit text when setting a unit value.")
            combined_unit = _join_bulk_unit(unit_value, unit_text)
            if not combined_unit:
                combined_unit = cleaned_data.get(unit_field, "")
            if len(combined_unit) > 40:
                self.add_error(text_field, "The combined bulk unit must be 40 characters or fewer.")
            cleaned_data[unit_field] = combined_unit
            if cleaned_data.get(price_field) is not None and not combined_unit:
                self.add_error(text_field, "Enter a unit when setting a bulk price.")
        return cleaned_data

    def save(self, commit=True):
        self.instance.unit = _join_product_unit(
            self.cleaned_data.get("unit_value"),
            self.cleaned_data.get("unit_text"),
        )
        product = super().save(commit=commit)
        if self.product_shop.shop.allow_product_image_overrides:
            for field_name, model_field in (
                ("shop_image_url", "image_url_override"),
                ("shop_image_url_2", "image_url_override_2"),
                ("shop_image_url_3", "image_url_override_3"),
                ("shop_image_url_4", "image_url_override_4"),
                ("shop_image_url_5", "image_url_override_5"),
            ):
                setattr(self.product_shop, model_field, self.cleaned_data[field_name])
        return product


class ClientDeliveryAddressForm(forms.ModelForm):

    class Meta:
        model = ClientDeliveryAddress

        fields = [
            "label",
            "address",
            "city",
            "pincode",
            "latitude",
            "longitude",
            "is_active",
            "is_default",
        ]

        widgets = {

            "label": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Mumbai - 400001",
                }
            ),

            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Delivery address",
                }
            ),

            "city": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),

            "pincode": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),

            "latitude": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "any",
                    "readonly": "readonly",
                }
            ),

            "longitude": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "any",
                    "readonly": "readonly",
                }
            ),

            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),

            "is_default": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }

class ProductCategoryForm(forms.ModelForm):
    market_category = forms.ChoiceField(
        choices=(
            ("", "Select a market"),
            *ProductCategory.MARKET_CHOICES,
        ),
        label="Marketplace",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ProductCategory
        fields = ("name", "market_category", "sort_order")
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. ORGANIC FOODS",
                "maxlength": 80,
            }),
            "sort_order": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
        }

    def clean_name(self):
        return self.cleaned_data["name"].strip().upper()


class MarketplaceLayoutSettingsForm(forms.ModelForm):
    class Meta:
        model = MarketplaceLayoutSettings
        fields = ("products_per_row", "rows_per_category")
        widgets = {
            "products_per_row": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 1,
                "max": 8,
                "step": 1,
            }),
            "rows_per_category": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 1,
                "max": 4,
                "step": 1,
            }),
        }


class ProductCloneSettingsForm(forms.ModelForm):
    allowed_fields = forms.MultipleChoiceField(
        choices=PRODUCT_CLONE_FIELD_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={
            "class": "form-check-input",
        }),
        help_text=(
            "Only selected details are copied from a marketplace product. "
            "Name and category are always used; stock always starts at zero."
        ),
    )

    class Meta:
        model = ProductCloneSettings
        fields = ("allowed_fields",)


class PublicRiderSettingsForm(forms.ModelForm):
    class Meta:
        model = DeliverySharingSettings
        labels = {
            "public_rider_service_enabled": "Enable public rider network",
            "max_additional_pickup_locations": "Additional pickup locations per shop",
            "shared_rider_radius_km": "Shared rider coverage radius (km)",
            "customer_direction_radius_km": "Customer-direction grouping radius (km)",
            "public_rider_batch_radius_km": "Public rider order-batching radius (km)",
            "public_rider_decline_cooldown_minutes": "Decline cooldown (minutes)",
            "public_rider_offer_timeout_seconds": "Maximum offer response time (seconds)",
            "public_rider_offer_riders_per_round": "Riders notified per offer round",
            "public_rider_max_active_offers": "Active offers per rider",
            "shared_rider_base_pay": "Public rider base fare (₹)",
            "shared_rider_rate_per_km": "Standard delivery rate (₹/km)",
            "public_rider_pay_include_pickup_distance": (
                "Include rider-to-pickup distance in standard fare"
            ),
            "public_rider_pickup_distance_pay_percentage": (
                "Pickup distance included in standard fare (%)"
            ),
            "public_rider_pickup_threshold_bonus_enabled": (
                "Enable additional pickup-distance threshold bonus"
            ),
            "public_rider_pickup_threshold_km": (
                "Pickup distance covered before extra pay (km)"
            ),
            "public_rider_pickup_threshold_rate_per_km": (
                "Extra pay beyond threshold (₹/km)"
            ),
            "public_rider_pickup_threshold_distance_method": (
                "Distance method for extra threshold bonus"
            ),
        }
        fields = (
            "public_rider_service_enabled",
            "max_additional_pickup_locations",
            "shared_rider_radius_km",
            "customer_direction_radius_km",
            "public_rider_batch_radius_km",
            "public_rider_decline_cooldown_minutes",
            "public_rider_offer_timeout_seconds",
            "public_rider_offer_riders_per_round",
            "public_rider_max_active_offers",
            "shared_rider_base_pay",
            "shared_rider_rate_per_km",
            "public_rider_pay_include_pickup_distance",
            "public_rider_pickup_distance_pay_percentage",
            "public_rider_pickup_threshold_bonus_enabled",
            "public_rider_pickup_threshold_km",
            "public_rider_pickup_threshold_rate_per_km",
            "public_rider_pickup_threshold_distance_method",
        )
        widgets = {
            "public_rider_service_enabled": forms.CheckboxInput(
                attrs={"class": "form-check-input"},
            ),
            "max_additional_pickup_locations": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": 20},
            ),
            "shared_rider_radius_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.05, "max": 10, "step": 0.01},
            ),
            "customer_direction_radius_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.05, "max": 10, "step": 0.01},
            ),
            "public_rider_batch_radius_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.5, "max": 4.2, "step": 0.1},
            ),
            "public_rider_decline_cooldown_minutes": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.5, "max": 60, "step": 0.1},
            ),
            "public_rider_offer_timeout_seconds": forms.NumberInput(
                attrs={"class": "form-control", "min": 15, "max": 600, "step": 1},
            ),
            "public_rider_offer_riders_per_round": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 100, "step": 1},
            ),
            "public_rider_max_active_offers": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 20, "step": 1},
            ),
            "shared_rider_base_pay": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": 0.01},
            ),
            "shared_rider_rate_per_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": 0.01},
            ),
            "public_rider_pay_include_pickup_distance": forms.CheckboxInput(
                attrs={"class": "form-check-input"},
            ),
            "public_rider_pickup_distance_pay_percentage": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": 100, "step": 0.01},
            ),
            "public_rider_pickup_threshold_bonus_enabled": forms.CheckboxInput(
                attrs={"class": "form-check-input"},
            ),
            "public_rider_pickup_threshold_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": 0.001},
            ),
            "public_rider_pickup_threshold_rate_per_km": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": 0.01},
            ),
            "public_rider_pickup_threshold_distance_method": forms.Select(
                attrs={"class": "form-select"},
            ),
        }


class SiteBrandingSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteBrandingSettings
        fields = (
            "site_name",
            "tagline",
            "support_email",
            "support_phone",
            "support_address",
            "android_app_url",
            "ios_app_url",
            "logo",
            "app_icon",
            "favicon",
        )
        widgets = {
            "site_name": forms.TextInput(attrs={"class": "form-control"}),
            "tagline": forms.TextInput(attrs={"class": "form-control"}),
            "support_email": forms.EmailInput(attrs={"class": "form-control"}),
            "support_phone": forms.TextInput(attrs={"class": "form-control"}),
            "support_address": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 2,
            }),
            "android_app_url": forms.URLInput(attrs={"class": "form-control"}),
            "ios_app_url": forms.URLInput(attrs={"class": "form-control"}),
            "logo": forms.FileInput(attrs={"class": "form-control"}),
            "app_icon": forms.FileInput(attrs={"class": "form-control"}),
            "favicon": forms.FileInput(attrs={"class": "form-control"}),
        }


class SiteThemeSettingsForm(forms.ModelForm):
    desktop_layout_order = forms.CharField(widget=forms.HiddenInput)
    mobile_layout_order = forms.CharField(widget=forms.HiddenInput)
    component_layouts = forms.JSONField(
        required=False,
        widget=forms.HiddenInput,
    )
    marketplace_featured_product_ids = forms.MultipleChoiceField(
        choices=(),
        required=False,
        widget=forms.SelectMultiple(attrs={
            "class": "form-select",
            "size": 8,
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.marketplace_category_choices = get_product_category_choices(
            include_inactive=True,
        )
        self.fields["marketplace_featured_product_ids"].choices = [
            (str(product.pk), f"{product.product_name} · {product.product_category}")
            for product in AdminProductCatalog.objects.order_by(
                "product_category",
                "product_name",
            )
        ]
        if not self.is_bound:
            current_category_order = self.instance.marketplace_category_order
            default_category_order = ",".join(
                name for name, _label in self.marketplace_category_choices
            )
            self.initial.setdefault(
                "marketplace_category_order",
                current_category_order or default_category_order,
            )
            self.initial.setdefault(
                "marketplace_featured_product_ids",
                self.instance.marketplace_featured_product_ids or [],
            )

    class Meta:
        model = SiteThemeSettings
        fields = (
            "primary_color",
            "accent_color",
            "page_background",
            "surface_color",
            "text_color",
            "font_family",
            "desktop_content_width",
            "desktop_page_padding",
            "mobile_page_padding",
            "mobile_topbar_scroll_hide_enabled",
            "mobile_bottombar_scroll_hide_enabled",
            "mobile_app_desktop_view_enabled",
            "component_radius",
            "header_padding",
            "content_padding",
            "footer_padding",
            "header_margin",
            "content_margin",
            "footer_margin",
            "desktop_layout_order",
            "mobile_layout_order",
            "homepage_hero_enabled",
            "homepage_features_enabled",
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
            "marketplace_hero_enabled",
            "marketplace_nearby_shops_enabled",
            "marketplace_category_nav_enabled",
            "marketplace_products_enabled",
            "marketplace_hero_title",
            "marketplace_hero_text",
            "marketplace_section_order",
            "marketplace_category_order",
            "marketplace_featured_product_ids",
            "marketplace_primary_color",
            "marketplace_accent_color",
            "marketplace_header_start_color",
            "marketplace_header_end_color",
            "marketplace_card_radius",
            "marketplace_section_gap",
            "marketplace_product_gap",
            "marketplace_content_width",
            "marketplace_product_width",
            "marketplace_desktop_padding",
            "marketplace_mobile_padding",
            "theme_revision_retention_days",
            "component_layouts",
        )
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "accent_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "page_background": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "surface_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "text_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "font_family": forms.TextInput(attrs={"class": "form-control"}),
            "desktop_content_width": forms.NumberInput(attrs={"class": "form-control", "min": 800, "max": 1800, "step": 10}),
            "desktop_page_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 64, "step": 1}),
            "mobile_page_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 32, "step": 1}),
            "mobile_topbar_scroll_hide_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "mobile_bottombar_scroll_hide_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "mobile_app_desktop_view_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "component_radius": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 32, "step": 1}),
            "header_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 32, "step": 1}),
            "content_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 80, "step": 1}),
            "footer_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 64, "step": 1}),
            "header_margin": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 48, "step": 1}),
            "content_margin": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 48, "step": 1}),
            "footer_margin": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 48, "step": 1}),
            "homepage_hero_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "homepage_features_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "homepage_hero_badge": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_hero_title": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_hero_text": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "homepage_primary_cta": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_secondary_cta": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_feature_1_title": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_feature_1_text": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "homepage_feature_2_title": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_feature_2_text": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "homepage_feature_3_title": forms.TextInput(attrs={"class": "form-control"}),
            "homepage_feature_3_text": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "homepage_section_order": forms.HiddenInput(),
            "marketplace_hero_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "marketplace_nearby_shops_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "marketplace_category_nav_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "marketplace_products_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "marketplace_hero_title": forms.TextInput(attrs={"class": "form-control"}),
            "marketplace_hero_text": forms.TextInput(attrs={"class": "form-control"}),
            "marketplace_section_order": forms.HiddenInput(),
            "marketplace_category_order": forms.HiddenInput(),
            "marketplace_primary_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "marketplace_accent_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "marketplace_header_start_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "marketplace_header_end_color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
            "marketplace_card_radius": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 32}),
            "marketplace_section_gap": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 64}),
            "marketplace_product_gap": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 32}),
            "marketplace_content_width": forms.NumberInput(attrs={"class": "form-control", "min": 800, "max": 3000, "step": 10}),
            "marketplace_product_width": forms.NumberInput(attrs={"class": "form-control", "min": 120, "max": 320, "step": 10}),
            "marketplace_desktop_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 48}),
            "marketplace_mobile_padding": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 24}),
            "theme_revision_retention_days": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 3650}),
            "component_layouts": forms.HiddenInput(),
        }

    def clean(self):
        cleaned_data = super().clean()
        component_names = {
            "site_header",
            "site_content",
            "site_footer",
            "home_content",
            "home_hero",
            "home_features",
            "home_feature_card",
            "marketplace_page",
            "marketplace_hero",
            "marketplace_search",
            "marketplace_nearby",
            "marketplace_category_nav",
            "marketplace_products",
            "marketplace_category",
            "marketplace_product_row",
            "marketplace_product",
            "marketplace_product_image",
        }
        layout_properties = {
            "width",
            "height",
            "min_width",
            "max_width",
            "min_height",
            "max_height",
            "margin_x",
            "margin_y",
            "margin_top",
            "margin_right",
            "margin_bottom",
            "margin_left",
            "padding_x",
            "padding_y",
            "padding_top",
            "padding_right",
            "padding_bottom",
            "padding_left",
            "gap_x",
            "gap_y",
            "border_radius",
            "visible",
            "object_fit",
        }
        component_layouts = cleaned_data.get("component_layouts") or {}
        if not isinstance(component_layouts, dict):
            self.add_error(
                "component_layouts",
                "Component layout settings must be a JSON object.",
            )
        else:
            normalized_layouts = {}
            for component_name, breakpoints in component_layouts.items():
                if component_name not in component_names or not isinstance(breakpoints, dict):
                    self.add_error(
                        "component_layouts",
                        "Component layout settings contain an unknown component or invalid breakpoint data.",
                    )
                    continue
                normalized_breakpoints = {}
                for breakpoint, values in breakpoints.items():
                    if breakpoint not in {"desktop", "mobile"} or not isinstance(values, dict):
                        self.add_error(
                            "component_layouts",
                            "Component layout settings must use desktop or mobile values.",
                        )
                        continue
                    normalized_values = {}
                    for property_name, value in values.items():
                        if property_name == "visible" and isinstance(value, bool):
                            normalized_values[property_name] = value
                            continue
                        if property_name == "object_fit":
                            if (
                                isinstance(value, str)
                                and value in {"contain", "cover", "fill", "scale-down"}
                            ):
                                normalized_values[property_name] = value
                            else:
                                self.add_error(
                                    "component_layouts",
                                    "Product image fit must be contain, cover, fill, or scale-down.",
                                )
                            continue
                        if (
                            property_name not in layout_properties
                            or isinstance(value, bool)
                            or not isinstance(value, int)
                            or not 0 <= value <= 3000
                        ):
                            self.add_error(
                                "component_layouts",
                                "Component layout values must be whole pixels between 0 and 3000.",
                            )
                            continue
                        normalized_values[property_name] = value
                    if normalized_values:
                        normalized_breakpoints[breakpoint] = normalized_values
                if normalized_breakpoints:
                    normalized_layouts[component_name] = normalized_breakpoints
            cleaned_data["component_layouts"] = normalized_layouts

        expected = {"header", "content", "footer"}
        for field_name in ("desktop_layout_order", "mobile_layout_order"):
            raw_order = cleaned_data.get(field_name, "")
            order = [part.strip() for part in raw_order.split(",")]
            if len(order) != len(expected) or set(order) != expected:
                self.add_error(
                    field_name,
                    "Arrange header, main content, and footer exactly once.",
                )
        homepage_order = [
            part.strip()
            for part in self.cleaned_data.get("homepage_section_order", "").split(",")
        ]
        if len(homepage_order) != 2 or set(homepage_order) != {"hero", "features"}:
            self.add_error(
                "homepage_section_order",
                "Arrange the welcome banner and feature cards exactly once.",
            )
        marketplace_sections = {"hero", "search", "nearby", "category_nav", "products"}
        marketplace_order = [
            part.strip()
            for part in cleaned_data.get("marketplace_section_order", "").split(",")
        ]
        if (
            len(marketplace_order) != len(marketplace_sections)
            or set(marketplace_order) != marketplace_sections
        ):
            self.add_error(
                "marketplace_section_order",
                "Arrange all marketplace sections exactly once.",
            )
        allowed_categories = {
            name for name, _label in self.marketplace_category_choices
        }
        category_order = [
            part.strip()
            for part in cleaned_data.get("marketplace_category_order", "").split(",")
            if part.strip()
        ]
        if category_order and (
            len(category_order) != len(set(category_order))
            or not set(category_order).issubset(allowed_categories)
        ):
            self.add_error(
                "marketplace_category_order",
                "Choose marketplace categories from the available category list.",
            )
        for field_name in (
            "primary_color",
            "accent_color",
            "page_background",
            "surface_color",
            "text_color",
            "marketplace_primary_color",
            "marketplace_accent_color",
            "marketplace_header_start_color",
            "marketplace_header_end_color",
        ):
            color = cleaned_data.get(field_name, "")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                self.add_error(field_name, "Enter a six-digit hexadecimal color.")
        font_family = cleaned_data.get("font_family", "")
        if not re.fullmatch(r"[A-Za-z0-9 ,\-]{1,120}", font_family):
            self.add_error("font_family", "Use a plain CSS font-family list.")
        return cleaned_data


class SharedRiderGroupCreateForm(forms.ModelForm):
    class Meta:
        model = SharedRiderGroup
        fields = ("name", "created_from_shop")
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 120,
                "placeholder": "e.g. Central Market Riders",
            }),
            "created_from_shop": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["created_from_shop"].queryset = Shop.objects.filter(
            owner=owner,
            allow_owner_create_shared_rider_groups=True,
        ).order_by("name")
        self.fields["created_from_shop"].label = "Group's managing shop"


class SharedRiderShopRequestForm(forms.Form):
    shop_id = forms.IntegerField(
        min_value=1,
        label="Shop ID to request",
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": 1,
            "placeholder": "Enter shop ID",
        }),
    )


class AdminProductCatalogForm(forms.ModelForm):
    product_category = forms.ChoiceField(
        choices=(),
        label="Product category",
        widget=forms.Select(attrs={
            "class": "form-select",
            "data-catalog-product-category": "catalog-form",
        }),
    )

    class Meta:
        model = AdminProductCatalog
        fields = (
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
        )
        widgets = {
            "product_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Product name",
                "maxlength": 200,
                "data-hsn-product-name": "catalog-form",
            }),
            "image_url": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "image_url_2": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "image_url_3": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "image_url_4": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "image_url_5": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "hsn_code": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 8,
                "placeholder": "2, 4, 6, or 8 digits",
                "data-hsn-product-code": "catalog-form",
            }),
            "sac_code": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 6,
                "placeholder": "2, 4, or 6 digits",
                "data-sac-product-code": "catalog-form",
            }),
            "gst_rate": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
                "max": 100,
                "step": "0.01",
            }),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image_url"].required = True
        self.fields["image_url"].label = "Image URL 1 (required)"
        for field_name in ("image_url_2", "image_url_3", "image_url_4", "image_url_5"):
            self.fields[field_name].label = f"Image URL {field_name[-1]} (optional)"
        category_choices = get_product_category_choices()
        current_category = getattr(self.instance, "product_category", "")
        if current_category and current_category not in dict(category_choices):
            category_choices.append((current_category, current_category))
        self.fields["product_category"].choices = [
            ("", "Select a category"),
            *category_choices,
        ]

    def clean_hsn_code(self):
        hsn_code = (self.cleaned_data.get("hsn_code") or "").strip()
        if hsn_code and not re.fullmatch(r"\d{2}(?:\d{2}){0,3}", hsn_code):
            raise ValidationError("Enter an HSN code with 2, 4, 6, or 8 digits.")
        return hsn_code

    def clean_sac_code(self):
        sac_code = (self.cleaned_data.get("sac_code") or "").strip()
        if sac_code and not re.fullmatch(r"\d{2}(?:\d{2}){0,2}", sac_code):
            raise ValidationError("Enter a SAC code with 2, 4, or 6 digits.")
        return sac_code

    def clean_gst_rate(self):
        gst_rate = self.cleaned_data.get("gst_rate")
        if gst_rate is not None and not Decimal("0.00") <= gst_rate <= Decimal("100.00"):
            raise ValidationError("GST rate must be between 0 and 100 percent.")
        return gst_rate


class ProductForm(forms.ModelForm):

    barcode = forms.CharField(
        required=False,
        max_length=128,
        strip=True,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "autocomplete": "off",
            "data-sellerhelper-product-barcode": "true",
            "placeholder": "Scan or enter product barcode",
        }),
        label="Product barcode",
        help_text="Optional barcode used to add this product during a counter sale.",
    )

    unit_value = forms.DecimalField(
        required=False,
        min_value=Decimal("0.001"),
        max_digits=10,
        decimal_places=3,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0.001",
            "step": "0.001",
            "placeholder": "e.g. 500",
        }),
        label="Unit value",
        help_text="Amount in one sellable unit, such as 500 for 500 gm.",
    )
    unit_text = forms.CharField(
        required=False,
        max_length=50,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. gm, kg, pcs",
        }),
        label="Unit text",
    )

    product_category = forms.ChoiceField(
        required=True,
        choices=(),
        label="Product category",
        widget=forms.Select(attrs={
            "class": "form-select",
            "data-product-category-select": "true",
        }),
    )

    product_name = forms.ChoiceField(
        required=True,
        choices=(),
        label="Product name",
        widget=forms.Select(attrs={
            "class": "form-select",
            "data-product-name-select": "true",
        }),
    )

    def __init__(self, *args, user=None, **kwargs):
        product_name_readonly = kwargs.pop("product_name_readonly", False)
        self.allow_custom_product_name = kwargs.pop(
            "allow_custom_product_name",
            False,
        )
        self.user = user
        super().__init__(*args, **kwargs)
        self.order_fields([
            "product_category",
            "product_name",
            "price",
            "unit_value",
            "unit_text",
        ])
        unit_value, unit_text = _split_product_unit(self.instance.unit)
        self.fields["unit_value"].initial = unit_value
        self.fields["unit_text"].initial = unit_text

        category = (
            self.data.get("product_category")
            or self.initial.get("product_category")
            or (
                getattr(self.instance, "product_category", "")
                if self.instance and self.instance.pk
                else ""
            )
        )

        self.catalog_data = self.get_catalog_map()
        self.catalog_defaults = self.get_catalog_defaults()
        self.catalog_products = (
            self.get_catalog_products()
            if self.allow_custom_product_name
            else []
        )
        category_choices = get_product_category_choices()
        known_categories = dict(category_choices)
        for product in self.catalog_products:
            product_category = product["product_category"]
            if product_category and product_category not in known_categories:
                category_choices.append((product_category, product_category))
                known_categories[product_category] = product_category
        current_category = (
            self.instance.product_category
            if self.instance and self.instance.pk
            else ""
        )
        if current_category and current_category not in dict(category_choices):
            category_choices.append((
                current_category,
                current_category,
            ))
        self.fields["product_category"].choices = [
            ("", "---------"),
            *category_choices,
        ]
        self.fields["gst_rate"].required = False
        self.fields["product_name"].choices = self.get_product_name_choices(category)
        if self.allow_custom_product_name:
            self.fields["product_name"] = forms.CharField(
                required=True,
                max_length=200,
                label="Product name",
                help_text="Enter a new name, or select a listed product to fill its details.",
                widget=forms.TextInput(attrs={
                    "class": "form-control",
                    "list": "product-name-options",
                    "autocomplete": "off",
                    "data-product-name-autofill": "true",
                }),
            )

        if self.instance and self.instance.pk and self.instance.product_name:
            self.fields["product_name"].initial = self.instance.product_name
        if product_name_readonly:
            self.fields["product_name"].disabled = True
            self.fields["product_name"].help_text = (
                "Product names can only be changed in the admin shared catalog."
            )
        if self.user and not self.user.is_superuser:
            self.fields["image_url"].disabled = True
            self.fields["image_url"].help_text = (
                "The admin catalog image is used unless this shop has image override permission."
            )

    def get_catalog_queryset(self, category=None):
        qs = Product.objects.exclude(product_name="")
        if category:
            qs = qs.filter(product_category=category)
        if not self.user:
            qs = qs.filter(created_by__is_staff=True)
        elif not getattr(self.user, "is_superuser", False):
            qs = qs.filter(
                Q(created_by=self.user)
                | Q(created_by__is_staff=True)
            )
        return qs.order_by("product_name")

    def get_catalog_defaults(self):
        catalog = {}
        for row in self.get_catalog_queryset().filter(
            created_by__is_staff=True,
            is_active=True,
        ).values(
            "product_category",
            "product_name",
            "image_url",
            "hsn_code",
            "gst_rate",
        ):
            catalog.setdefault(row["product_category"], {})[row["product_name"]] = {
                "image_url": row["image_url"],
                "hsn_code": row["hsn_code"],
                "gst_rate": str(row["gst_rate"]),
            }
        rows = AdminProductCatalog.objects.filter(is_active=True).values(
            "product_category",
            "product_name",
            "image_url",
            "hsn_code",
            "gst_rate",
        )
        for row in rows:
            catalog.setdefault(row["product_category"], {})[row["product_name"]] = {
                "image_url": row["image_url"],
                "hsn_code": row["hsn_code"],
                "gst_rate": str(row["gst_rate"]),
            }
        return catalog

    def get_catalog_products(self):
        products = {}
        product_fields = (
            "sku",
            "product_category",
            "product_name",
            "product_alias_name",
            "product_title",
            "product_color",
            "product_size",
            "price",
            "unit",
            "image_url",
            "product_description",
            "hsn_code",
            "gst_rate",
            "shelf_life_days",
            "expiry_price_drop_percent",
            "expiry_management_enabled",
            "is_approximate_weight",
            "is_active",
        )
        for row in self.get_catalog_queryset().filter(
            is_active=True,
        ).values(*product_fields):
            row["price"] = str(row["price"])
            row["gst_rate"] = str(row["gst_rate"])
            row["expiry_price_drop_percent"] = str(row["expiry_price_drop_percent"])
            row["unit_value"], row["unit_text"] = _split_product_unit(row["unit"])
            products[(row["product_category"], row["product_name"])] = row

        for row in AdminProductCatalog.objects.filter(
            is_active=True,
        ).values(
            "product_category",
            "product_name",
            "image_url",
            "hsn_code",
            "gst_rate",
        ):
            key = (row["product_category"], row["product_name"])
            details = products.setdefault(
                key,
                {
                    "sku": "",
                    "product_category": row["product_category"],
                    "product_name": row["product_name"],
                    "product_alias_name": "",
                    "product_title": "",
                    "product_color": "",
                    "product_size": "",
                    "price": "",
                    "unit": "",
                    "unit_value": "",
                    "unit_text": "",
                    "image_url": "",
                    "product_description": "",
                    "hsn_code": "",
                    "gst_rate": "0.00",
                    "shelf_life_days": "",
                    "expiry_price_drop_percent": "0.00",
                    "expiry_management_enabled": False,
                    "is_approximate_weight": False,
                    "is_active": True,
                },
            )
            details["image_url"] = row["image_url"] or details["image_url"]
            details["hsn_code"] = row["hsn_code"] or details["hsn_code"]
            details["gst_rate"] = str(row["gst_rate"])
        return list(products.values())

    def get_catalog_map(self):
        catalog = {}

        for category, _ in get_product_category_choices(include_inactive=True):
            entry_map = {}
            if self.user:
                for product_name, image_url in Product.objects.filter(
                    created_by=self.user,
                    product_category=category,
                ).exclude(product_name="").values_list("product_name", "image_url"):
                    key = (product_name or "").strip()
                    if key:
                        entry_map[key] = image_url or ""

            for product_name, image_url in Product.objects.filter(
                product_category=category,
                created_by__is_staff=True,
                is_active=True,
            ).exclude(product_name="").values_list("product_name", "image_url"):
                key = (product_name or "").strip()
                if key:
                    entry_map[key] = image_url or ""

            for product_name, image_url in AdminProductCatalog.objects.filter(
                product_category=category,
                is_active=True,
            ).values_list("product_name", "image_url"):
                key = (product_name or "").strip()
                if key:
                    entry_map[key] = image_url or ""

            if entry_map:
                catalog[category] = entry_map

        return catalog

    def get_admin_catalog_product(self, category, product_name):
        if not category or not product_name:
            return None
        return AdminProductCatalog.objects.filter(
            product_category=category,
            product_name=product_name,
            is_active=True,
        ).first()

    def get_catalog_data_for_category(self, category=None):
        if not category:
            return {}

        return self.get_catalog_map().get(category, {})

    def get_product_name_choices(self, category=None):
        choices = [("", "---------")]

        if not category:
            return choices

        product_names = self.get_catalog_data_for_category(category).keys()

        for product_name in sorted(product_names):
            choices.append((product_name, product_name))

        return choices

    def get_catalog_image_url_for_name(self, product_name):
        if not product_name:
            return ""

        return self.get_catalog_data_for_category(
            self.data.get("product_category") or self.initial.get("product_category") or getattr(self.instance, "product_category", "")
        ).get(product_name, "")

    def clean_product_name(self):
        return self.cleaned_data.get("product_name")

    def clean_hsn_code(self):
        hsn_code = (self.cleaned_data.get("hsn_code") or "").strip()
        if hsn_code and not re.fullmatch(r"\d{4}(?:\d{2}){0,2}", hsn_code):
            raise ValidationError("Enter an HSN code with 4, 6, or 8 digits.")
        return hsn_code

    def clean_gst_rate(self):
        gst_rate = self.cleaned_data.get("gst_rate")
        if gst_rate is not None and not Decimal("0.00") <= gst_rate <= Decimal("100.00"):
            raise ValidationError("GST rate must be between 0 and 100 percent.")
        return gst_rate

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("unit_value") is not None and not cleaned_data.get("unit_text"):
            self.add_error("unit_text", "Enter the unit when setting a unit value.")
        category = cleaned_data.get("product_category")
        product_name = cleaned_data.get("product_name")
        if self.allow_custom_product_name and product_name:
            matching_categories = {
                item["product_category"]
                for item in self.catalog_products
                if item["product_name"].strip().casefold()
                == product_name.strip().casefold()
            }
            if matching_categories and category not in matching_categories:
                self.add_error(
                    "product_category",
                    "This listed product belongs to a different category. Select the product again to fill its category.",
                )
        defaults = self.catalog_defaults.get(category, {}).get(product_name)

        if defaults:
            if self.user and not self.user.is_superuser:
                cleaned_data["image_url"] = defaults["image_url"]
            if not cleaned_data.get("image_url"):
                cleaned_data["image_url"] = defaults["image_url"]
            if not cleaned_data.get("hsn_code"):
                cleaned_data["hsn_code"] = defaults["hsn_code"]
            if cleaned_data.get("gst_rate") is None:
                try:
                    cleaned_data["gst_rate"] = Decimal(defaults["gst_rate"])
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError(
                        "The selected catalog product has an invalid default GST rate."
                    )
        elif self.user and not self.user.is_superuser:
            cleaned_data["image_url"] = ""
        elif category and product_name and not cleaned_data.get("image_url"):
            cleaned_data["image_url"] = self.get_catalog_data_for_category(
                category
            ).get(product_name, "")

        if cleaned_data.get("gst_rate") is None:
            cleaned_data["gst_rate"] = Decimal("0.00")
        if cleaned_data.get("gst_rate") and not cleaned_data.get("hsn_code"):
            self.add_error("hsn_code", "HSN code is required when GST applies.")
        return cleaned_data

    def save(self, commit=True):
        self.instance.unit = _join_product_unit(
            self.cleaned_data.get("unit_value"),
            self.cleaned_data.get("unit_text"),
        )
        instance = super().save(commit=False)
        if commit:
            instance.save()

        return instance

    class Meta:
        model = Product

        fields = [
            "sku",
            "barcode",
            "product_category",
            "product_name",
            "product_alias_name",
            "product_title",
            "product_color",
            "product_size",
            "price",
            "hsn_code",
            "gst_rate",
            "image_url",
            "shelf_life_days",
            "expiry_price_drop_percent",
            "expiry_management_enabled",
            "is_approximate_weight",
            "product_description",
            "is_active",
        ]

        widgets = {

            "sku": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "SKU",
                }
            ),

            "product_category": forms.Select(
                attrs={
                    "class": "form-select",
                    "data-product-category-select": "true",
                }
            ),

            "product_name": forms.Select(
                attrs={
                    "class": "form-select",
                    "data-product-name-select": "true",
                }
            ),

            "product_alias_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Alias name",
                }
            ),

            "product_title": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Product title",
                }
            ),

            "product_color": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Color",
                }
            ),

            "product_size": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Size",
                }
            ),

            "price": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                }
            ),
            "hsn_code": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "maxlength": "8",
                    "placeholder": "HSN code (4, 6, or 8 digits)",
                }
            ),
            "gst_rate": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "0",
                    "max": "100",
                    "step": "0.01",
                }
            ),

            "image_url": forms.URLInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://...",
                    "data-product-image-url": "true",
                }
            ),

            "shelf_life_days": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "expiry_price_drop_percent": forms.NumberInput(attrs={"class": "form-control", "min": "0", "max": "100", "step": "0.01"}),
            "expiry_management_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_approximate_weight": forms.CheckboxInput(attrs={"class": "form-check-input"}),

            "product_description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Product description",
                }
            ),

            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }


from django import forms
from .models import ProductShop

class ProductShopAssignmentForm(forms.Form):

    shop = forms.ModelChoiceField(
        queryset=Shop.objects.none(),
        widget=forms.HiddenInput()
    )

    assigned = forms.BooleanField(
        required=False,
        label="Assign"
    )

    price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "Default price"
            }
        ),
        label="Shop Price"
    )

    b2b_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.01",
            "min": "0",
            "placeholder": "Optional price before GST",
        }),
        label="B2B Price",
    )
    b2b_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Defaults to product unit"}),
        label="B2B unit",
    )
    bulk_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="B2C bulk price before GST",
    )
    bulk_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. case"}),
        label="B2C bulk unit",
    )
    b2b_bulk_price = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="B2B bulk price before GST",
    )
    b2b_bulk_unit = forms.CharField(
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. case"}),
        label="B2B bulk unit",
    )
    bulk_min_quantity = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=32767,
        initial=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
        label="Minimum bulk quantity",
    )

    stock = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0",
                "step": "0.0001",
            }
        )
    )

    is_active = forms.BooleanField(
        required=False,
        initial=True,
        label="Active"
    )

    def __init__(self, *args, owner=None, **kwargs):

        super().__init__(*args, **kwargs)

        self.fields["shop"].queryset = Shop.objects.filter(
            owner=owner
        )

    def clean(self):
        cleaned_data = super().clean()
        for price_field, unit_field in (
            ("bulk_price", "bulk_unit"),
            ("b2b_bulk_price", "b2b_bulk_unit"),
        ):
            if cleaned_data.get(price_field) is not None and not cleaned_data.get(unit_field):
                self.add_error(unit_field, "Enter a unit when setting a bulk price.")
        return cleaned_data

class ClientDeliveryAddressForm(forms.ModelForm):

    class Meta:
        model = ClientDeliveryAddress

        fields = [
            "label",
            "address",
            "city",
            "state_code",
            "gstin",
            "pincode",
            "latitude",
            "longitude",
            "is_active",
            "is_default",
        ]

        widgets = {

            "label": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Mumbai - 400001",
                }
            ),

            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Delivery address",
                }
            ),

            "city": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),
            "state_code": forms.Select(attrs={"class": "form-select"}),
            "gstin": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Optional buyer GSTIN",
                "maxlength": "15",
            }),

            "pincode": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "readonly": "readonly",
                }
            ),

            "latitude": forms.HiddenInput(),

            "longitude": forms.HiddenInput(),

            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),

            "is_default": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
            raise ValidationError("Enter a valid 15-character GSTIN.")
        state_code = self.cleaned_data.get("state_code")
        if gstin and state_code and gstin[:2] != state_code:
            raise ValidationError("The GSTIN state prefix must match the selected state.")
        return gstin
