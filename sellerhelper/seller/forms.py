
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db.models import Q

from .models import (
    AdminProductCatalog,
    MarketplaceLayoutSettings,
    SiteBrandingSettings,
    Order,
    Product,
    ProductCategory,
    SharedRiderGroup,
    Shop,
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

    class Meta:
        model = Shop

        fields = [
            "name",
            "storefront_slug",
            "market_category",
            "shop_category",
            "alias_name",
            "logo_url",
            "cover_image_url",
            "contact_no",
            "email_no",
            "upi_id",
            "gstin",
            "delivery_fee",
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
        required=True,
    )

    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
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

        if self.instance.pk:

            self.fields["username"].initial = (
                self.instance.user.username
            )

            self.fields[
                "username"
            ].disabled = True

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

            if User.objects.filter(username=username).exists():
                user = User.objects.get(username=username)
            else:
                user = User.objects.create_user(
                    username=username,
                    password=password or "shopstaff@123",
                )

            if password:
                user.set_password(password)
                user.save()

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

    stock = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0"
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

class StorefrontProductEditForm(forms.ModelForm):
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
    stock = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "1",
        }),
        label="Available stock",
    )
    b2b_stock = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "0",
            "step": "1",
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
        label="Product image URL for this shop",
    )

    class Meta:
        model = Product
        fields = [
            "product_alias_name",
            "product_title",
            "product_color",
            "product_size",
            "product_description",
        ]
        widgets = {
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
            self.fields["shop_image_url"].initial = (
                product_shop.image_url_override or product_shop.display_image_url
            )
        else:
            self.fields.pop("shop_image_url")
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
            self.product_shop.image_url_override = self.cleaned_data["shop_image_url"]
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
    class Meta:
        model = ProductCategory
        fields = ("name", "sort_order")
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


class SiteBrandingSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteBrandingSettings
        fields = (
            "site_name",
            "tagline",
            "support_email",
            "support_phone",
            "support_address",
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
            "logo": forms.FileInput(attrs={"class": "form-control"}),
            "app_icon": forms.FileInput(attrs={"class": "form-control"}),
            "favicon": forms.FileInput(attrs={"class": "form-control"}),
        }


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
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = AdminProductCatalog
        fields = (
            "product_category",
            "product_name",
            "image_url",
            "hsn_code",
            "gst_rate",
            "is_active",
        )
        widgets = {
            "product_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Product name",
                "maxlength": 200,
            }),
            "image_url": forms.URLInput(attrs={
                "class": "form-control",
                "placeholder": "https://...",
            }),
            "hsn_code": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": 8,
                "placeholder": "4, 6, or 8 digits",
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
        if hsn_code and not re.fullmatch(r"\d{4}(?:\d{2}){0,2}", hsn_code):
            raise ValidationError("Enter an HSN code with 4, 6, or 8 digits.")
        return hsn_code

    def clean_gst_rate(self):
        gst_rate = self.cleaned_data.get("gst_rate")
        if gst_rate is not None and not Decimal("0.00") <= gst_rate <= Decimal("100.00"):
            raise ValidationError("GST rate must be between 0 and 100 percent.")
        return gst_rate


class ProductForm(forms.ModelForm):

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
        category_choices = get_product_category_choices()
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

    stock = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "0"
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

    def clean_gstin(self):
        gstin = (self.cleaned_data.get("gstin") or "").strip().upper()
        if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
            raise ValidationError("Enter a valid 15-character GSTIN.")
        state_code = self.cleaned_data.get("state_code")
        if gstin and state_code and gstin[:2] != state_code:
            raise ValidationError("The GSTIN state prefix must match the selected state.")
        return gstin
