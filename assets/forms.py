from django import forms
from django.utils.text import slugify

from .models import Asset, AssetTypeDictionary


class AssetForm(forms.ModelForm):
    asset_type = forms.ChoiceField(label="Rodzaj", required=False)
    record_quantity = forms.IntegerField(
        label="Ilość ewidencyjna",
        min_value=0,
        required=False,
        initial=1,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        asset_type_choices = [
            (asset_type.code, asset_type.name)
            for asset_type in AssetTypeDictionary.objects.filter(is_active=True).order_by("sort_order", "name")
        ]
        self.fields["asset_type"].choices = [("", "---------")] + asset_type_choices
        self.fields["asset_type"].label = "Rodzaj"

        if self.instance and self.instance.pk and not self.initial.get("asset_type") and self.instance.asset_type_ref_id:
            self.initial["asset_type"] = self.instance.asset_type_ref.code

    class Meta:
        model = Asset
        fields = [
            "name",
            "inventory_number",
            "asset_type",
            "record_quantity",
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
            "location",
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
            "record_quantity": forms.NumberInput(attrs={"min": "0"}),
        }
        help_texts = {
            "inventory_number": "Unikalny numer ewidencyjny składnika majątku.",
            "barcode": "Pole opcjonalne. Jeśli zostanie podane, musi być unikalne.",
            "purchase_value": "Kwota brutto lub netto zgodnie z przyjętą polityką ewidencji.",
        }

    def clean_barcode(self):
        barcode = (self.cleaned_data.get("barcode") or "").strip()
        return barcode

    def clean_record_quantity(self):
        value = self.cleaned_data.get("record_quantity")
        return 1 if value is None else value

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.add("asset_type")
        return exclude


class AssetTypeDictionaryForm(forms.ModelForm):
    code = forms.CharField(label="Kod", max_length=64)

    class Meta:
        model = AssetTypeDictionary
        fields = [
            "name",
            "code",
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
