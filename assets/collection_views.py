"""Scoped set/group forms with explicit atomic operations on complete sets."""
import uuid
import hashlib
import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from config.features import feature_enabled, require_feature

from accounts.utils import get_accessible_location_ids
from .collections import create_collection, change_collection_members, effective_inventory_numbers
from .models import Asset, AssetCollection, AssetCollectionMembership, AssetCollectionEvent
from .forms import AssetForm


def scoped_assets(user):
    queryset = Asset.objects.all()
    allowed = get_accessible_location_ids(user)
    return queryset if allowed is None else queryset.filter(location_fk_id__in=allowed)


def collection_context(asset, user, request=None):
    membership = AssetCollectionMembership.objects.filter(asset=asset, left_at__isnull=True).select_related("collection").first()
    if membership is None:
        return {"membership": None, "effective_number": asset.inventory_number}
    visible_query = scoped_assets(user)
    if request is not None and not feature_enabled(request, 'asset_archive'):
        visible_query = visible_query.filter(is_active=True)
    visible = visible_query.values("pk")
    members = membership.collection.memberships.filter(left_at__isnull=True)
    visible_members = members.filter(asset_id__in=visible).select_related("asset").order_by("role", "asset__name", "asset_id")
    parent = visible_members.filter(role="parent").first() if membership.collection.kind == "set" else None
    # Do not reveal the number or name of a parent outside the user's scope.
    number = parent.asset.inventory_number if parent else asset.inventory_number
    return {
        "membership": membership,
        "members": list(visible_members[:20]),
        "visible_count": visible_members.count(),
        "can_manage": not members.exclude(asset_id__in=visible).exists(),
        "effective_number": (number if parent else "—") if membership.collection.kind == "set" else asset.inventory_number,
        "parent": parent,
        "is_strict_set": membership.collection.kind == 'set',
        "is_set_component": membership.collection.kind == 'set' and membership.role == 'member',
    }


def _choices(user, query):
    assets = scoped_assets(user).filter(is_active=True).exclude(
        pk__in=AssetCollectionMembership.objects.filter(left_at__isnull=True).values("asset_id"))
    if query:
        assets = assets.filter(Q(name__icontains=query) | Q(barcode__icontains=query) | Q(inventory_number__icontains=query))
    return assets.order_by("name", "pk")


def decorate_collection_rows(rows, user):
    """Add display links without hiding/reordering results or leaking parents."""
    ids = [row['id'] for row in rows]
    links = list(AssetCollectionMembership.objects.filter(asset_id__in=ids, left_at__isnull=True).select_related('collection'))
    set_ids = {link.collection_id for link in links if link.collection.kind == 'set'}
    visible = scoped_assets(user).values('pk')
    parents = {link.collection_id: link.asset for link in AssetCollectionMembership.objects.filter(
        collection_id__in=set_ids, role='parent', left_at__isnull=True, asset_id__in=visible).select_related('asset')}
    from django.db.models import Count
    counts = dict(AssetCollectionMembership.objects.filter(collection_id__in=set_ids,
        role='member', left_at__isnull=True, asset_id__in=visible).values('collection_id').annotate(n=Count('pk')).values_list('collection_id', 'n'))
    by_asset = {link.asset_id: link for link in links}
    for row in rows:
        link = by_asset.get(row['id'])
        if link is None:
            continue
        parent = parents.get(link.collection_id)
        row['collection'] = dict(id=link.collection_id, kind=link.collection.kind, role=link.role,
            name=link.collection.name if link.collection.kind == 'group' else (parent.name if parent else 'Zestaw'),
            child_count=counts.get(link.collection_id, 0))
        if link.collection.kind == 'set':
            row['own_inventory_number'] = row['inventory_number']
            row['inventory_number'] = parent.inventory_number if parent else '—'


def hierarchical_queryset(queryset, request):
    from copy import copy
    from .views import build_asset_list_queryset
    scope = request.GET.get('asset_scope', 'active')
    accessible = scoped_assets(request.user).filter(is_active=scope != 'archive')
    parents = AssetCollectionMembership.objects.filter(role='parent', left_at__isnull=True,
        collection__kind='set', asset_id__in=accessible.values('pk'))
    children = AssetCollectionMembership.objects.filter(role='member', left_at__isnull=True,
        collection_id__in=parents.values('collection_id'))
    matched_children = children.filter(asset_id__in=queryset.values('pk'))
    context_parents = parents.filter(collection_id__in=matched_children.values('collection_id')).values('asset_id')
    root_ids = queryset.exclude(pk__in=children.values('asset_id')).values('pk')
    clone = copy(request)
    clone.GET = request.GET.copy()
    clone.GET.clear()
    clone.GET['asset_scope'] = scope
    clone.GET['ordering'] = request.GET.get('ordering', '-updated_at')
    unfiltered, _ = build_asset_list_queryset(clone)
    return unfiltered.filter(Q(pk__in=root_ids) | Q(pk__in=context_parents))


def child_queryset(queryset, request, parent_id):
    parent = get_object_or_404(AssetCollectionMembership, asset_id=parent_id, role='parent',
        left_at__isnull=True, collection__kind='set', asset_id__in=scoped_assets(request.user).values('pk'))
    children = parent.collection.memberships.filter(role='member', left_at__isnull=True).values('asset_id')
    return queryset.filter(pk__in=children)


def label_assets(assets, user):
    from copy import copy
    rows = [dict(id=asset.pk, inventory_number=asset.inventory_number) for asset in assets]
    decorate_collection_rows(rows, user)
    numbers = {row['id']: row['inventory_number'] for row in rows}
    result = []
    for asset in assets:
        label = copy(asset)
        label.inventory_number = numbers[asset.pk]
        result.append(label)
    return result


class GroupForm(forms.Form):
    name = forms.CharField(label="Nazwa grupy", max_length=255)
    request_id = forms.UUIDField(widget=forms.HiddenInput)


class ChangeMembersForm(forms.Form):
    action = forms.ChoiceField(choices=[("attach", "Dodaj"), ("detach", "Odłącz")], widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    request_id = forms.UUIDField(widget=forms.HiddenInput)


def _selected(request):
    raw = request.POST.getlist("asset_ids")
    if not raw or len(raw) > 200:
        raise ValidationError("Wybierz od 1 do 200 środków.")
    try:
        return [int(value) for value in raw]
    except (TypeError, ValueError) as exc:
        raise ValidationError("Nieprawidłowe identyfikatory.") from exc


def _form_signature(request):
    data = {key: request.POST.getlist(key) for key in sorted(request.POST) if key not in {'csrfmiddlewaretoken', 'request_id'}}
    data['_path'] = request.path
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _replay_form(request, key):
    event = AssetCollectionEvent.objects.filter(request_id=key).select_related('collection').first()
    if event is None:
        return None
    if event.operator_id != request.user.pk or event.payload.get('form_signature') != _form_signature(request):
        raise ValidationError('Identyfikator operacji został użyty dla innych danych.')
    ids = {row['asset_id'] for row in event.before + event.after}
    if scoped_assets(request.user).filter(pk__in=ids).count() != len(ids):
        raise PermissionDenied
    return event.collection


class SetForm(forms.Form):
    parent_mode = forms.ChoiceField(label='Środek nadrzędny', choices=[('existing', 'Wybrana kartoteka jest środkiem nadrzędnym'), ('new', 'Utwórz nową kartotekę nadrzędną')])
    request_id = forms.UUIDField(widget=forms.HiddenInput)


@login_required
@require_http_methods(['GET', 'POST'])
def set_create(request, asset_id):
    anchor = get_object_or_404(scoped_assets(request.user), pk=asset_id, is_active=True)
    form = SetForm(request.POST or None, initial={'parent_mode': 'existing', 'request_id': uuid.uuid4()})
    allowed_locations = get_accessible_location_ids(request.user)
    from locations.models import Location
    locations = Location.objects.filter(is_active=True)
    if allowed_locations is not None:
        locations = locations.filter(pk__in=allowed_locations)
    parent_form = AssetForm(request.POST if request.method == 'POST' else None, prefix='parent',
        location_queryset=locations, initial={'name': f'Zestaw: {anchor.name}'[:255],
            'asset_type': anchor.asset_type, 'location_fk': anchor.location_fk_id})
    for field in ['is_active', 'is_catalogued', 'last_inventory_date']:
        parent_form.fields[field].widget = forms.HiddenInput()
    if request.method == 'POST' and form.is_valid():
        try:
            replay = _replay_form(request, form.cleaned_data['request_id'])
            if replay:
                return redirect('assets:collection-detail', pk=replay.pk)
            mode = form.cleaned_data['parent_mode']
            ids = [int(value) for value in request.POST.getlist('asset_ids')]
            if mode == 'new' and not parent_form.is_valid():
                raise ValidationError('Uzupełnij poprawnie dane nowej kartoteki nadrzędnej.')
            with transaction.atomic():
                from .collections import _assets
                _assets(request.user, sorted(set(ids + [anchor.pk])))
                parent = parent_form.save() if mode == 'new' else anchor
                if mode == 'new':
                    from .services import record_asset_history
                    record_asset_history(asset=parent, operator=request.user, event_type='created', description='Utworzono środek nadrzędny zestawu')
                collection = create_collection(operator=request.user, kind='set', name=parent.name,
                    parent_id=parent.pk, asset_ids=sorted(set(ids + [anchor.pk, parent.pk])),
                    request_id=form.cleaned_data['request_id'])
                event = AssetCollectionEvent.objects.get(request_id=form.cleaned_data['request_id'])
                event.payload['form_signature'] = _form_signature(request)
                event.save(update_fields=['payload'])
        except (ValidationError, ValueError) as error:
            form.add_error(None, '; '.join(error.messages) if isinstance(error, ValidationError) else 'Nieprawidłowe identyfikatory.')
        except IntegrityError:
            form.add_error(None, 'Powiązanie lub kod zmienił się w innym oknie. Odśwież formularz.')
        else:
            messages.success(request, 'Utworzono zestaw ze środkiem nadrzędnym.')
            return redirect('assets:collection-detail', pk=collection.pk)
    query = request.GET.get('q', '').strip()[:255]
    candidates = Paginator(_choices(request.user, query).exclude(pk=anchor.pk), 30).get_page(request.GET.get('page'))
    return render(request, 'assets/set_form.html', dict(page_title='Utwórz zestaw', anchor=anchor,
        form=form, parent_form=parent_form, candidates=candidates, q=query,
        selected_ids=request.POST.getlist('asset_ids')))


@login_required
@require_http_methods(['GET', 'POST'])
def collection_member_create(request, pk):
    collection = get_object_or_404(AssetCollection, pk=pk)
    memberships = collection.memberships.filter(left_at__isnull=True)
    visible = visible_request_assets(request).values('pk')
    if not memberships.filter(asset_id__in=visible).exists() or memberships.exclude(asset_id__in=visible).exists():
        raise PermissionDenied
    member_form = ChangeMembersForm(request.POST or None, initial={'action': 'attach',
        'expected_version': collection.version, 'request_id': uuid.uuid4()})
    allowed = get_accessible_location_ids(request.user)
    from locations.models import Location
    locations = Location.objects.filter(is_active=True)
    if allowed is not None:
        locations = locations.filter(pk__in=allowed)
    form = AssetForm(request.POST if request.method == 'POST' else None, location_queryset=locations,
        initial={'location_fk': memberships.first().asset.location_fk_id})
    for field in ['is_active', 'is_catalogued', 'last_inventory_date']:
        form.fields[field].widget = forms.HiddenInput()
    if request.method == 'POST' and member_form.is_valid():
        try:
            replay = _replay_form(request, member_form.cleaned_data['request_id'])
            if replay:
                return redirect('assets:collection-detail', pk=replay.pk)
            if not form.is_valid():
                raise ValidationError('Uzupełnij poprawnie dane nowego elementu.')
            if member_form.cleaned_data['action'] != 'attach':
                raise ValidationError('Nieprawidłowa operacja.')
            with transaction.atomic():
                # Match collection -> asset ordering before writing a new asset.
                AssetCollection.objects.select_for_update().get(pk=collection.pk)
                from .collections import _assets
                _assets(request.user, sorted(memberships.values_list('asset_id', flat=True)))
                asset = form.save()
                from .services import record_asset_history
                record_asset_history(asset=asset, operator=request.user, event_type='created', description='Utworzono element składu')
                change_collection_members(operator=request.user, collection_id=collection.pk,
                    asset_ids=[asset.pk], **member_form.cleaned_data)
                event = AssetCollectionEvent.objects.get(request_id=member_form.cleaned_data['request_id'])
                event.payload['form_signature'] = _form_signature(request)
                event.save(update_fields=['payload'])
        except ValidationError as error:
            form.add_error(None, '; '.join(error.messages))
        except IntegrityError:
            form.add_error(None, 'Kod lub skład zmienił się. Odśwież formularz.')
        else:
            messages.success(request, 'Utworzono i dołączono nowy element.')
            return redirect('assets:collection-detail', pk=collection.pk)
    return render(request, 'assets/collection_member_form.html', dict(page_title='Utwórz element',
        collection=collection, form=form, member_form=member_form))


@login_required
@require_http_methods(["GET", "POST"])
def group_create(request, asset_id):
    anchor = get_object_or_404(scoped_assets(request.user), pk=asset_id, is_active=True)
    query = request.GET.get("q", "").strip()[:255]
    form = GroupForm(request.POST or None, initial={"name": f"Grupa: {anchor.name}"[:255], "request_id": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        try:
            if request.POST.get("kind", "group") != "group":
                raise ValidationError("Ten formularz tworzy grupę równorzędnych środków.")
            ids = sorted(set([anchor.pk] + [int(value) for value in request.POST.getlist("asset_ids")]))
            collection = create_collection(operator=request.user, kind="group", name=form.cleaned_data["name"],
                asset_ids=ids, request_id=form.cleaned_data["request_id"])
        except (ValidationError, ValueError) as error:
            form.add_error(None, "; ".join(error.messages) if isinstance(error, ValidationError) else "Nieprawidłowe identyfikatory.")
        except IntegrityError:
            form.add_error(None, "Inny użytkownik zmienił powiązanie. Odśwież formularz.")
        else:
            messages.success(request, "Utworzono grupę środków.")
            return redirect("assets:collection-detail", pk=collection.pk)
    page = Paginator(_choices(request.user, query).exclude(pk=anchor.pk), 30).get_page(request.GET.get("page"))
    return render(request, "assets/collection_form.html", {"page_title": "Utwórz grupę środków", "form": form,
        "anchor": anchor, "candidates": page, "q": query, "selected_ids": request.POST.getlist("asset_ids")})


@login_required
@require_http_methods(["GET", "POST"])
def collection_detail(request, pk):
    collection = get_object_or_404(AssetCollection, pk=pk)
    visible_assets = visible_request_assets(request).values("pk")
    all_members = collection.memberships.filter(left_at__isnull=True)
    members = all_members.filter(asset_id__in=visible_assets).select_related("asset").order_by("role", "asset__name", "asset_id")
    if not members.exists():
        # Empty groups are still accessible to their creator; no previous members shown.
        if all_members.exists() or collection.created_by_id != request.user.pk:
            raise Http404
    can_manage = not all_members.exclude(asset_id__in=visible_assets).exists() and not all_members.filter(asset__is_active=False).exists()
    whole_scope = not all_members.exclude(asset_id__in=visible_assets).exists()
    has_members = all_members.exists()
    active_set = collection.kind == 'set' and whole_scope and can_manage and has_members
    archived_set = collection.kind == 'set' and whole_scope and has_members and not all_members.filter(asset__is_active=True).exists()
    if collection.kind == 'set' and not has_members:
        can_manage = False
    form = ChangeMembersForm(request.POST or None, initial={"request_id": uuid.uuid4(), "expected_version": collection.version})
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if form.is_valid():
            try:
                collection = change_collection_members(operator=request.user, collection_id=collection.pk,
                    asset_ids=_selected(request), **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, "; ".join(error.messages))
            except IntegrityError:
                form.add_error(None, "Inny użytkownik zmienił powiązanie. Odśwież formularz.")
            else:
                messages.success(request, "Zaktualizowano skład grupy.")
                return redirect("assets:collection-detail", pk=collection.pk)
    query = request.GET.get("q", "").strip()[:255]
    page = Paginator(members, 50).get_page(request.GET.get("members_page"))
    candidates = Paginator(_choices(request.user, query), 30).get_page(request.GET.get("page")) if can_manage else None
    visible_parent = members.filter(role='parent').first()
    for member in page:
        member.display_inventory_number = (visible_parent.asset.inventory_number if visible_parent else '—') if collection.kind == 'set' else member.asset.inventory_number
    label_ids = ','.join(str(member.asset_id) for member in page if member.asset.is_active and (collection.kind != 'set' or member.role == 'member'))
    return render(request, "assets/collection_detail.html", {"page_title": collection.get_kind_display(),
        "collection": collection, "members": page, "can_manage": can_manage, "form": form,
        "candidates": candidates, "q": query,
        "can_withdraw_set": active_set and feature_enabled(request, 'asset_withdraw'),
        "can_move_set": active_set,
        "can_restore_set": archived_set and feature_enabled(request, 'asset_archive'),
        "label_ids": label_ids,
        "display_name": collection.name if collection.kind == 'group' else (visible_parent.asset.name if visible_parent else 'Karta nadrzędna poza zakresem')})


def visible_request_assets(request):
    assets = scoped_assets(request.user)
    return assets if feature_enabled(request, 'asset_archive') else assets.filter(is_active=True)


class SetStateForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


@login_required
@require_http_methods(['GET', 'POST'])
def set_operation(request, pk, action):
    if action not in {'move', 'withdraw', 'restore', 'dissolve'}:
        raise Http404
    require_feature(request, {'move': 'assets_core', 'withdraw': 'asset_withdraw', 'restore': 'asset_archive', 'dissolve': 'assets_core'}[action])
    collection = get_object_or_404(AssetCollection, pk=pk, kind='set')
    memberships = collection.memberships.filter(left_at__isnull=True).select_related('asset').order_by('role', 'asset__name')
    visible = visible_request_assets(request).values('pk')
    if not memberships.exists() or memberships.exclude(asset_id__in=visible).exists():
        raise PermissionDenied
    state_form = SetStateForm(request.POST or None, initial={'request_id': uuid.uuid4(), 'expected_version': collection.version})
    operation_form = None
    if action == 'move':
        from locations.models import Location
        targets = Location.objects.filter(is_active=True)
        allowed = get_accessible_location_ids(request.user)
        if allowed is not None:
            targets = targets.filter(pk__in=allowed)
        class MoveForm(forms.Form):
            target_location = forms.ModelChoiceField(label='Lokalizacja docelowa', queryset=targets)
        operation_form = MoveForm(request.POST or None)
    elif action == 'withdraw':
        from .forms import AssetWithdrawalForm
        from django.utils import timezone
        data = request.POST.copy() if request.method == 'POST' else None
        if data is not None:
            data['expected_quantity'] = '0'
        operation_form = AssetWithdrawalForm(data, initial={'action_date': timezone.localdate(), 'expected_quantity': 0})
        # The whole set uses the state form's UUID, with per-asset UUID5 children.
        operation_form.fields.pop('request_id')
    if request.method == 'POST' and state_form.is_valid() and (operation_form is None or operation_form.is_valid()):
        if action == 'move':
            data = dict(target_location_id=operation_form.cleaned_data['target_location'].pk)
        elif action == 'withdraw':
            values = operation_form.cleaned_data
            data = {key: values[key] for key in ['method', 'notes', 'document_number']}
            data['action_date'] = values['action_date'].isoformat()
        else:
            data = {}
        try:
            from .collections import operate_set
            operate_set(operator=request.user, collection_id=pk, action=action, data=data, **state_form.cleaned_data)
        except (ValidationError, IntegrityError) as error:
            state_form.add_error(None, '; '.join(error.messages) if isinstance(error, ValidationError) else 'Stan zestawu zmienił się. Odśwież formularz.')
        else:
            messages.success(request, 'Zapisano operację całego zestawu.')
            if action == 'dissolve' or (action == 'withdraw' and not feature_enabled(request, 'asset_archive')):
                return redirect('assets:list')
            return redirect('assets:collection-detail', pk=pk)
    return render(request, 'assets/set_operation.html', dict(collection=collection, action=action,
        state_form=state_form, operation_form=operation_form,
        members=Paginator(memberships, 50).get_page(request.GET.get('page')), total=memberships.count(),
        title={'move': 'Przenieś cały zestaw', 'withdraw': 'Wycofaj cały zestaw', 'restore': 'Przywróć cały zestaw', 'dissolve': 'Rozwiąż zestaw'}[action]))
