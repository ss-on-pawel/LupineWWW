from django.contrib import messages
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView

from .forms import AssetForm
from .models import Asset


class AssetListView(ListView):
    model = Asset
    template_name = "assets/asset_list.html"
    context_object_name = "assets"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Ewidencja majątku"
        return context


class AssetCreateView(CreateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    success_url = reverse_lazy("assets:list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Dodaj składnik majątku"
        return context

    def form_valid(self, form):
        messages.success(self.request, "Składnik majątku został zapisany.")
        return super().form_valid(form)


def asset_detail(request, id):
    asset = get_object_or_404(Asset, pk=id)
    return render(
        request,
        "assets/asset_detail.html",
        {
            "asset": asset,
            "page_title": "Karta środka",
        },
    )
