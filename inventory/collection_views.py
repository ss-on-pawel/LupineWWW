from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from config.features import require_feature

from .views import get_visible_inventory_sessions, _build_inventory_session_analysis


@login_required
@require_GET
def collection_report(request, pk):
    require_feature(request, 'inventory_reports')
    session = get_object_or_404(get_visible_inventory_sessions(request.user), pk=pk)
    analysis = _build_inventory_session_analysis(session, user=request.user)
    standalone = [item for item in analysis['inventory_work_items'] if not item['snapshot'].collection_snapshot]
    return render(request, 'inventory/collection_report.html', dict(session=session,
        summaries=analysis['collection_summaries'], standalone=standalone,
        page_title=f'Raport zbiorczy {session.number}'))
