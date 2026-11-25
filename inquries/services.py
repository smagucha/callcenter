import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from servicedesk.models import (
    FarmerRef,
    FarmerPhone,
)
from .models import (
    AdminUnit,
    Call,
    CaseAction,
    Escalation,
    FarmerRef,
    Inquiry,
    AdminUnit,
    ProvisionalCaller,
    RoutingRule,
)

CACHE_DAYS = 7  # re-check KIAMIS after this long
ESCALATION_HOURS = {"critical": 2, "high": 8, "medium": 24, "low": 48}

ALLOWED = {
    Inquiry.Status.IN_PROGRESS: {"escalated", "awaiting_farmer", "resolved"},
    Inquiry.Status.ESCALATED: {"in_progress", "awaiting_farmer", "resolved"},
    Inquiry.Status.AWAITING_FARMER: {"in_progress", "resolved", "closed"},
    Inquiry.Status.RESOLVED: {"closed", "reopened"},
    Inquiry.Status.CLOSED: {"reopened"},
    Inquiry.Status.REOPENED: {
        "in_progress",
        "escalated",
        "awaiting_farmer",
        "resolved",
    },
}


# ------------------------------------------------------------ phone numbers
def normalise_phone(raw, default_cc="254"):
    """Any format -> 2547XXXXXXXX."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = default_cc + digits[1:]
    elif len(digits) == 9:
        digits = default_cc + digits
    return digits


def phone_variants(phone):
    """KIAMIS may store 07..., 7..., 2547... or +2547...; match all."""
    core = phone[-9:]
    return {phone, "0" + core, core, "+" + phone}


# ------------------------------------------------- caller matching (KIAMIS)
def _unit(code):
    return AdminUnit.objects.filter(code=code).first() if code else None


def cache_farmer(k):
    """Save or refresh the local snapshot of a KIAMIS farmer."""
    farmer, _ = FarmerRef.objects.update_or_create(
        farmer_id=k.farmer_id,
        defaults=dict(
            national_id=k.national_id or "",
            full_name=k.full_name,
            gender=k.gender or "",
            county=_unit(k.county_code),
            sub_county=_unit(k.subcounty_code),
            ward=_unit(k.ward_code),
            last_synced_at=timezone.now(),
        ),
    )
    FarmerPhone.objects.update_or_create(
        farmer=farmer,
        phone_number=normalise_phone(k.phone),
        defaults={"is_primary": True},
    )
    return farmer


def match_caller(phone):
    """Return a FarmerRef, or None if the number is not in KIAMIS.
    Uses the local cache when fresh; asks KIAMIS when stale or unknown.
    Raises if KIAMIS is unreachable and there is no cached copy."""
    cached = (
        FarmerPhone.objects.select_related("farmer").filter(phone_number=phone).first()
    )
    if cached and cached.farmer.last_synced_at > timezone.now() - timedelta(
        days=CACHE_DAYS
    ):
        return cached.farmer
    try:
        k = (
            Farmers.objects.using("farmers")
            .filter(phone__in=phone_variants(phone))
            .first()
        )
    except Exception:
        if cached:
            return cached.farmer  # KIAMIS down: the cache is good enough
        raise
    if k:
        return cache_farmer(k)
    return cached.farmer if cached else None


def attach_caller(call):
    """Registry match -> call.farmer. No match -> call.provisional_caller (created if new)."""
    farmer = match_caller(call.phone_number)
    if farmer:
        Call.objects.filter(pk=call.pk).update(farmer=farmer)
        return
    provisional, _ = ProvisionalCaller.objects.get_or_create(
        phone_number=call.phone_number
    )
    Call.objects.filter(pk=call.pk).update(provisional_caller=provisional)


def complete_provisional(
    caller, user, *, name, ward, village="", national_id="", enterprise="", notes=""
):
    caller.name, caller.ward, caller.village = name, ward, village
    caller.sub_county = ward.parent
    caller.county = ward.parent.parent if ward.parent else None
    caller.national_id, caller.enterprise, caller.notes = national_id, enterprise, notes
    caller.registration_status = ProvisionalCaller.Status.PENDING
    caller.captured_by = user
    caller.save()
    return caller


# ------------------------------------------------------------------- audit
def log(ticket, action_type, user=None, from_value="", to_value="", notes=""):
    return CaseAction.objects.create(
        ticket=ticket,
        user=user,
        action_type=action_type,
        from_value=str(from_value),
        to_value=str(to_value),
        notes=notes,
    )


# --------------------------------------------------------------- SLA + routing
# def sla_targets(ticket):
#     """(first response hours, resolution hours): priority override, else sub-category, else category."""
#     t = (
#         ServiceLevelTarget.objects.filter(
#             category=ticket.category, priority=ticket.priority
#         ).first()
#         or ServiceLevelTarget.objects.filter(
#             category=ticket.category, priority=""
#         ).first()
#     )
#     if t:
#         return t.first_response_hours, t.resolution_hours
#     return (
#         ticket.category.first_response_hours,
#         ticket.sub_category.sla_hours or ticket.category.sla_hours,
#     )


# def apply_sla(ticket):
#     first, resolve = sla_targets(ticket)
#     start = ticket.created_at or timezone.now()
#     ticket.first_response_due = start + timedelta(hours=first)
#     ticket.resolution_due = start + timedelta(hours=resolve)


def unit_at_level(ward, level):
    if level == RoutingRule.Level.WARD:
        return ward
    if level == RoutingRule.Level.SUB_COUNTY:
        return ward.parent
    if level == RoutingRule.Level.COUNTY:
        return ward.parent.parent if ward.parent else None
    return None  # national


def resolve_rule(ticket, level):
    """The routing rule (officer name, email, phone) for this ticket at this level, or None."""
    rules = RoutingRule.objects.filter(category=ticket.category, level=level)
    if level != RoutingRule.Level.NATIONAL:
        rules = rules.filter(admin_unit=unit_at_level(ticket.ward, level))
    return rules.first()


def resolve_officer(ticket, level):
    """The rule's officer as a system User, matched by email, or None.
    If the officer has no login account, use resolve_rule() to notify them by email / SMS instead.
    """
    rule = resolve_rule(ticket, level)
    if rule is None or not rule.officer_email:
        return None
    return (
        get_user_model()
        .objects.filter(email__iexact=rule.officer_email, is_active=True)
        .first()
    )


# -------------------------------------------------------------- ticket actions
@transaction.atomic
def create_ticket(
    *,
    created_by,
    category,
    sub_category,
    description,
    call=None,
    farmer=None,
    provisional_caller=None,
    commodity=None,
    ward=None,
):
    caller = farmer or provisional_caller
    ward = ward or getattr(caller, "ward", None)
    if ward is None:
        raise ValueError("A ward is required (the caller has none on file).")

    ticket = Inquiry(
        call=call,
        farmer=farmer,
        provisional_caller=provisional_caller,
        category=category,
        sub_category=sub_category,
        commodity=commodity,
        description=description,
        ward=ward,
        priority=sub_category.default_priority,
        created_by=created_by,
    )
    ticket.save()
    apply_sla(ticket)
    ticket.assigned_to = resolve_officer(ticket, RoutingRule.Level.WARD)
    ticket.save()

    log(ticket, "created", created_by, to_value=ticket.priority)
    if ticket.assigned_to:
        log(
            ticket,
            "assigned",
            None,
            to_value=ticket.assigned_to_id,
            notes="Auto-routed",
        )
    if ticket.priority == "critical":  # e.g. suspected disease outbreak
        escalate(ticket, None, reason="Critical priority: automatic escalation")
    return ticket


@transaction.atomic
def change_status(ticket, new_status, user, notes=""):
    if new_status not in ALLOWED[ticket.status]:
        raise ValueError(f"Cannot move from {ticket.status} to {new_status}")
    old, ticket.status = ticket.status, new_status
    now = timezone.now()
    if new_status == Inquiry.Status.RESOLVED:
        ticket.resolved_at = now
    elif new_status == Inquiry.Status.CLOSED:
        ticket.closed_at = now
    elif new_status == Inquiry.Status.REOPENED:
        ticket.resolved_at = ticket.closed_at = None
        ticket.overdue_alerted_at = None
    if (
        not ticket.first_response_at
    ):  # first action on the ticket counts as the first response
        ticket.first_response_at = now
    ticket.save()
    log(
        ticket,
        "reopened" if new_status == "reopened" else "status_changed",
        user,
        old,
        new_status,
        notes,
    )


@transaction.atomic
def escalate(ticket, user, reason=""):
    level = min(ticket.escalation_level + 1, RoutingRule.Level.NATIONAL)
    officer = resolve_officer(ticket, level)
    old_officer = ticket.assigned_to_id

    ticket.escalations.filter(status__in=["open", "acknowledged", "overdue"]).update(
        status=Escalation.Status.RESOLVED
    )
    Escalation.objects.create(
        ticket=ticket,
        level=level,
        assigned_to=officer,
        reason=reason,
        due_at=timezone.now() + timedelta(hours=ESCALATION_HOURS[ticket.priority]),
    )

    ticket.escalation_level = level
    ticket.status = Inquiry.Status.ESCALATED
    if officer:
        ticket.assigned_to = officer
    ticket.save()
    log(
        ticket,
        "escalated",
        user,
        old_officer or "",
        officer.pk if officer else "no officer found",
        f"Level {level}. {reason}",
    )


def change_ward(ticket, new_ward, user):
    """The farmer is calling about another farm."""
    old = ticket.ward_id
    ticket.ward = new_ward
    ticket.assigned_to = (
        resolve_officer(ticket, ticket.escalation_level) or ticket.assigned_to
    )
    ticket.save()
    log(
        ticket,
        "field_edited",
        user,
        f"ward:{old}",
        f"ward:{new_ward.pk}",
        "Location changed",
    )


def flag_overdue():
    """Run every 15 minutes (Celery beat, or cron + a management command)."""
    now = timezone.now()
    late = Inquiry.objects.filter(
        resolution_due__lt=now, overdue_alerted_at__isnull=True
    ).exclude(status__in=["resolved", "closed"])
    for t in late:
        t.overdue_alerted_at = now
        t.save(update_fields=["overdue_alerted_at"])
        log(t, "sla_breached", None, notes=f"Due {t.resolution_due:%Y-%m-%d %H:%M}")
        # notify t.assigned_to and the next-level officer here (email / SmsLog)
    Escalation.objects.filter(
        status__in=["open", "acknowledged"], due_at__lt=now
    ).update(status=Escalation.Status.OVERDUE)


# ------------------------------------------------------------------ KIAMIS sync
def sync_admin_units():
    """Copy counties, sub-counties and wards from KIAMIS. Run once, then now and then."""
    rows = list(AdminUnit.objects.using("farmers").all())
    for level in ("county", "sub_county", "ward"):  # parents before children
        for r in (x for x in rows if x.level == level):
            parent = (
                AdminUnit.objects.filter(code=r.parent_code).first()
                if r.parent_code
                else None
            )
            AdminUnit.objects.update_or_create(
                code=r.code, defaults={"name": r.name, "level": level, "parent": parent}
            )


def resolve_rule(ticket, level):
    """The routing rule (officer name, email, phone) for this ticket at this level, or None."""
    rules = RoutingRule.objects.filter(category=ticket.category, level=level)
    if level != RoutingRule.Level.NATIONAL:
        rules = rules.filter(admin_unit=unit_at_level(ticket.ward, level))
    return rules.first()


def resolve_officer(ticket, level):
    """The rule's officer as a system User, matched by email, or None.
    If the officer has no login account, use resolve_rule() to notify them by email / SMS instead.
    """
    rule = resolve_rule(ticket, level)
    if rule is None or not rule.officer_email:
        return None
    return (
        get_user_model()
        .objects.filter(email__iexact=rule.officer_email, is_active=True)
        .first()
    )


def send_sms_messsage(*, phone, message, user=None, ticket=None):
    sms = SmsLog.objects.cretae(
        ticket=ticket, phone_number=phone, message=message, sent_by=user
    )
    ok, provider = ref, error = sms_gateway.send(phone, message)
    sms.status = SmsLog.status.SENT if ok else SmsLog.status.FAILED
    sms.provider_ref, sms.error = provider, error
    sms.sent_at = timezone.now() if ok else None
    sms.save()
    if ticket:
        log(
            ticket,
            "sms_sent",
            user,
            to_value=phone,
            notes=message[:100] if ok else f"FAILED:{error}",
        )
    return sms


# TO IMPLEMENT WHATSAPP API
