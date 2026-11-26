import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import AdminUnit, FarmerPhone, FarmerRef, ProvisionalCaller


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
            KiamisFarmer.objects.using("farmers")
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
