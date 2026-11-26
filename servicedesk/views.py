import re
from datetime import timedelta

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.conf import settings
from .caller_serializers import (
    AdminUnitSerializer,
    FarmerPhoneSerializer,
    FarmerRefSerializer,
    ProvisionalCallerSerializer,
    ProvisionalCallerStatusSerializer,
)
from .models import AdminUnit, Call, FarmerPhone, FarmerRef, Inquiry, ProvisionalCaller
from .services import normalise_phone, phone_variants
from .utils import PERIOD_DAYS


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


class AdminUnitPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 2000  # enough to load every ward for a dropdown


def _flag(value):
    return str(value).lower() in ("true", "1", "yes")


def _authorised(request):
    return hmac.compare_digest(
        request.headers.get("x-webhook-secret", ""), settings.CALL_WEBHOOK_SECRET
    )


@api_view(["POST"])
@permission_classes(
    [AllowAny]
)  # the phone system cannot log in, so a shared secret is used
def call_webhook(request):
    """Called by the phone system the moment a call arrives."""
    if not _authorised(request):
        return Response(status=403)
    d = request.data
    number = (
        d.get("callerNumber") or d.get("From") or d.get("caller_number")
    )  # varies by provider
    call_id = d.get("sessionId") or d.get("CallSid") or d.get("call_id")
    if not number or not call_id:
        return Response({"detail": "missing caller or call id"}, status=400)

    # Saving the call fires the signal that matches the caller
    Call.objects.get_or_create(
        call_id=call_id, defaults={"phone_number": normalise_phone(number)}
    )
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
def call_ended_webhook(request):
    """Called when the call finishes: stores end time, duration and the recording."""
    if not _authorised(request):
        return Response(status=403)
    call = Call.objects.filter(
        call_id=request.data.get("call_id") or request.data.get("sessionId")
    ).first()
    if not call:
        return Response(status=404)
    call.end_time = timezone.now()
    call.duration = int((call.end_time - call.start_time).total_seconds())
    call.recording_ref = request.data.get("recording_url", "") or call.recording_ref
    call.save()
    return Response({"ok": True})


def agent_screen(request):
    wards = AdminUnit.objects.filter(level="ward").order_by("name")
    return render(request, "inquiries/incoming_call.html", {"wards": wards})


# =====================================================================
# Admin units (counties, sub-counties, wards): read-only
# =====================================================================
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_unit_list(request):
    """
    ?level=county|sub_county|ward
    ?parent=12                      the units directly under one unit (e.g. a county's sub-counties)
    ?county=Kisumu                  that county and everything inside it
    ?sub_county=Kisumu Central      that sub-county and its wards (send county too: names repeat)
    ?q=kol                          partial match on the name
    ?code=04701                     exact KIAMIS code
    ?page=2&page_size=100
    The response carries a summary: how many counties, sub-counties and wards matched.
    """
    p = request.query_params
    qs = AdminUnit.objects.select_related("parent__parent").order_by("name")

    if p.get("level"):
        if p["level"] not in AdminUnit.Level.values:
            return Response(
                {
                    "detail": f"level must be one of: {', '.join(AdminUnit.Level.values)}."
                },
                status=400,
            )
        qs = qs.filter(level=p["level"])
    if p.get("parent"):
        if not p["parent"].isdigit():
            return Response({"detail": "'parent' must be a number."}, status=400)
        qs = qs.filter(parent_id=int(p["parent"]))
    if p.get("county"):
        c = p["county"]
        qs = qs.filter(
            Q(level="county", name__iexact=c)
            | Q(level="sub_county", parent__name__iexact=c)
            | Q(level="ward", parent__parent__name__iexact=c)
        )
    if p.get("sub_county"):
        s = p["sub_county"]
        qs = qs.filter(
            Q(level="sub_county", name__iexact=s)
            | Q(level="ward", parent__name__iexact=s)
        )
    if p.get("q"):
        qs = qs.filter(name__icontains=p["q"])
    if p.get("code"):
        qs = qs.filter(code=p["code"])

    summary = qs.aggregate(
        **{lv: Count("pk", filter=Q(level=lv)) for lv in AdminUnit.Level.values}
    )
    paginator = AdminUnitPagination()
    page = paginator.paginate_queryset(qs, request)
    response = paginator.get_paginated_response(
        AdminUnitSerializer(page, many=True).data
    )
    response.data["summary"] = summary
    return response


# =====================================================================
# Farmers (KIAMIS, read-only)
# =====================================================================
def _phones_map(farmers):
    """The cached numbers of a page of farmers, in one query."""
    out = {}
    rows = FarmerPhone.objects.using("default").filter(
        farmer_id__in=[f.pk for f in farmers]
    )
    for row in rows:
        out.setdefault(row.farmer_id, []).append(row)
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farmer_list(request):
    """
    Search KIAMIS farmers (read-only):
      ?q=wanjiru                      partial match on the name
      ?phone=0712345678               a full number in any format, or part of one
      ?national_id=12345678
      ?county=Kisumu  ?sub_county=Kisumu Central  ?ward=Kolwa     by name, not case-sensitive
      ?county_code=042  ?subcounty_code=04201  ?ward_code=0721    by KIAMIS code
      ?page=2&page_size=50
    """
    p = request.query_params
    qs = FarmerRef.objects.using("farmers").order_by("full_name")

    if p.get("q"):
        qs = qs.filter(full_name__icontains=p["q"])
    if p.get("phone"):
        digits = normalise_phone(p["phone"])
        if len(digits) == 12:  # a whole number: match every format
            qs = qs.filter(phone__in=phone_variants(digits))
        else:  # part of a number
            qs = qs.filter(phone__icontains=re.sub(r"\D", "", p["phone"]))
    if p.get("national_id"):
        qs = qs.filter(national_id=p["national_id"])
    for param, field in (
        ("county", "county"),
        ("sub_county", "subcounty"),
        ("ward", "ward"),
    ):
        if p.get(param):
            qs = qs.filter(**{f"{field}__iexact": p[param]})
    for param in ("county_code", "subcounty_code", "ward_code"):
        if p.get(param):
            qs = qs.filter(**{param: p[param]})

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    data = FarmerRefSerializer(
        page, many=True, context={"phones_by_farmer": _phones_map(page)}
    ).data
    return paginator.get_paginated_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farmer_detail(request, farmer_id):
    """One farmer, with their cached numbers and how many calls and inquiries they have had."""
    farmer = FarmerRef.objects.using("farmers").filter(pk=farmer_id).first()
    if farmer is None:
        return Response({"detail": "No farmer with that ID in KIAMIS."}, status=404)
    data = dict(FarmerRefSerializer(farmer).data)
    data["call_count"] = Call.objects.filter(farmer_id=farmer_id).count()
    data["inquiry_count"] = Inquiry.objects.filter(farmer_id=farmer_id).count()
    return Response(data)


# =====================================================================
# Farmer phone numbers
# =====================================================================
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def farmer_phone_list(request):
    """
    GET filters: ?farmer=KE123456  ?phone=0712  ?is_primary=true  ?verified=false  ?page=2
    POST adds a number: {"farmer": "KE123456", "phone_number": "0712345678",
                         "is_primary": false, "verified": false}
    The number is cleaned to 2547XXXXXXXX. Making one primary un-marks the farmer's others.
    """
    if request.method == "POST":
        serializer = FarmerPhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            FarmerPhoneSerializer(serializer.save()).data,
            status=status.HTTP_201_CREATED,
        )

    p = request.query_params
    qs = FarmerPhone.objects.using("default").order_by(
        "farmer_id", "-is_primary", "phone_number"
    )
    if p.get("farmer"):
        qs = qs.filter(farmer_id=p["farmer"])
    if p.get("phone"):
        qs = qs.filter(phone_number__icontains=re.sub(r"\D", "", p["phone"]))
    for name in ("is_primary", "verified"):
        if p.get(name):
            qs = qs.filter(**{name: _flag(p[name])})

    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response(FarmerPhoneSerializer(page, many=True).data)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def farmer_phone_detail(request, pk):
    """GET one number. PATCH changes phone_number / is_primary / verified. DELETE removes it."""
    phone = get_object_or_404(FarmerPhone.objects.using("default"), pk=pk)
    if request.method == "DELETE":
        phone.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if request.method == "PATCH":
        serializer = FarmerPhoneSerializer(phone, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        phone = serializer.save()
    return Response(FarmerPhoneSerializer(phone).data)


# =====================================================================
# Provisional callers (callers not yet in KIAMIS)
# =====================================================================
def _provisional_qs():
    return ProvisionalCaller.objects.select_related(
        "county", "sub_county", "ward", "captured_by"
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def provisional_caller_list(request):
    """
    GET filters (all combinable):
      ?status=incomplete|pending|registered|rejected
      ?q=achieng                      partial match on name or phone number
      ?county=Kisumu  ?sub_county=Kisumu Central  ?ward=Kolwa     by name (send county with the others)
      ?enterprise=dairy               partial match on what they farm
      ?captured_by=4                  the agent who captured the details
      ?period=daily|weekly|monthly    when they were first captured: today / 7 days / 30 days
      ?page=2&page_size=50
    The response carries a summary: how many matching callers are in each status.

    POST creates one by hand: {"phone_number": "0712345678", "name": "Jane Achieng",
        "ward": 812, "enterprise": "Dairy", "notes": ""}. Only phone_number is required.
        Giving a ward fills in the sub-county and county.
    """
    if request.method == "POST":
        serializer = ProvisionalCallerSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return Response(
            ProvisionalCallerSerializer(serializer.save()).data,
            status=status.HTTP_201_CREATED,
        )

    p = request.query_params
    qs = _provisional_qs().order_by("-created_at")

    if p.get("status"):
        if p["status"] not in ProvisionalCaller.Status.values:
            return Response(
                {
                    "detail": f"status must be one of: {', '.join(ProvisionalCaller.Status.values)}."
                },
                status=400,
            )
        qs = qs.filter(registration_status=p["status"])
    if p.get("q"):
        qs = qs.filter(
            Q(name__icontains=p["q"])
            | Q(phone_number__icontains=re.sub(r"\D", "", p["q"]) or p["q"])
        )
    for param, field in (
        ("county", "county"),
        ("sub_county", "sub_county"),
        ("ward", "ward"),
    ):
        if p.get(param):
            qs = qs.filter(**{f"{field}__name__iexact": p[param]})
    if p.get("enterprise"):
        qs = qs.filter(enterprise__icontains=p["enterprise"])
    if p.get("captured_by"):
        if not p["captured_by"].isdigit():
            return Response({"detail": "'captured_by' must be a number."}, status=400)
        qs = qs.filter(captured_by_id=int(p["captured_by"]))
    if p.get("period"):
        if p["period"] not in PERIOD_DAYS:
            return Response(
                {"detail": "period must be one of: daily, weekly, monthly."}, status=400
            )
        today = timezone.localdate()
        qs = qs.filter(
            created_at__date__gte=today - timedelta(days=PERIOD_DAYS[p["period"]])
        )

    summary = qs.aggregate(
        total=Count("pk"),
        **{
            v: Count("pk", filter=Q(registration_status=v))
            for v in ProvisionalCaller.Status.values
        },
    )
    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    response = paginator.get_paginated_response(
        ProvisionalCallerSerializer(page, many=True).data
    )
    response.data["summary"] = summary
    return response


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def provisional_caller_detail(request, pk):
    """GET one caller. PATCH fills in or corrects their details (name, ward, enterprise, notes...)."""
    caller = get_object_or_404(_provisional_qs(), pk=pk)
    if request.method == "PATCH":
        serializer = ProvisionalCallerSerializer(
            caller, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        caller = serializer.save()
    return Response(ProvisionalCallerSerializer(caller).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def provisional_caller_status(request, pk):
    """
    Mark a caller Registered, Rejected, or back to Pending:
      {"registration_status": "registered", "kiamis_farmer": "KE123456"}
    Registered needs the KIAMIS farmer ID, which is checked against KIAMIS.
    """
    caller = get_object_or_404(ProvisionalCaller, pk=pk)
    serializer = ProvisionalCallerStatusSerializer(caller, data=request.data)
    serializer.is_valid(raise_exception=True)
    caller = serializer.save()
    return Response(
        ProvisionalCallerSerializer(_provisional_qs().get(pk=caller.pk)).data
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def complete_registration(request, call_id):
    """Agent saves the details of a caller who is not in KIAMIS."""
    try:
        call = Call.objects.select_related("provisional_caller").get(pk=call_id)
    except Call.DoesNotExist:
        return Response(status=404)
    if call.provisional_caller is None:
        return Response({"detail": "This caller is already in KIAMIS."}, status=409)

    ser = ProvisionalSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    v = ser.validated_data
    caller = complete_provisional(
        call.provisional_caller,
        request.user,
        name=v["full_name"],
        ward=v["ward"],
        village=v.get("village", ""),
        national_id=v.get("national_id", ""),
        enterprise=v.get("enterprise", ""),
        notes=v.get("notes", ""),
    )
    return Response({"ok": True, "reference": caller.reference})
