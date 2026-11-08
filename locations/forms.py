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
        fields = ["name", "code"]
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
        }

    def __init__(self, *args, parent=None, **kwargs):
        self.parent = parent
        super().__init__(*args, **kwargs)
        self.fields['code'].required = False
        if self.is_bound and 'code' not in self.data and self.instance.pk:
            self.data = self.data.copy()
            self.data['code'] = self.instance.code

    def clean_code(self):
        from assets.scan_codes import validate_scan_code
        code = self.cleaned_data.get('code') or (self.instance.code if self.instance.pk else '')
        validate_scan_code(code, kind='location', owner_id=self.instance.pk)
        return code

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.parent = self.parent
        if commit:
            instance.save()
        return instance
