from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import audit
from logistics import permissions as perms
from logistics.forms import ItemTypeForm
from logistics.models import DocumentType, HandlingUnit, ItemType


@login_required
def catalog(request):
    return render(
        request,
        "logistics/catalog.html",
        {
            "item_types": ItemType.objects.all(),
            "doc_types": DocumentType.objects.filter(is_active=True),
            "unit_prefixes": [(HandlingUnit.Kind(kind).label, prefix) for kind, prefix in HandlingUnit.PREFIX.items()],
            "can_manage": perms.can_manage_catalog(request.user),
        },
    )


@login_required
def item_type_form(request, pk=None):
    perms.require(perms.can_manage_catalog(request.user))
    instance = get_object_or_404(ItemType, pk=pk) if pk else None
    form = ItemTypeForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        item_type = form.save()
        audit.log(request.user, "catalog.saved", item_type, request=request, details={"fields": form.changed_data})
        messages.success(request, _("Saved."))
        return redirect("logistics:catalog")
    return render(request, "logistics/item_type_form.html", {"form": form, "instance": instance})


@login_required
def coding_help(request):
    return render(request, "logistics/coding_help.html")
