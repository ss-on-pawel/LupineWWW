from django import forms
from django.contrib.auth import get_user_model

from locations.models import Location

from .models import UserProfile

User = get_user_model()


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, label="Hasło")
    role = forms.ChoiceField(
        choices=UserProfile.Role.choices,
        label="Rola",
        initial=UserProfile.Role.USER,
    )
    allowed_locations = forms.ModelMultipleChoiceField(
        queryset=Location.objects.filter(parent__isnull=True).order_by("name", "id"),
        required=False,
        label="Dopuszczone lokalizacje (korzenie dostępu)",
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
            profile = user.profile
            role = self.cleaned_data["role"]
            profile.role = role
            profile.can_approve_asset_changes = role in (
                UserProfile.Role.ADMIN,
                UserProfile.Role.MANAGER,
            )
            profile.save()
            profile.allowed_locations.set(self.cleaned_data["allowed_locations"])
        return user


class UserEditForm(forms.ModelForm):
    role = forms.ChoiceField(
        choices=UserProfile.Role.choices,
        label="Rola",
    )
    allowed_locations = forms.ModelMultipleChoiceField(
        queryset=Location.objects.filter(parent__isnull=True).order_by("name", "id"),
        required=False,
        label="Dopuszczone lokalizacje (korzenie dostępu)",
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            try:
                profile = self.instance.profile
                self.initial["role"] = profile.role
                self.initial["allowed_locations"] = list(
                    profile.allowed_locations.values_list("pk", flat=True)
                )
            except UserProfile.DoesNotExist:
                pass

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile = user.profile
            role = self.cleaned_data["role"]
            profile.role = role
            profile.can_approve_asset_changes = role in (
                UserProfile.Role.ADMIN,
                UserProfile.Role.MANAGER,
            )
            profile.save()
            profile.allowed_locations.set(self.cleaned_data["allowed_locations"])
        return user
