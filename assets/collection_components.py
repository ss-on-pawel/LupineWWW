"""Identifiers for new physical set components; callers lock the parent first."""
import re

from django import forms
from django.core.exceptions import ValidationError

from .forms import AssetForm
from .models import Asset, AssetCollectionEvent, ScanCode
from locations.models import Location


def next_component_code(parent):
    prefix = parent.barcode + '/'
    limit = Asset._meta.get_field('barcode').max_length
    maximum = 0
    # Include archived/detached assets and historical assignments, so released
    # suffixes are not silently reused after a component is renamed or removed.
    sources = [
        Asset.objects.filter(barcode__startswith=prefix).values_list('barcode', flat=True),
        Asset.objects.filter(inventory_number__startswith=prefix).values_list('inventory_number', flat=True),
        Location.objects.filter(code__startswith=prefix).values_list('code', flat=True),
        ScanCode.objects.filter(code__startswith=prefix).values_list('code', flat=True),
        AssetCollectionEvent.objects.filter(payload__component_parent_id=parent.pk).values_list('payload__component_barcode', flat=True),
    ]
    for source in sources:
        for code in source.iterator():
            suffix = code[len(prefix):] if isinstance(code, str) and code.startswith(prefix) else ''
            if re.fullmatch(r'[0-9]+', suffix):
                maximum = max(maximum, int(suffix))
    candidate = f'{prefix}{maximum + 1}'
    if not parent.barcode or len(candidate) > limit:
        raise ValidationError(f'Kod środka nadrzędnego jest za długi na końcówkę składnika (limit {limit} znaków).')
    return candidate


def component_form(request, locations, parent, strict_set):
    initial = {}
    if strict_set and parent:
        initial = dict(inventory_number=parent.inventory_number, current_quantity=1,
            purchase_value=None, barcode='', location_fk=parent.location_fk_id)
        try:
            initial['barcode'] = next_component_code(parent)
        except ValidationError:
            pass  # Present an actionable form error on both GET and POST.
    form = AssetForm(request.POST if request.method == 'POST' else None,
        location_queryset=locations, initial=initial)
    for name in ['is_active', 'is_catalogued', 'last_inventory_date']:
        form.fields[name].widget = forms.HiddenInput()
    if strict_set:
        for name in ['inventory_number', 'barcode', 'current_quantity', 'purchase_value']:
            form.fields[name].disabled = True
        form.fields['barcode'].help_text = 'Kod rodzica z kolejną końcówką /1, /2 itd. Ostateczny kod zostanie nadany przy zapisie.'
        form.fields['inventory_number'].help_text = 'Numer dziedziczony ze środka nadrzędnego.'
        form.fields['purchase_value'].help_text = 'Wartość zestawu jest prowadzona na środku nadrzędnym.'
        if not parent or not initial.get('barcode'):
            # Present the identifier error on the unbound GET as well.
            form.full_clean()
            if not hasattr(form, 'cleaned_data'):
                form.cleaned_data = {}
            form.add_error(None, 'Brak aktywnego rodzica lub jego kod jest za długi na końcówkę składnika.')
    return form
