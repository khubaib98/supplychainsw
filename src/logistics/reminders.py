"""
Missing-document reminders with an escalation ladder.

For every required document that is due and not uploaded:
  level 1 (on the due date)        -> responsible party
  level 2 (N days overdue)         -> responsible party again
  level 3 (M days overdue)         -> responsible party's managers + Oversight
The day offsets come from settings.PORTAL["REMINDER_DAYS"] (default 0, 2, 5).
Each level is sent once; running the job again the same day sends nothing new.
"""
from django.conf import settings
from django.utils import timezone

from accounts.models import Profile
from core import notify
from logistics.models import Consignment, DocumentRequirement, DocumentType, ReminderLog
from logistics.services import members, oversight_members


def _responsible_orgs(requirement):
    consignment = requirement.consignment
    responsible = requirement.doc_type.responsible
    if responsible == DocumentType.Party.SHIPPER:
        return [consignment.shipper]
    if responsible == DocumentType.Party.RECEIVER:
        return [consignment.receiver]
    return [consignment.shipper, consignment.receiver]


def run_reminders(today=None):
    today = today or timezone.localdate()
    thresholds = settings.PORTAL["REMINDER_DAYS"]
    sent = []
    requirements = (
        DocumentRequirement.objects.filter(consignment__is_archived=False, waived=False)
        .exclude(consignment__status=Consignment.Status.CLOSED)
        .select_related("consignment__shipper", "consignment__receiver", "doc_type")
        .prefetch_related("reminders", "consignment__milestones", "consignment__documents")
    )
    for requirement in requirements:
        state = requirement.state(today)
        if state not in (DocumentRequirement.State.DUE, DocumentRequirement.State.OVERDUE):
            continue
        days = requirement.days_overdue(today)
        already = {reminder.level for reminder in requirement.reminders.all()}
        reached = [level for level, threshold in enumerate(thresholds, start=1) if days >= threshold]
        if not reached or max(reached) in already:
            continue
        level = max(reached)
        escalate = level == len(thresholds)
        organizations = _responsible_orgs(requirement)
        recipients = []
        for organization in organizations:
            levels = (Profile.Level.MANAGER,) if escalate else (Profile.Level.MANAGER, Profile.Level.OPERATOR)
            recipients.extend(members(organization, levels=levels))
        consignment = requirement.consignment
        params = {
            "code": consignment.code,
            "doc_type": requirement.doc_type.code,
            "days": days,
            "org": ", ".join(organization.name for organization in organizations),
        }
        url = f"/consignments/{consignment.code}/documents/"
        kind = "doc_due" if days == 0 else "doc_overdue"
        notify.notify(recipients, kind, params, consignment=consignment, url=url, organizations=organizations)
        if escalate:
            notify.notify(oversight_members(), "doc_escalated", params, consignment=consignment, url=url)
        # Record this level and any lower level that was skipped (e.g. the job was offline).
        for missed in reached:
            if missed not in already:
                ReminderLog.objects.create(
                    requirement=requirement,
                    level=missed,
                    recipients=", ".join(sorted({user.get_username() for user in recipients})) if missed == level else "",
                )
        sent.append((requirement, level))
    return sent
