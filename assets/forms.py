from django import forms

from .models import Asset


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = ["name", "inventory_number", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }
