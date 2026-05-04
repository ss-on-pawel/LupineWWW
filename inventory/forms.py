from django import forms

from assets.models import Asset


DEFAULT_ASSET_TYPES = [Asset.AssetType.FIXED, Asset.AssetType.LOW_VALUE]

START_ASSET_TYPE_CHOICES = [
    (Asset.AssetType.FIXED, "Środek trwały"),
    (Asset.AssetType.LOW_VALUE, "Niskocenny"),
    (Asset.AssetType.INTANGIBLE, "WNiP"),
    (Asset.AssetType.QUANTITY, "Ilościowy"),
    (Asset.AssetType.OTHER, "Inny"),
]


class InventorySessionStartForm(forms.Form):
    root_locations = forms.ModelMultipleChoiceField(
        queryset=None,
        widget=forms.CheckboxSelectMultiple,
        label="Lokalizacje root",
        required=True,
        error_messages={"required": "Wybierz co najmniej jedną lokalizację."},
    )
    asset_types = forms.MultipleChoiceField(
        choices=START_ASSET_TYPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label="Rodzaj",
        required=True,
        initial=DEFAULT_ASSET_TYPES,
        error_messages={"required": "Wybierz co najmniej jeden rodzaj."},
    )

    def __init__(self, *args, available_locations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["root_locations"].queryset = available_locations


class SimpleInventorySessionStartForm(forms.Form):
    def __init__(self, *args, root_locations, **kwargs):
        super().__init__(*args, **kwargs)
        self.root_locations = list(root_locations)

    def clean(self):
        cleaned_data = super().clean()
        if not self.root_locations:
            raise forms.ValidationError("Nie masz przypisanych lokalizacji do inwentaryzacji.")
        return cleaned_data
