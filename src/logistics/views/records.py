"""
"Database view": read-only tables of everything the user may see, with
search, per-consignment filter and CSV export (Excel-friendly UTF-8).
This is Oversight's main window into the raw data.
"""
import csv

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import AuditLog
from core.templatetags.portal import day_text as day
from core.templatetags.portal import when_text as when
from logistics import permissions as perms
from logistics.models import (
    Consignment,
    ConsignmentDocument,
    ConsignmentTag,
    DocumentRequirement,
    HandlingUnit,
    Item,
    Movement,
    Thread,
)


def _code(obj):
    return obj.code if obj else ""


def _id(getter):
    """Mark a column of IDs: shown in monospace and never broken across lines."""
    getter.css = "code"
    return getter


def _date(getter):
    """Mark a column of dates, which must not break at a hyphen."""
    getter.css = "nowrap"
    return getter


DATASETS = {
    "consignments": {
        "label": _("Consignments"),
        "queryset": lambda: Consignment.objects.select_related("shipper", "receiver"),
        "scope": "code__in",
        "search": ["code", "reference", "description", "transport_ref"],
        "columns": [
            (_("Consignment"), _id(lambda o: o.code)),
            (_("Reference"), lambda o: o.reference),
            (_("Shipper"), lambda o: o.shipper.name),
            (_("Receiver"), lambda o: o.receiver.name),
            (_("Mode"), lambda o: o.get_transport_mode_display()),
            (_("Route"), lambda o: f"{o.origin} → {o.destination}"),
            (_("Status"), lambda o: o.get_status_display()),
            (_("ETD"), _date(lambda o: day(o.etd))),
            (_("ETA"), _date(lambda o: day(o.eta))),
            (_("B/L, AWB or truck no."), lambda o: o.transport_ref),
            (_("Created"), lambda o: when(o.created_at)),
        ],
    },
    "units": {
        "label": _("Handling units"),
        "queryset": lambda: HandlingUnit.objects.select_related("consignment", "parent"),
        "scope": "consignment__code__in",
        "search": ["code", "container_no", "seal_no", "marks"],
        "columns": [
            (_("Unit ID"), _id(lambda o: o.code)),
            (_("Type"), lambda o: o.get_kind_display()),
            (_("Inside"), _id(lambda o: _code(o.parent))),
            (_("Container no. (ISO 6346)"), _id(lambda o: o.container_no)),
            (_("Seal no."), _id(lambda o: o.seal_no)),
            (_("Gross weight (kg)"), lambda o: o.gross_kg or ""),
            (_("Received"), lambda o: when(o.received_at)),
            (_("Condition"), lambda o: o.get_condition_display()),
        ],
    },
    "items": {
        "label": _("Items"),
        "queryset": lambda: Item.objects.select_related("consignment", "item_type", "unit"),
        "scope": "consignment__code__in",
        "search": ["uid", "oem_serial", "batch_no", "item_type__code", "item_type__name_en"],
        "columns": [
            (_("Item ID"), _id(lambda o: o.uid)),
            (_("Item code"), _id(lambda o: o.item_type.code)),
            (_("Item"), lambda o: o.item_type.name()),
            (_("Quantity"), lambda o: o.quantity),
            (_("Inside"), _id(lambda o: _code(o.unit))),
            (_("Manufacturer serial no."), _id(lambda o: o.oem_serial)),
            (_("Batch / lot no."), _id(lambda o: o.batch_no)),
            (_("Received"), lambda o: when(o.received_at)),
            (_("Condition"), lambda o: o.get_condition_display()),
        ],
    },
    "movements": {
        "label": _("Movements"),
        "queryset": lambda: Movement.objects.select_related("consignment", "item", "unit", "by"),
        "scope": "consignment__code__in",
        "search": ["item__uid", "unit__code", "from_code", "to_code", "reason", "place"],
        "columns": [
            (_("Time"), lambda o: when(o.at)),
            (_("Action"), lambda o: o.get_action_display()),
            (_("ID"), _id(lambda o: o.subject_code)),
            (_("From"), _id(lambda o: o.from_code)),
            (_("To"), _id(lambda o: o.to_code)),
            (_("Quantity"), lambda o: o.quantity),
            (_("Reason"), lambda o: o.reason),
            (_("Location"), lambda o: o.place),
            (_("By"), lambda o: o.by.get_username() if o.by else ""),
        ],
    },
    "documents": {
        "label": _("Documents"),
        "queryset": lambda: ConsignmentDocument.objects.select_related("consignment", "doc_type", "uploaded_by"),
        "scope": "consignment__code__in",
        "search": ["original_name", "doc_type__code", "sha256", "note"],
        "columns": [
            (_("Consignment"), _id(lambda o: o.consignment.code)),
            (_("Document"), lambda o: o.doc_type.name()),
            (_("Version"), lambda o: o.version),
            (_("File name"), lambda o: o.original_name),
            (_("Uploaded"), lambda o: when(o.uploaded_at)),
            (_("By"), lambda o: o.uploaded_by.get_username() if o.uploaded_by else ""),
            (_("SHA-256 fingerprint"), _id(lambda o: o.sha256[:16] + "…")),
            (_("Archived"), lambda o: o.archive_reason if o.is_archived else ""),
        ],
    },
    "requirements": {
        "label": _("Required documents"),
        "queryset": lambda: DocumentRequirement.objects.select_related("consignment", "doc_type").prefetch_related(
            "consignment__milestones", "consignment__documents"
        ),
        "scope": "consignment__code__in",
        "search": ["doc_type__code", "doc_type__name_en"],
        "columns": [
            (_("Consignment"), _id(lambda o: o.consignment.code)),
            (_("Document"), lambda o: o.doc_type.name()),
            (_("Uploaded by"), lambda o: o.doc_type.get_responsible_display()),
            (_("Due"), _date(lambda o: day(o.due_date()))),
            (_("State"), lambda o: DocumentRequirement.State(o.state()).label),
        ],
    },
    "threads": {
        "label": _("Remarks / queries"),
        "queryset": lambda: Thread.objects.select_related("consignment", "raised_by"),
        "scope": "consignment__code__in",
        "search": ["subject"],
        "columns": [
            (_("Consignment"), _id(lambda o: o.consignment.code)),
            (_("Type"), lambda o: o.get_category_display()),
            (_("Subject"), lambda o: o.subject),
            (_("State"), lambda o: o.get_state_display()),
            (_("Raised by"), lambda o: o.raised_by.get_username() if o.raised_by else ""),
            (_("Opened"), lambda o: when(o.created_at)),
            (_("Last update"), lambda o: when(o.updated_at)),
        ],
    },
    "tags": {
        "label": _("Tags & status updates"),
        "queryset": lambda: ConsignmentTag.objects.select_related("consignment", "added_by"),
        "scope": "consignment__code__in",
        "search": ["note", "kind"],
        "columns": [
            (_("Consignment"), _id(lambda o: o.consignment.code)),
            (_("Type"), lambda o: o.get_kind_display()),
            (_("What happened"), lambda o: o.note),
            (_("Revised ETA"), _date(lambda o: day(o.revised_eta))),
            (_("Added by"), lambda o: o.added_by.get_username() if o.added_by else ""),
            (_("Added"), lambda o: when(o.added_at)),
            (_("Removed"), lambda o: when(o.removed_at)),
        ],
    },
    "audit": {
        "label": _("Audit log"),
        "queryset": lambda: AuditLog.objects.all(),
        "scope": "consignment_code__in",
        "search": ["username", "action", "target_label", "consignment_code"],
        "requires_audit": True,
        "columns": [
            (_("Time"), lambda o: when(o.at)),
            (_("User"), lambda o: o.username),
            (_("Organisation"), lambda o: o.organization),
            (_("Action"), lambda o: o.action),
            (_("Object"), lambda o: o.target_label),
            (_("Consignment"), _id(lambda o: o.consignment_code)),
            (_("Details"), lambda o: ", ".join(f"{k}={v}" for k, v in (o.details or {}).items())[:200]),
            (_("Hash"), _id(lambda o: o.hash[:12])),
        ],
    },
}


def _dataset_queryset(request, dataset):
    queryset = dataset["queryset"]()
    user = request.user
    if not perms.sees_everything(user):
        codes = list(perms.visible_consignments(user).values_list("code", flat=True))
        queryset = queryset.filter(**{dataset["scope"]: codes})
    code = request.GET.get("consignment", "").strip().upper()
    if code:
        queryset = queryset.filter(**{dataset["scope"]: [code]})
    query = request.GET.get("q", "").strip()
    if query:
        condition = Q()
        for field in dataset["search"]:
            condition |= Q(**{f"{field}__icontains": query})
        queryset = queryset.filter(condition)
    return queryset


@login_required
def records(request, name="consignments"):
    dataset = DATASETS.get(name)
    if dataset is None or (dataset.get("requires_audit") and not perms.sees_audit(request.user)):
        raise Http404
    queryset = _dataset_queryset(request, dataset)
    headers = [str(header) for header, _getter in dataset["columns"]]
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response["Content-Disposition"] = f'attachment; filename="{name}-{stamp}.csv"'
        response.write("﻿")
        writer = csv.writer(response)
        writer.writerow(headers)
        for obj in queryset[:50000]:
            writer.writerow([getter(obj) for _header, getter in dataset["columns"]])
        return response
    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    rows = [[(getter(obj), getattr(getter, "css", "")) for _header, getter in dataset["columns"]]
            for obj in page.object_list]
    tabs = [(key, value["label"]) for key, value in DATASETS.items()
            if not value.get("requires_audit") or perms.sees_audit(request.user)]
    return render(
        request,
        "logistics/records.html",
        {"name": name, "dataset": dataset, "headers": headers, "rows": rows, "page": page, "tabs": tabs,
         "q": request.GET.get("q", ""), "consignment_filter": request.GET.get("consignment", "")},
    )
