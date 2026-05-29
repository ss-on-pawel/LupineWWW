import re
from decimal import Decimal

from django import forms
from django.utils.text import slugify

from locations.models import Location

from .models import Asset, AssetDepreciationPlan, AssetTypeDictionary


SYSTEM_MANAGED_ASSET_FIELDS = frozenset(
    {
        "record_quantity",
        "is_active",
        "last_inventory_date",
    }
)


class AssetForm(forms.ModelForm):
    asset_type = forms.ChoiceField(label="Rodzaj", required=False)
    location_fk = forms.ModelChoiceField(
        label="Lokalizacja",
        queryset=Location.objects.none(),
        required=True,
    )
    current_quantity = forms.IntegerField(label="Ilość", min_value=1, initial=1)

    def __init__(self, *args, **kwargs):
        location_queryset = kwargs.pop("location_queryset", None)
        super().__init__(*args, **kwargs)
        asset_type_choices = [
            (asset_type.code, asset_type.name)
            for asset_type in AssetTypeDictionary.objects.filter(is_active=True).order_by("sort_order", "name")
        ]
        self.fields["asset_type"].choices = [("", "---------")] + asset_type_choices
        self.fields["asset_type"].label = "Rodzaj"
        self.fields["location_fk"].queryset = (
            location_queryset
            if location_queryset is not None
            else Location.objects.filter(is_active=True).order_by("name", "id")
        )

        if self.instance and self.instance.pk and not self.initial.get("asset_type") and self.instance.asset_type_ref_id:
            self.initial["asset_type"] = self.instance.asset_type_ref.code

    class Meta:
        model = Asset
        fields = [
            "name",
            "inventory_number",
            "asset_type",
            "current_quantity",
            "category",
            "manufacturer",
            "model",
            "serial_number",
            "barcode",
            "description",
            "purchase_date",
            "commissioning_date",
            "purchase_value",
            "invoice_number",
            "external_id",
            "cost_center",
            "organizational_unit",
            "department",
            "location_fk",
            "room",
            "responsible_person",
            "current_user",
            "status",
            "technical_condition",
            "last_inventory_date",
            "next_review_date",
            "warranty_until",
            "insurance_until",
            "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "purchase_date": forms.DateInput(attrs={"type": "date"}),
            "commissioning_date": forms.DateInput(attrs={"type": "date"}),
            "last_inventory_date": forms.DateInput(attrs={"type": "date"}),
            "next_review_date": forms.DateInput(attrs={"type": "date"}),
            "warranty_until": forms.DateInput(attrs={"type": "date"}),
            "insurance_until": forms.DateInput(attrs={"type": "date"}),
            "purchase_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "current_quantity": forms.NumberInput(attrs={"min": "1"}),
        }
        help_texts = {
            "inventory_number": "Numer ewidencyjny/księgowy. Może być wspólny dla wielu składników majątku.",
            "barcode": "Pole opcjonalne. Jeśli zostanie podane, musi być unikalne.",
            "purchase_value": "Kwota brutto lub netto zgodnie z przyjętą polityką ewidencji.",
        }

    def clean_barcode(self):
        barcode = (self.cleaned_data.get("barcode") or "").strip()
        if not barcode:
            return ""

        barcode_query = Asset.objects.filter(barcode=barcode)
        if self.instance and self.instance.pk:
            barcode_query = barcode_query.exclude(pk=self.instance.pk)
        if barcode_query.exists():
            raise forms.ValidationError("Składnik o tym kodzie kreskowym już istnieje.")

        if Asset.objects.filter(inventory_number=barcode).exists():
            raise forms.ValidationError("Kod kreskowy nie może być taki sam jak istniejący numer inwentarzowy.")

        if Location.objects.filter(code=barcode).exists():
            raise forms.ValidationError("Kod kreskowy nie może być taki sam jak kod lokalizacji.")

        return barcode

    def clean_current_quantity(self):
        value = self.cleaned_data.get("current_quantity")
        if value is None:
            return 1
        return value

    def clean(self):
        cleaned_data = super().clean()
        if self.instance and self.instance.pk:
            return cleaned_data

        if cleaned_data.get("barcode"):
            return cleaned_data

        asset_type_code = cleaned_data.get("asset_type") or ""
        if not asset_type_code:
            self.add_error("asset_type", "Wybierz rodzaj środka, aby wygenerować kod kreskowy.")
            return cleaned_data

        asset_type = AssetTypeDictionary.objects.filter(code=asset_type_code).first()
        if asset_type is not None and not (asset_type.barcode_prefix or "").strip():
            self.add_error(
                "asset_type",
                "Wybrany rodzaj środka nie ma skonfigurowanego prefixu kodu kreskowego.",
            )
        return cleaned_data

    def save(self, commit=True):
        asset = super().save(commit=False)
        if asset.pk:
            current_asset = Asset.objects.only(*SYSTEM_MANAGED_ASSET_FIELDS).get(pk=asset.pk)
            for field_name in SYSTEM_MANAGED_ASSET_FIELDS:
                setattr(asset, field_name, getattr(current_asset, field_name))
        else:
            asset.record_quantity = 1
            asset.is_active = True
            asset.last_inventory_date = None

        if commit:
            if not asset.pk and not asset.barcode:
                from .services import generate_unique_asset_barcode

                if asset.asset_type and not asset.asset_type_ref_id:
                    asset.asset_type_ref = AssetTypeDictionary.objects.filter(code=asset.asset_type).first()

                asset.barcode = generate_unique_asset_barcode(
                    asset_type_ref=asset.asset_type_ref,
                    asset_type=asset.asset_type,
                )
            asset.save()
            self.save_m2m()
        return asset

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.add("asset_type")
        return exclude


class DepreciationPlanForm(forms.ModelForm):
    class Meta:
        model = AssetDepreciationPlan
        fields = [
            "enabled",
            "method",
            "kst_category",
            "initial_value",
            "residual_value",
            "depreciation_start_date",
            "annual_rate_percent",
            "notes",
        ]
        widgets = {
            "initial_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "residual_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "depreciation_start_date": forms.DateInput(attrs={"type": "date"}),
            "annual_rate_percent": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        enabled = cleaned.get("enabled")
        method = cleaned.get("method")
        annual_rate = cleaned.get("annual_rate_percent")
        initial = cleaned.get("initial_value")
        residual = cleaned.get("residual_value")

        if not enabled:
            return cleaned

        if not method:
            self.add_error("method", "Wybierz metodę amortyzacji.")
            return cleaned

        if initial is None:
            self.add_error("initial_value", "Podaj wartość początkową do amortyzacji.")
        elif initial < 0:
            self.add_error("initial_value", "Wartość początkowa nie może być ujemna.")
        if residual is not None and residual < 0:
            self.add_error("residual_value", "Wartość rezydualna nie może być ujemna.")
        if initial is not None and residual is not None and residual > initial:
            self.add_error("residual_value", "Wartość rezydualna nie może przekraczać wartości początkowej.")

        if method == AssetDepreciationPlan.Method.ONE_TIME:
            cleaned["annual_rate_percent"] = Decimal("100")
        elif method == AssetDepreciationPlan.Method.LINEAR:
            if annual_rate is None:
                self.add_error("annual_rate_percent", "Podaj stawkę roczną amortyzacji.")
            elif annual_rate <= 0:
                self.add_error("annual_rate_percent", "Stawka roczna musi być większa od zera.")

        return cleaned


class AssetAttachmentForm(forms.Form):
    title = forms.CharField(label="Tytuł", max_length=255)
    file = forms.FileField(label="Plik")


class AssetTypeDictionaryForm(forms.ModelForm):
    code = forms.CharField(label="Kod", max_length=64)
    barcode_prefix = forms.CharField(
        label="Prefix kodu",
        max_length=3,
        required=False,
        help_text="2-3 znaki używane później do generowania kodów kreskowych, np. ST260000001.",
    )

    class Meta:
        model = AssetTypeDictionary
        fields = [
            "name",
            "code",
            "barcode_prefix",
            "is_quantity_based",
            "is_active",
            "sort_order",
        ]
        labels = {
            "name": "Nazwa",
            "code": "Kod",
            "is_quantity_based": "Ilościowy",
            "is_active": "Aktywny",
            "sort_order": "Kolejność",
        }

    def clean_code(self):
        code = slugify((self.cleaned_data.get("code") or "").strip())
        if not code:
            raise forms.ValidationError("Kod jest wymagany.")
        return code

    def clean_barcode_prefix(self):
        prefix = (self.cleaned_data.get("barcode_prefix") or "").strip().upper()
        if not prefix:
            return ""
        if not re.fullmatch(r"[A-Z0-9]{2,3}", prefix):
            raise forms.ValidationError("Prefix kodu musi miec 2-3 znaki i zawierac tylko wielkie litery oraz cyfry.")
        return prefix
