from django import forms

from .models import Location, OrganizationSettings


class OrganizationSettingsForm(forms.ModelForm):
    class Meta:
        model = OrganizationSettings
        fields = ["full_name", "short_name", "report_footer"]
        widgets = {
            "report_footer": forms.Textarea(attrs={"rows": 3}),
        }


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
        }

    def __init__(self, *args, parent=None, **kwargs):
        self.parent = parent
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.parent = self.parent
        if commit:
            instance.save()
        return instance
