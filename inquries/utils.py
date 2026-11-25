"""Inquiry query helpers. Each filter takes a queryset and returns a narrower one,
so they can be combined in any order."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Call, Escalation, Inquiry, RoutingRule


def all_inquiries():
    """Newest first, with related rows loaded in one query instead of one per ticket."""
    return Inquiry.objects.select_related(
        "category",
        "sub_category",
        "commodity",
        "assigned_to",
        "farmer__county",
        "farmer__sub_county",
        "provisional_caller__county",
        "provisional_caller__sub_county",
    ).order_by("-pk")


def by_category(qs, category_id):
    return qs.filter(category_id=category_id)


def by_sub_category(qs, sub_category_id):
    return qs.filter(sub_category_id=sub_category_id)


def by_county(qs, county):
    # the caller is either a KIAMIS farmer or a provisional caller; check both
    return qs.filter(
        Q(farmer__county__name__iexact=county)
        | Q(provisional_caller__county__name__iexact=county)
    )


def by_sub_county(qs, sub_county):
    # sub-county names repeat across counties, so also pass county to by_county()
    return qs.filter(
        Q(farmer__sub_county__name__iexact=sub_county)
        | Q(provisional_caller__sub_county__name__iexact=sub_county)
    )


def by_farmer(qs, farmer_id):
    return qs.filter(farmer_id=farmer_id)  # KIAMIS farmer ID


def by_priority(qs, priority):
    return qs.filter(priority=priority)


def by_ward(qs, ward):
    return qs.filter(
        Q(farmer__ward__name__iexact=ward)
        | Q(provisional_caller__ward__name__iexact=ward)
    )


# ---------------------------------------------------------------------------
# Calls. by_county, by_sub_county, by_ward and by_farmer above also work on
# call querysets, because Call has the same farmer / provisional_caller fields.
# ---------------------------------------------------------------------------
PERIOD_DAYS = {"daily": 0, "weekly": 7, "monthly": 30}  # daily = today only


def all_calls():
    """Newest first, with related rows loaded in one query."""
    return Call.objects.select_related(
        "agent",
        "farmer__county",
        "farmer__sub_county",
        "farmer__ward",
        "provisional_caller__county",
        "provisional_caller__sub_county",
        "provisional_caller__ward",
    ).order_by("-start_time")


def calls_in_period(qs, period):
    """period: daily (today), weekly (last 7 days) or monthly (last 30 days)."""
    today = timezone.localdate()
    return qs.filter(
        start_time__date__gte=today - timedelta(days=PERIOD_DAYS[period]),
        start_time__date__lte=today,
    )


def by_agent(qs, agent_id):
    return qs.filter(agent_id=agent_id)


def by_provisional_caller(qs, provisional_caller_id):
    return qs.filter(provisional_caller_id=provisional_caller_id)


def by_caller_type(qs, caller_type):
    """farmer = found in KIAMIS, provisional = not in KIAMIS, unknown = lookup did not run."""
    if caller_type == "farmer":
        return qs.filter(farmer__isnull=False)
    if caller_type == "provisional":
        return qs.filter(provisional_caller__isnull=False)
    return qs.filter(farmer__isnull=True, provisional_caller__isnull=True)


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------
def all_escalations():
    return Escalation.objects.select_related("ticket", "assigned_to").order_by(
        "-assigned_at"
    )


def escalations_by_status(qs, status):
    """open / acknowledged / resolved as stored. 'overdue' also catches open or acknowledged
    escalations whose due time has passed, so it is right even if the 15-minute job is late.
    """
    if status == "overdue":
        return qs.filter(
            Q(status="overdue")
            | Q(status__in=["open", "acknowledged"], due_at__lt=timezone.now())
        )
    return qs.filter(status=status)


def escalations_by_level(qs, level):
    return qs.filter(level=level)


def escalations_by_assignee(qs, user_id):
    return qs.filter(assigned_to_id=user_id)


def escalations_by_ticket(qs, ticket_id):
    return qs.filter(ticket_id=ticket_id)


def escalations_assigned_between(qs, start=None, end=None):
    if start:
        qs = qs.filter(assigned_at__date__gte=start)
    if end:
        qs = qs.filter(assigned_at__date__lte=end)
    return qs


def escalations_due_between(qs, start=None, end=None):
    if start:
        qs = qs.filter(due_at__date__gte=start)
    if end:
        qs = qs.filter(due_at__date__lte=end)
    return qs


# ---------------------------------------------------------------------------
# Routing rules. A rule sits on one admin unit (ward, sub-county or county), so
# "in a county" means the county itself or any sub-county / ward below it.
# ---------------------------------------------------------------------------
def all_routing_rules():
    return RoutingRule.objects.select_related(
        "category", "admin_unit__parent__parent"
    ).order_by("category__name", "level", "admin_unit__name")


def rules_by_category(qs, category_id):
    return qs.filter(category_id=category_id)


def rules_by_level(qs, level):
    return qs.filter(level=level)


def rules_in_ward(qs, ward):
    return qs.filter(admin_unit__level="ward", admin_unit__name__iexact=ward)


def rules_in_sub_county(qs, sub_county):
    return qs.filter(
        Q(admin_unit__level="sub_county", admin_unit__name__iexact=sub_county)
        | Q(admin_unit__level="ward", admin_unit__parent__name__iexact=sub_county)
    )


def rules_in_county(qs, county):
    return qs.filter(
        Q(admin_unit__level="county", admin_unit__name__iexact=county)
        | Q(admin_unit__level="sub_county", admin_unit__parent__name__iexact=county)
        | Q(admin_unit__level="ward", admin_unit__parent__parent__name__iexact=county)
    )


def rules_by_officer(qs, text):
    """Matches the officer's name, email or phone number."""
    return qs.filter(
        Q(officer__icontains=text)
        | Q(officer_email__icontains=text)
        | Q(officer_phone__icontains=text)
    )


# ---------------------------------------------------------------------------
# Case actions (audit trail)
# ---------------------------------------------------------------------------
def all_case_actions():
    return CaseAction.objects.select_related("ticket", "user").order_by("-timestamp")


def actions_by_ticket(qs, ticket_id):
    return qs.filter(ticket_id=ticket_id)


def actions_by_user(qs, user_id):
    return qs.filter(user_id=user_id)


def system_actions(qs):
    """Things the system did on its own: routing, SLA flags, automatic escalation."""
    return qs.filter(user__isnull=True)


def actions_by_type(qs, action_type):
    return qs.filter(action_type=action_type)


def actions_in_period(qs, period):
    """period: daily (today), weekly (last 7 days) or monthly (last 30 days)."""
    today = timezone.localdate()
    return qs.filter(
        timestamp__date__gte=today - timedelta(days=PERIOD_DAYS[period]),
        timestamp__date__lte=today,
    )


def actions_between(qs, start=None, end=None):
    if start:
        qs = qs.filter(timestamp__date__gte=start)
    if end:
        qs = qs.filter(timestamp__date__lte=end)
    return qs


# ---------------------------------------------------------------------------
# SMS log
# ---------------------------------------------------------------------------
def all_sms_logs():
    return SmsLog.objects.select_related("ticket", "sent_by").order_by("-created_at")


def sms_by_status(qs, status):
    return qs.filter(status=status)


def sms_by_ticket(qs, ticket_id):
    return qs.filter(ticket_id=ticket_id)


def sms_by_phone(qs, text):
    return qs.filter(phone_number__icontains=text)


def sms_by_sender(qs, user_id):
    return qs.filter(sent_by_id=user_id)


def sms_in_period(qs, period):
    today = timezone.localdate()
    return qs.filter(
        created_at__date__gte=today - timedelta(days=PERIOD_DAYS[period]),
        created_at__date__lte=today,
    )


def sms_between(qs, start=None, end=None):
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    return qs
