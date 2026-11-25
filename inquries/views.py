import hmac

from django.conf import settings
from django.db.models import Count, Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import SmsLog
from .serializers import SendSmsSerializer, SmsLogSerializer
from .utils import (
    PERIOD_DAYS,
    all_sms_logs,
    sms_between,
    sms_by_phone,
    sms_by_sender,
    sms_by_status,
    sms_by_ticket,
    sms_in_period,
)


# InquiryPagination


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"  # e.g. ?page_size=50
    max_page_size = 100


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def inquiry_list(request):
    """
    Inquiry list with optional filters (all combinable), via query parameters:
      ?category=3  ?sub_category=12  ?county=Kisumu  ?sub_county=Kisumu Central
      ?ward=Kolwa  ?farmer=KE123456  ?priority=high  ?page=2&page_size=50
    The response also carries a summary: how many of the matching inquiries
    were created today, in the last 7 days and in the last 30 days.

    POST creates an inquiry. Send exactly one of "farmer" (KIAMIS farmer ID) or
    "provisional_caller" (ID), for example:
      {"call": 15, "farmer": "KE123456", "category": 3, "sub_category": 12,
       "sub_sub_category": 7, "commodity": 2, "description": "Maize leaves turning yellow",
       "priority": "medium"}
    "call", "commodity" and "priority" are optional (priority defaults to the
    sub-category's default). Status, ticket number and SLA times are set by the system.
    """
    if request.method == "POST":
        serializer = InquirySerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)  # returns 400 with the field errors
        inquiry = serializer.save()
        return Response(
            InquirySerializer(inquiry, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    p = request.query_params
    qs = all_inquiries()

    # numeric filters
    for name, func in (("category", by_category), ("sub_category", by_sub_category)):
        if p.get(name):
            if not p[name].isdigit():
                return Response({"detail": f"'{name}' must be a number."}, status=400)
            qs = func(qs, int(p[name]))

    # text filters
    if p.get("county"):
        qs = by_county(qs, p["county"])
    if p.get("sub_county"):
        qs = by_sub_county(
            qs, p["sub_county"]
        )  # send county too: names repeat across counties
    if p.get("ward"):
        qs = by_ward(qs, p["ward"])
    if p.get("farmer"):
        qs = by_farmer(qs, p["farmer"])
    if p.get("priority"):
        valid = [value for value, _ in PRIORITY_CHOICES]
        if p["priority"] not in valid:
            return Response(
                {"detail": f"priority must be one of: {', '.join(valid)}."}, status=400
            )
        qs = by_priority(qs, p["priority"])

    # daily / weekly / monthly counts for the filtered inquiries
    today = timezone.localdate()
    summary = qs.aggregate(
        today=Count("pk", filter=Q(created_at__date=today)),
        last_7_days=Count(
            "pk", filter=Q(created_at__date__gte=today - timedelta(days=7))
        ),
        last_30_days=Count(
            "pk", filter=Q(created_at__date__gte=today - timedelta(days=30))
        ),
    )

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    data = InquirySerializer(page, many=True, context={"request": request}).data
    response = paginator.get_paginated_response(data)
    response.data["summary"] = summary
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def call_list(request):
    """
    Call list with optional filters (all combinable), via query parameters:
      ?period=daily|weekly|monthly    today / last 7 days / last 30 days
      ?farmer=KE123456                KIAMIS farmer ID
      ?provisional_caller=8           provisional caller ID
      ?caller_type=farmer|provisional|unknown
      ?agent=4                        agent (user) ID
      ?county=Kisumu  ?sub_county=Kisumu Central  ?ward=Kolwa
      ?page=2&page_size=50

    The response also carries a summary of the matching calls: how many were
    made today, in the last 7 and 30 days, and the average duration in seconds.
    """
    if request.method == "GET":
        p = request.query_params
        qs = all_calls()

        if p.get("period"):
            if p["period"] not in PERIOD_DAYS:
                return Response(
                    {"detail": "period must be one of: daily, weekly, monthly."},
                    status=400,
                )
            qs = calls_in_period(qs, p["period"])

        for name in ("agent", "provisional_caller"):
            if p.get(name) and not p[name].isdigit():
                return Response({"detail": f"'{name}' must be a number."}, status=400)

        if p.get("caller_type"):
            if p["caller_type"] not in ("farmer", "provisional", "unknown"):
                return Response(
                    {
                        "detail": "caller_type must be one of: farmer, provisional, unknown."
                    },
                    status=400,
                )
            qs = by_caller_type(qs, p["caller_type"])

        if p.get("farmer"):
            qs = by_farmer(qs, p["farmer"])
        if p.get("provisional_caller"):
            qs = by_provisional_caller(qs, int(p["provisional_caller"]))
        if p.get("agent"):
            qs = by_agent(qs, int(p["agent"]))
        if p.get("county"):
            qs = by_county(qs, p["county"])
        if p.get("sub_county"):
            qs = by_sub_county(
                qs, p["sub_county"]
            )  # send county too: names repeat across counties
        if p.get("ward"):
            qs = by_ward(qs, p["ward"])

        today = timezone.localdate()
        summary = qs.aggregate(
            today=Count("pk", filter=Q(start_time__date=today)),
            last_7_days=Count(
                "pk", filter=Q(start_time__date__gte=today - timedelta(days=7))
            ),
            last_30_days=Count(
                "pk", filter=Q(start_time__date__gte=today - timedelta(days=30))
            ),
            avg_duration_seconds=Avg("duration"),
        )

        qs = qs.annotate(
            inquiry_count=Count("inquiries")
        )  # tickets raised on each call

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        data = CallSerializer(page, many=True, context={"request": request}).data
        response = paginator.get_paginated_response(data)
        response.data["summary"] = summary
        return response
    else:
        if request.method == "POST":
            serializer = CallCreateSerializer(
                data=request.data, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)  # 400 with the field errors
            call = serializer.save()
            call.refresh_from_db()  # the signal has just attached the caller
            return Response(
                CallSerializer(call, context={"request": request}).data,
                status=status.HTTP_201_CREATED,
            )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def escalation_list(request):
    """
    GET: escalation list with optional filters (all combinable):
      ?status=open|acknowledged|resolved|overdue
      ?level=0|1|2|3                  0 ward, 1 sub-county, 2 county, 3 national
      ?assigned_to=4                  officer (user) ID
      ?ticket=15                      inquiry ID
      ?assigned_from=2026-11-01&assigned_until=2026-11-24     when it was assigned
      ?due_from=2026-11-24&due_until=2026-11-30                when it is due
      ?page=2&page_size=50
    The response carries a summary of open / acknowledged / resolved / overdue counts for
    the other filters you applied (the status filter itself does not narrow the summary).

    POST: escalate a ticket, for example
      {"ticket": 15, "reason": "Suspected foot and mouth outbreak", "assigned_to": 9,
       "level": 2, "due_at": "2026-11-25T12:00:00+03:00"}
    Only "ticket" is required. If "assigned_to" is left out the routing rules pick the officer;
    "level" defaults to one above the ticket's current level; "due_at" defaults from the
    ticket's priority. The ticket is set to Escalated, reassigned, and the action is logged.
    """
    if request.method == "POST":
        serializer = EscalationSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        escalation = serializer.save()
        return Response(
            EscalationSerializer(escalation, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    p = request.query_params
    qs = all_escalations()

    # numeric filters
    for name in ("level", "assigned_to", "ticket"):
        if p.get(name) and not p[name].isdigit():
            return Response({"detail": f"'{name}' must be a number."}, status=400)
    if p.get("level"):
        if int(p["level"]) not in RoutingRule.Level.values:
            return Response(
                {
                    "detail": "level must be 0 (ward), 1 (sub-county), 2 (county) or 3 (national)."
                },
                status=400,
            )
        qs = escalations_by_level(qs, int(p["level"]))
    if p.get("assigned_to"):
        qs = escalations_by_assignee(qs, int(p["assigned_to"]))
    if p.get("ticket"):
        qs = escalations_by_ticket(qs, int(p["ticket"]))

    # date filters (YYYY-MM-DD, inclusive)
    dates = {}
    for name in ("assigned_from", "assigned_until", "due_from", "due_until"):
        if p.get(name):
            dates[name] = parse_date(p[name])
            if dates[name] is None:
                return Response(
                    {"detail": f"'{name}' must be a date like 2026-11-24."}, status=400
                )
    qs = escalations_assigned_between(
        qs, dates.get("assigned_from"), dates.get("assigned_until")
    )
    qs = escalations_due_between(qs, dates.get("due_from"), dates.get("due_until"))

    # summary of every status for what we have so far
    now = timezone.now()
    late = Q(status__in=["open", "acknowledged"], due_at__lt=now) | Q(status="overdue")
    summary = qs.aggregate(
        open=Count("pk", filter=Q(status="open")),
        acknowledged=Count("pk", filter=Q(status="acknowledged")),
        resolved=Count("pk", filter=Q(status="resolved")),
        overdue=Count("pk", filter=late),
    )

    if p.get("status"):
        if p["status"] not in STATUS_FILTERS:
            return Response(
                {"detail": f"status must be one of: {', '.join(STATUS_FILTERS)}."},
                status=400,
            )
        qs = escalations_by_status(qs, p["status"])

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    data = EscalationSerializer(page, many=True, context={"request": request}).data
    response = paginator.get_paginated_response(data)
    response.data["summary"] = summary
    return response


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def case_action_list(request):
    """
    The audit trail across all tickets, newest first.

    GET filters (all combinable):
      ?ticket=15                      one ticket's actions
      ?user=4                         actions by one person
      ?system=true                    only actions the system did automatically
      ?action_type=escalated          created, status_changed, priority_changed, assigned,
                                      escalated, field_edited, note, sms_sent, sla_breached, reopened
      ?period=daily|weekly|monthly    today / last 7 days / last 30 days
      ?from=2026-11-01&until=2026-11-24   date range, inclusive
      ?page=2&page_size=50
    The response carries a summary: how many matching actions there are of each type.

    POST adds a note to a ticket: {"ticket": 15, "notes": "Called the farmer, no answer."}
    Notes are the only thing that can be added. Everything else is recorded by the system,
    and entries can never be edited or deleted.
    """
    if request.method == "POST":
        serializer = CaseNoteSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        action = serializer.save()
        return Response(
            CaseActionSerializer(action).data, status=status.HTTP_201_CREATED
        )

    p = request.query_params
    qs = all_case_actions()

    for name in ("ticket", "user"):
        if p.get(name) and not p[name].isdigit():
            return Response({"detail": f"'{name}' must be a number."}, status=400)
    if p.get("ticket"):
        qs = actions_by_ticket(qs, int(p["ticket"]))
    if p.get("user"):
        qs = actions_by_user(qs, int(p["user"]))
    if p.get("system", "").lower() == "true":
        qs = system_actions(qs)

    if p.get("action_type"):
        valid = [value for value, _ in CaseAction.ActionType.choices]
        if p["action_type"] not in valid:
            return Response(
                {"detail": f"action_type must be one of: {', '.join(valid)}."},
                status=400,
            )
        qs = actions_by_type(qs, p["action_type"])

    if p.get("period"):
        if p["period"] not in PERIOD_DAYS:
            return Response(
                {"detail": "period must be one of: daily, weekly, monthly."}, status=400
            )
        qs = actions_in_period(qs, p["period"])

    dates = {}
    for name in ("from", "until"):
        if p.get(name):
            dates[name] = parse_date(p[name])
            if dates[name] is None:
                return Response(
                    {"detail": f"'{name}' must be a date like 2026-11-24."}, status=400
                )
    qs = actions_between(qs, dates.get("from"), dates.get("until"))

    # how many of each type, for what is left after filtering
    summary = qs.aggregate(
        total=Count("pk"),
        **{
            value: Count("pk", filter=Q(action_type=value))
            for value, _ in CaseAction.ActionType.choices
        },
    )

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    data = CaseActionSerializer(page, many=True).data
    response = paginator.get_paginated_response(data)
    response.data["summary"] = summary
    return response


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def inquiry_timeline(request, ticket_id):
    """
    One ticket's whole story, oldest first (the view an officer opens on a ticket).

    GET  /inquiries/15/actions/
    POST /inquiries/15/actions/   {"notes": "Called the farmer, no answer."}   adds a note
    """
    ticket = get_object_or_404(Inquiry, pk=ticket_id)

    if request.method == "POST":
        serializer = CaseNoteSerializer(
            data={"ticket": ticket.pk, "notes": request.data.get("notes", "")},
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        action = serializer.save()
        return Response(
            CaseActionSerializer(action).data, status=status.HTTP_201_CREATED
        )

    actions = (
        CaseAction.objects.filter(ticket=ticket)
        .select_related("ticket", "user")
        .order_by("timestamp", "pk")
    )
    return Response(
        {
            "ticket": ticket.pk,
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "count": actions.count(),
            "actions": CaseActionSerializer(actions, many=True).data,
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def sms_list(request):
    """
    GET: the SMS log, newest first, with optional filters (all combinable):
      ?status=queued|sent|delivered|failed
      ?ticket=15                      messages tied to one ticket
      ?phone=0712                     partial match on the number
      ?sent_by=4                      the agent who sent it
      ?period=daily|weekly|monthly    today / last 7 days / last 30 days
      ?from=2026-11-01&until=2026-11-24   date range, inclusive
      ?page=2&page_size=50
    The response carries a summary: how many matching messages have each status.

    POST: send a text. Give ONE way to say who gets it:
      {"phone_number": "0712345678", "message": "Your vet visit is booked for Friday."}
      {"user": 9, "message": "Ticket TKT-2026-000015 has been escalated to you."}
      {"ticket": 15, "message": "We are looking into your inquiry."}   (texts the ticket's caller)
    "ticket" can also be sent alongside phone_number or user, to link the message to that ticket.
    Returns 201 with the log entry if the gateway accepted it, 502 (with the entry and the
    reason) if it did not.
    """
    if request.method == "POST":
        serializer = SendSmsSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        sms = serializer.save()
        ok = sms.status == SmsLog.Status.SENT
        return Response(
            SmsLogSerializer(sms).data,
            status=status.HTTP_201_CREATED if ok else status.HTTP_502_BAD_GATEWAY,
        )

    p = request.query_params
    qs = all_sms_logs()

    for name in ("ticket", "sent_by"):
        if p.get(name) and not p[name].isdigit():
            return Response({"detail": f"'{name}' must be a number."}, status=400)
    if p.get("status"):
        valid = [value for value, _ in SmsLog.Status.choices]
        if p["status"] not in valid:
            return Response(
                {"detail": f"status must be one of: {', '.join(valid)}."}, status=400
            )
        qs = sms_by_status(qs, p["status"])
    if p.get("ticket"):
        qs = sms_by_ticket(qs, int(p["ticket"]))
    if p.get("phone"):
        qs = sms_by_phone(qs, p["phone"])
    if p.get("sent_by"):
        qs = sms_by_sender(qs, int(p["sent_by"]))
    if p.get("period"):
        if p["period"] not in PERIOD_DAYS:
            return Response(
                {"detail": "period must be one of: daily, weekly, monthly."}, status=400
            )
        qs = sms_in_period(qs, p["period"])

    dates = {}
    for name in ("from", "until"):
        if p.get(name):
            dates[name] = parse_date(p[name])
            if dates[name] is None:
                return Response(
                    {"detail": f"'{name}' must be a date like 2026-11-24."}, status=400
                )
    qs = sms_between(qs, dates.get("from"), dates.get("until"))

    summary = qs.aggregate(
        total=Count("pk"),
        **{
            value: Count("pk", filter=Q(status=value))
            for value, _ in SmsLog.Status.choices
        },
    )

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    response = paginator.get_paginated_response(SmsLogSerializer(page, many=True).data)
    response.data["summary"] = summary
    return response


@api_view(["POST"])
@permission_classes(
    [AllowAny]
)  # the gateway cannot log in, so a secret in the URL is used
def sms_delivery_webhook(request):
    """
    Delivery reports from the gateway. Give it this URL, with your secret:
        https://your-site/webhooks/sms-delivery/?secret=<SMS_WEBHOOK_SECRET>
    Written for Africa's Talking's report format (id, status, failureReason); check it against
    your provider's current documentation.
    """
    if not hmac.compare_digest(
        request.query_params.get("secret", ""), settings.SMS_WEBHOOK_SECRET
    ):
        return Response(status=403)

    provider_ref = request.data.get("id", "")
    outcome = str(request.data.get("status", "")).lower()
    sms = (
        SmsLog.objects.filter(provider_ref=provider_ref).first()
        if provider_ref
        else None
    )
    if sms is None:
        return Response({"ok": True})  # not ours, or too early; nothing to do

    if outcome == "success":
        sms.status = SmsLog.Status.DELIVERED
    elif outcome in ("failed", "rejected"):
        sms.status = SmsLog.Status.FAILED
        sms.error = str(request.data.get("failureReason", ""))[:255]
    sms.save()  # other states (sent, buffered) change nothing
    return Response({"ok": True})
