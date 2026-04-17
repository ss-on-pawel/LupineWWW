from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


FILTER_QUERY_PREFIX = "filter__"

TYPE_OPERATORS = {
    "text": ("contains", "equals"),
    "enum": ("equals", "in"),
    "number": ("eq", "gt", "lt", "between"),
    "date": ("before", "after", "between"),
}

OPERATOR_LOOKUPS = {
    "text": {
        "contains": "icontains",
        "equals": "iexact",
    },
    "enum": {
        "equals": "exact",
        "in": "in",
    },
    "number": {
        "eq": "exact",
        "gt": "gt",
        "lt": "lt",
        "between": "range",
    },
    "date": {
        "before": "lt",
        "after": "gt",
        "between": "range",
    },
}


@dataclass(frozen=True)
class FilterFieldSpec:
    field: str
    label: str
    type: str
    lookup_field: str
    operators: tuple[str, ...]
    choices: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FilterCondition:
    field: str
    operator: str
    value: Any
    lookup: str


@dataclass(frozen=True)
class ParsedFilters:
    conditions: tuple[FilterCondition, ...]
    errors: tuple[dict[str, str], ...]


ASSET_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "name": FilterFieldSpec("name", "Nazwa", "text", "name", TYPE_OPERATORS["text"]),
    "inventory_number": FilterFieldSpec("inventory_number", "Nr inwentarzowy", "text", "inventory_number", TYPE_OPERATORS["text"]),
    "location": FilterFieldSpec("location", "Lokalizacja", "text", "location", TYPE_OPERATORS["text"]),
    "category": FilterFieldSpec("category", "Kategoria", "text", "category", TYPE_OPERATORS["text"]),
    "manufacturer": FilterFieldSpec("manufacturer", "Producent", "text", "manufacturer", TYPE_OPERATORS["text"]),
    "model": FilterFieldSpec("model", "Model", "text", "model", TYPE_OPERATORS["text"]),
    "serial_number": FilterFieldSpec("serial_number", "Nr seryjny", "text", "serial_number", TYPE_OPERATORS["text"]),
    "barcode": FilterFieldSpec("barcode", "Kod kreskowy", "text", "barcode", TYPE_OPERATORS["text"]),
    "department": FilterFieldSpec("department", "Dział", "text", "department", TYPE_OPERATORS["text"]),
    "organizational_unit": FilterFieldSpec("organizational_unit", "Jednostka organizacyjna", "text", "organizational_unit", TYPE_OPERATORS["text"]),
    "room": FilterFieldSpec("room", "Pomieszczenie", "text", "room", TYPE_OPERATORS["text"]),
    "invoice_number": FilterFieldSpec("invoice_number", "Nr faktury", "text", "invoice_number", TYPE_OPERATORS["text"]),
    "external_id": FilterFieldSpec("external_id", "ID zewnętrzne", "text", "external_id", TYPE_OPERATORS["text"]),
    "cost_center": FilterFieldSpec("cost_center", "MPK / koszt", "text", "cost_center", TYPE_OPERATORS["text"]),
    "status": FilterFieldSpec(
        "status",
        "Status",
        "enum",
        "status",
        TYPE_OPERATORS["enum"],
        (
            ("in_stock", "Na stanie"),
            ("in_use", "W użyciu"),
            ("reserved", "Zarezerwowany"),
            ("in_service", "W serwisie"),
            ("liquidated", "Zlikwidowany"),
            ("sold", "Sprzedany"),
            ("lost", "Utracony"),
        ),
    ),
    "asset_type": FilterFieldSpec(
        "asset_type",
        "Typ",
        "enum",
        "asset_type",
        TYPE_OPERATORS["enum"],
        (
            ("fixed_asset", "Środek trwały"),
            ("low_value_asset", "Wyposażenie"),
            ("it_equipment", "Sprzęt IT"),
            ("software", "Oprogramowanie"),
            ("vehicle", "Pojazd"),
            ("other", "Inny składnik"),
        ),
    ),
    "technical_condition": FilterFieldSpec(
        "technical_condition",
        "Stan techniczny",
        "enum",
        "technical_condition",
        TYPE_OPERATORS["enum"],
        (
            ("new", "Nowy"),
            ("very_good", "Bardzo dobry"),
            ("good", "Dobry"),
            ("average", "Średni"),
            ("poor", "Słaby"),
            ("damaged", "Uszkodzony"),
        ),
    ),
    "is_active": FilterFieldSpec(
        "is_active",
        "Aktywny",
        "enum",
        "is_active",
        TYPE_OPERATORS["enum"],
        (
            ("true", "Tak"),
            ("false", "Nie"),
        ),
    ),
    "purchase_value": FilterFieldSpec("purchase_value", "Wartość", "number", "purchase_value", TYPE_OPERATORS["number"]),
    "purchase_date": FilterFieldSpec("purchase_date", "Data zakupu", "date", "purchase_date", TYPE_OPERATORS["date"]),
    "commissioning_date": FilterFieldSpec("commissioning_date", "Przyjęcie do użycia", "date", "commissioning_date", TYPE_OPERATORS["date"]),
    "last_inventory_date": FilterFieldSpec("last_inventory_date", "Ost. inwentaryzacja", "date", "last_inventory_date", TYPE_OPERATORS["date"]),
    "next_review_date": FilterFieldSpec("next_review_date", "Nast. przegląd", "date", "next_review_date", TYPE_OPERATORS["date"]),
    "warranty_until": FilterFieldSpec("warranty_until", "Gwarancja do", "date", "warranty_until", TYPE_OPERATORS["date"]),
    "insurance_until": FilterFieldSpec("insurance_until", "Ubezpieczenie do", "date", "insurance_until", TYPE_OPERATORS["date"]),
    "updated_at": FilterFieldSpec("updated_at", "Data modyfikacji", "date", "updated_at__date", TYPE_OPERATORS["date"]),
}


def get_asset_filter_ui_schema() -> list[dict[str, Any]]:
    schema = []
    for spec in ASSET_FILTER_SPECS.values():
        schema.append(
            {
                "field": spec.field,
                "label": spec.label,
                "type": spec.type,
                "operators": list(spec.operators),
                "choices": [{"value": value, "label": label} for value, label in spec.choices],
            }
        )
    return schema


def parse_asset_filters(params) -> ParsedFilters:
    conditions: list[FilterCondition] = []
    errors: list[dict[str, str]] = []

    for key, raw_value in params.items():
        if not key.startswith(FILTER_QUERY_PREFIX):
            continue

        _, field, operator = key.split("__", 2) if key.count("__") >= 2 else ("", "", "")
        if not field or not operator:
            errors.append({"key": key, "error": "invalid_key"})
            continue

        spec = ASSET_FILTER_SPECS.get(field)
        if spec is None:
            errors.append({"key": key, "error": "unknown_field"})
            continue

        if operator not in spec.operators:
            errors.append({"key": key, "field": field, "error": "invalid_operator"})
            continue

        normalized = _normalize_filter_value(spec, operator, raw_value)
        if normalized is None:
            errors.append({"key": key, "field": field, "operator": operator, "error": "invalid_value"})
            continue

        lookup = spec.lookup_field if operator == "between" else f"{spec.lookup_field}__{OPERATOR_LOOKUPS[spec.type][operator]}"
        conditions.append(FilterCondition(field=field, operator=operator, value=normalized, lookup=lookup))
    return ParsedFilters(conditions=tuple(conditions), errors=tuple(errors))


def apply_asset_filters(queryset, parsed_filters: ParsedFilters):
    filtered = queryset
    for condition in parsed_filters.conditions:
        if condition.operator == "between":
            range_from, range_to = condition.value
            filtered = filtered.filter(
                **{
                    f"{condition.lookup}__gte": range_from,
                    f"{condition.lookup}__lte": range_to,
                }
            )
            continue
        filtered = filtered.filter(**{condition.lookup: condition.value})
    return filtered


def _normalize_filter_value(spec: FilterFieldSpec, operator: str, raw_value: str):
    if spec.type == "text":
        value = str(raw_value).strip()
        return value or None

    if spec.type == "enum":
        allowed = {choice for choice, _ in spec.choices}
        if operator == "in":
            values = [item.strip() for item in str(raw_value).split(",") if item.strip()]
            if not values or any(item not in allowed for item in values):
                return None
            if spec.field == "is_active":
                return [_parse_bool(value) for value in values]
            return values

        value = str(raw_value).strip()
        if value not in allowed:
            return None
        if spec.field == "is_active":
            return _parse_bool(value)
        return value

    if spec.type == "number":
        if operator == "between":
            return _normalize_range_values(spec, raw_value)
        try:
            return Decimal(str(raw_value).strip())
        except (InvalidOperation, ValueError):
            return None

    if spec.type == "date":
        if operator == "between":
            return _normalize_range_values(spec, raw_value)
        try:
            return date.fromisoformat(str(raw_value).strip())
        except ValueError:
            return None

    return None


def _parse_bool(raw_value: str) -> bool:
    return str(raw_value).strip().lower() == "true"


def _normalize_range_values(spec: FilterFieldSpec, raw_value: str):
    parts = [item.strip() for item in str(raw_value).split(",", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None

    if spec.type == "number":
        try:
            range_from = Decimal(parts[0])
            range_to = Decimal(parts[1])
        except (InvalidOperation, ValueError):
            return None
    elif spec.type == "date":
        try:
            range_from = date.fromisoformat(parts[0])
            range_to = date.fromisoformat(parts[1])
        except ValueError:
            return None
    else:
        return None

    if range_from > range_to:
        return None

    return (range_from, range_to)
