import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .models import Asset, AssetDepreciationPlan

_MONEY = Decimal("0.01")

_MONTHS_PL = [
    "Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
    "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień",
]


def period_end_date(year: int, month: int | None) -> date:
    if month:
        return date(year, month, calendar.monthrange(year, month)[1])
    return date(year, 12, 31)


def period_label(year: int, month: int | None) -> str:
    if month:
        return f"{_MONTHS_PL[month - 1]} {year}"
    return str(year)


def _months_elapsed(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _last_active_date(plan) -> date | None:
    """First day of the last month the plan is active, or None if indefinite."""
    if not plan.useful_life_months:
        return None
    start = plan.depreciation_start_date
    abs_month = (start.year * 12 + start.month - 1) + plan.useful_life_months - 1
    y, m = divmod(abs_month, 12)
    return date(y, m + 1, 1)


def _active_months_in_year(plan, year: int) -> int:
    start = plan.depreciation_start_date
    last = _last_active_date(plan)
    count = 0
    for m in range(1, 13):
        month_date = date(year, m, 1)
        if month_date < date(start.year, start.month, 1):
            continue
        if last is not None and month_date > last:
            continue
        count += 1
    return count


def build_depreciation_report(year: int, month: int | None = None):
    """
    Returns (rows, totals, label).

    Each row dict has: inventory_number, name, kst_category, start_date,
    initial_value, residual_value, depreciation_base, method_display,
    annual_rate_percent, period_charge, accumulated, net_value.
    """
    end = period_end_date(year, month)
    label = period_label(year, month)

    plans = (
        AssetDepreciationPlan.objects
        .filter(
            enabled=True,
            depreciation_start_date__isnull=False,
            depreciation_start_date__lte=end,
            monthly_depreciation_amount__isnull=False,
            asset__status=Asset.Status.ACTIVE,
        )
        .select_related("asset")
        .order_by("asset__inventory_number")
    )

    rows = []
    for plan in plans:
        start = plan.depreciation_start_date
        initial = plan.initial_value or Decimal("0")
        residual = plan.residual_value or Decimal("0")
        monthly = plan.monthly_depreciation_amount
        base = (initial - residual).quantize(_MONEY, rounding=ROUND_HALF_UP)

        if base <= 0 or not monthly:
            continue

        elapsed = _months_elapsed(start, end)
        if plan.useful_life_months:
            elapsed = min(elapsed, plan.useful_life_months)
        accumulated = min(
            (monthly * elapsed).quantize(_MONEY, rounding=ROUND_HALF_UP),
            base,
        )
        net_value = max(initial - accumulated, residual).quantize(_MONEY, rounding=ROUND_HALF_UP)

        if plan.method == AssetDepreciationPlan.Method.ONE_TIME:
            if month:
                period_charge = base if (start.year == year and start.month == month) else Decimal("0")
            else:
                period_charge = base if start.year == year else Decimal("0")
        else:
            if month:
                month_date = date(year, month, 1)
                last = _last_active_date(plan)
                if month_date < date(start.year, start.month, 1):
                    period_charge = Decimal("0")
                elif last is not None and month_date > last:
                    period_charge = Decimal("0")
                else:
                    period_charge = monthly
            else:
                active = _active_months_in_year(plan, year)
                period_charge = (monthly * active).quantize(_MONEY, rounding=ROUND_HALF_UP)

        rows.append({
            "inventory_number": plan.asset.inventory_number or "—",
            "name": plan.asset.name or "—",
            "kst_category": plan.kst_category or "—",
            "start_date": start,
            "initial_value": initial,
            "residual_value": residual,
            "depreciation_base": base,
            "method_display": plan.get_method_display(),
            "annual_rate_percent": plan.annual_rate_percent,
            "period_charge": period_charge,
            "accumulated": accumulated,
            "net_value": net_value,
        })

    def _sum(key):
        vals = [r[key] for r in rows if isinstance(r[key], Decimal)]
        return sum(vals, Decimal("0")).quantize(_MONEY, rounding=ROUND_HALF_UP)

    totals = {
        "initial_value": _sum("initial_value"),
        "depreciation_base": _sum("depreciation_base"),
        "period_charge": _sum("period_charge"),
        "accumulated": _sum("accumulated"),
        "net_value": _sum("net_value"),
    }

    return rows, totals, label


def build_depreciation_report_full_year(year: int):
    """
    Annual report with month-by-month breakdown.

    Returns (rows, totals) where each row has:
      inventory_number, name, bo, months (list[12] Decimal), suma, bz
    """
    year_end = date(year, 12, 31)
    prev_year_end = date(year - 1, 12, 31)

    plans = (
        AssetDepreciationPlan.objects
        .filter(
            enabled=True,
            depreciation_start_date__isnull=False,
            depreciation_start_date__lte=year_end,
            monthly_depreciation_amount__isnull=False,
            asset__status=Asset.Status.ACTIVE,
        )
        .select_related("asset")
        .order_by("asset__inventory_number")
    )

    rows = []
    for plan in plans:
        start = plan.depreciation_start_date
        initial = plan.initial_value or Decimal("0")
        residual = plan.residual_value or Decimal("0")
        monthly = plan.monthly_depreciation_amount
        base = (initial - residual).quantize(_MONEY, rounding=ROUND_HALF_UP)

        if base <= 0 or not monthly:
            continue

        last = _last_active_date(plan)

        # BO: net_value at 1 January of selected year
        if start > prev_year_end:
            bo = initial
        else:
            n_prev = _months_elapsed(start, prev_year_end)
            if plan.useful_life_months:
                n_prev = min(n_prev, plan.useful_life_months)
            acc_prev = min(
                (monthly * n_prev).quantize(_MONEY, rounding=ROUND_HALF_UP),
                base,
            )
            bo = max(initial - acc_prev, residual).quantize(_MONEY, rounding=ROUND_HALF_UP)

        # Monthly charges I–XII
        month_charges = []
        for m in range(1, 13):
            month_date = date(year, m, 1)
            if plan.method == AssetDepreciationPlan.Method.ONE_TIME:
                charge = base if (start.year == year and start.month == m) else Decimal("0")
            else:
                if month_date < date(start.year, start.month, 1):
                    charge = Decimal("0")
                elif last is not None and month_date > last:
                    charge = Decimal("0")
                else:
                    n_before = (year - start.year) * 12 + (m - start.month)
                    acc_before = min(
                        (monthly * n_before).quantize(_MONEY, rounding=ROUND_HALF_UP) if n_before > 0 else Decimal("0"),
                        base,
                    )
                    remaining = base - acc_before
                    charge = (
                        Decimal("0") if remaining <= Decimal("0")
                        else min(monthly, remaining).quantize(_MONEY, rounding=ROUND_HALF_UP)
                    )
            month_charges.append(charge)

        suma = sum(month_charges).quantize(_MONEY, rounding=ROUND_HALF_UP)
        if suma == Decimal("0"):
            continue

        bz = max(bo - suma, residual).quantize(_MONEY, rounding=ROUND_HALF_UP)

        rows.append({
            "inventory_number": plan.asset.inventory_number or "—",
            "name": plan.asset.name or "—",
            "bo": bo,
            "months": month_charges,
            "suma": suma,
            "bz": bz,
        })

    def _sum_month(idx):
        return sum(r["months"][idx] for r in rows).quantize(_MONEY, rounding=ROUND_HALF_UP)

    totals = {
        "bo": sum(r["bo"] for r in rows).quantize(_MONEY, rounding=ROUND_HALF_UP),
        "months": [_sum_month(i) for i in range(12)],
        "suma": sum(r["suma"] for r in rows).quantize(_MONEY, rounding=ROUND_HALF_UP),
        "bz": sum(r["bz"] for r in rows).quantize(_MONEY, rounding=ROUND_HALF_UP),
    }

    return rows, totals
