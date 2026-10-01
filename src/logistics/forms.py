import re

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import Organization
from core.ids import iso6346_valid
from logistics.models import Consignment, ConsignmentTag, DocumentType, HandlingUnit, Item, ItemType, Thread


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class ConsignmentForm(forms.ModelForm):
    class Meta:
        model = Consignment
        fields = [
            "receiver", "reference", "description", "transport_mode", "incoterm", "origin", "destination",
            "carrier", "transport_ref", "etd", "eta", "planned_packages", "notes",
        ]
        widgets = {"etd": DateInput(), "eta": DateInput(), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["receiver"].queryset = Organization.objects.filter(kind=Organization.Kind.RECEIVER, is_active=True)
        self.fields["origin"].widget.attrs["placeholder"] = _("e.g. Shenzhen (Yantian port)")
        self.fields["destination"].widget.attrs["placeholder"] = _("e.g. Karachi (Port Qasim) → Rawalpindi")
        if editing:
            del self.fields["receiver"]

    def clean(self):
        cleaned = super().clean()
        etd, eta = cleaned.get("etd"), cleaned.get("eta")
        if etd and eta and eta < etd:
            self.add_error("eta", _("ETA cannot be before ETD."))
        return cleaned


class StatusForm(forms.Form):
    status = forms.ChoiceField(label=_("New milestone"))
    at = forms.DateTimeField(label=_("When did it happen?"), widget=DateTimeInput(), required=False)
    note = forms.CharField(label=_("Note"), max_length=300, required=False)

    def __init__(self, *args, statuses=(), **kwargs):
        super().__init__(*args, **kwargs)
        labels = dict(Consignment.Status.choices)
        self.fields["status"].choices = [(value, labels[value]) for value in statuses]
        self.fields["at"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")


def clean_container_number(value):
    """ISO 6346 numbers (4 letters + 7 digits) must have a valid check digit; truck plates etc. pass as typed."""
    value = (value or "").strip().upper()
    compact = re.sub(r"[\s-]", "", value)
    if re.fullmatch(r"[A-Z]{4}\d{7}", compact) and not iso6346_valid(compact):
        raise forms.ValidationError(_("The check digit of this container number is wrong. Please re-check it."))
    return compact if re.fullmatch(r"[A-Z]{4}\d{7}", compact) else value


def unit_choices(consignment, kinds=None, exclude=None, empty_label=None):
    queryset = consignment.units.filter(is_archived=False).order_by("kind", "seq")
    if kinds:
        queryset = queryset.filter(kind__in=kinds)
    choices = [("", empty_label)] if empty_label is not None else []
    for unit in queryset:
        if exclude is not None and unit.pk == exclude:
            continue
        choices.append((unit.pk, f"{unit.short} · {unit.get_kind_display()}"))
    return choices


class AddUnitsForm(forms.Form):
    kind = forms.ChoiceField(label=_("Type"), choices=HandlingUnit.Kind.choices, initial=HandlingUnit.Kind.CARTON)
    count = forms.IntegerField(label=_("How many"), min_value=1, max_value=1000, initial=1)
    parent = forms.ChoiceField(label=_("Place inside"), required=False)
    container_no = forms.CharField(label=_("Container no. (ISO 6346)"), max_length=20, required=False)
    seal_no = forms.CharField(label=_("Seal no."), max_length=30, required=False)
    gross_kg = forms.DecimalField(label=_("Gross weight each (kg)"), required=False, max_digits=9, decimal_places=2)
    length_cm = forms.DecimalField(label=_("Length (cm)"), required=False, max_digits=7, decimal_places=1)
    width_cm = forms.DecimalField(label=_("Width (cm)"), required=False, max_digits=7, decimal_places=1)
    height_cm = forms.DecimalField(label=_("Height (cm)"), required=False, max_digits=7, decimal_places=1)

    def __init__(self, *args, consignment=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.consignment = consignment
        self.fields["parent"].choices = unit_choices(consignment, empty_label=_("— not inside anything —"))

    def clean_parent(self):
        value = self.cleaned_data.get("parent")
        if not value:
            return None
        return self.consignment.units.filter(pk=value, is_archived=False).first()

    def clean_container_no(self):
        return clean_container_number(self.cleaned_data.get("container_no"))


class AddItemsForm(forms.Form):
    item_type = forms.ModelChoiceField(label=_("Item"), queryset=ItemType.objects.filter(is_active=True))
    quantity = forms.IntegerField(label=_("Total quantity"), min_value=1, max_value=10000)
    units = forms.MultipleChoiceField(
        label=_("Pack into"),
        required=False,
        help_text=_("Select one or more units; the quantity is spread across them in order."),
    )
    per_unit = forms.IntegerField(
        label=_("Max. per unit"), min_value=1, required=False,
        help_text=_("Leave empty to spread evenly (40 over 3 cartons → 14, 13, 13)."),
    )
    oem_serials = forms.CharField(
        label=_("Manufacturer serial numbers"), required=False, widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Optional, one per line, in packing order."),
    )
    batch_no = forms.CharField(label=_("Batch / lot no."), max_length=60, required=False)

    def __init__(self, *args, consignment=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.consignment = consignment
        self.fields["units"].choices = unit_choices(consignment)
        self.fields["units"].widget.attrs["size"] = 8

    def clean_units(self):
        ids = [int(value) for value in self.cleaned_data.get("units") or []]
        units = {unit.pk: unit for unit in self.consignment.units.filter(pk__in=ids, is_archived=False)}
        return [units[pk] for pk in ids if pk in units]

    def clean_oem_serials(self):
        return [line.strip() for line in (self.cleaned_data.get("oem_serials") or "").splitlines() if line.strip()]


class MoveForm(forms.Form):
    to_unit = forms.ChoiceField(label=_("Move into"), required=False)
    quantity = forms.IntegerField(label=_("Quantity to move"), min_value=1, required=False)
    reason = forms.CharField(label=_("Reason"), max_length=300, required=False,
                             widget=forms.TextInput(attrs={"placeholder": _("e.g. repacked after customs inspection")}))
    place = forms.CharField(label=_("Location"), max_length=120, required=False,
                            widget=forms.TextInput(attrs={"placeholder": _("e.g. Karachi port, shed 4")}))

    def __init__(self, *args, obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.obj = obj
        consignment = obj.consignment
        if isinstance(obj, HandlingUnit):
            kinds = [kind for kind, rank in HandlingUnit.RANK.items() if rank > obj.rank]
            self.fields["to_unit"].choices = unit_choices(consignment, kinds=kinds, exclude=obj.pk,
                                                          empty_label=_("— take out (loose) —"))
            del self.fields["quantity"]
        else:
            self.fields["to_unit"].choices = unit_choices(consignment, empty_label=_("— take out (loose) —"))
            if obj.item_type.is_serial:
                del self.fields["quantity"]
            else:
                self.fields["quantity"].max_value = obj.quantity
                self.fields["quantity"].initial = obj.quantity

    def clean_to_unit(self):
        value = self.cleaned_data.get("to_unit")
        if not value:
            return None
        return self.obj.consignment.units.filter(pk=value, is_archived=False).first()


class UnitEditForm(forms.ModelForm):
    class Meta:
        model = HandlingUnit
        fields = ["container_no", "seal_no", "length_cm", "width_cm", "height_cm", "gross_kg", "marks"]

    def clean_container_no(self):
        return clean_container_number(self.cleaned_data.get("container_no"))


class ItemEditForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = ["oem_serial", "batch_no"]


class ReceiveForm(forms.Form):
    condition = forms.ChoiceField(label=_("Condition"), choices=HandlingUnit.Condition.choices)
    include_contents = forms.BooleanField(
        label=_("Also mark everything inside as received"), required=False, initial=False
    )
    place = forms.CharField(label=_("Location"), max_length=120, required=False)


class UploadForm(forms.Form):
    doc_type = forms.ModelChoiceField(label=_("Document"), queryset=DocumentType.objects.none())
    file = forms.FileField(label=_("File"))
    note = forms.CharField(label=_("Note"), max_length=300, required=False)

    def __init__(self, *args, doc_types=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doc_type"].queryset = DocumentType.objects.filter(pk__in=[dt.pk for dt in doc_types])


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=300)


class RequirementForm(forms.Form):
    doc_type = forms.ModelChoiceField(label=_("Document"), queryset=DocumentType.objects.filter(is_active=True))
    due_override = forms.DateField(label=_("Due date (optional)"), required=False, widget=DateInput())


class TagForm(forms.Form):
    kind = forms.ChoiceField(label=_("Type"), choices=ConsignmentTag.Kind.choices, initial=ConsignmentTag.Kind.UPDATE)
    note = forms.CharField(
        label=_("What happened"), max_length=300, required=False, widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("For example: vessel missed the connection at Colombo; next sailing in 7 days."),
    )
    revised_eta = forms.DateField(label=_("Revised ETA (delays only)"), required=False, widget=DateInput())


class ThreadForm(forms.Form):
    category = forms.ChoiceField(label=_("Type"), choices=Thread.Category.choices)
    subject = forms.CharField(label=_("Subject"), max_length=200)
    body = forms.CharField(label=_("Message"), max_length=4000, widget=forms.Textarea(attrs={"rows": 5}))
    about = forms.CharField(
        label=_("About unit or item (optional)"), max_length=60, required=False,
        widget=forms.TextInput(attrs={"placeholder": "CTN-017 / DCJM-017"}),
    )


class ReplyForm(forms.Form):
    body = forms.CharField(label=_("Reply"), max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))


class ImportForm(forms.Form):
    file = forms.FileField(label=_("CSV file"))


class ItemTypeForm(forms.ModelForm):
    class Meta:
        model = ItemType
        fields = ["code", "name_en", "name_zh", "name_ur", "hs_code", "tracking", "uom", "controlled", "is_active"]

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if code in HandlingUnit.PREFIX.values():
            raise forms.ValidationError(_("%(code)s is reserved for packaging units.") % {"code": code})
        if self.instance.pk and self.instance.code != code and self.instance.items.exists():
            raise forms.ValidationError(_("The code cannot change once items use it (their IDs contain it)."))
        return code
