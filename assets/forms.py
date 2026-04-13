from django import forms

from .models import Asset


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = [
            "name",
            "inventory_number",
            "asset_type",
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
        }
        help_texts = {
            "inventory_number": "Unikalny numer ewidencyjny składnika majątku.",
            "barcode": "Pole opcjonalne. Jeśli zostanie podane, musi być unikalne.",
            "purchase_value": "Kwota brutto lub netto zgodnie z przyjętą polityką ewidencji.",
        }

    def clean_barcode(self):
        barcode = (self.cleaned_data.get("barcode") or "").strip()
        return barcode
