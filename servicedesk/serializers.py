from django.db import transaction
from rest_framework import serializers

from .models import (
    AdminUnit,
    FarmerPhone,
    FarmerRef,
    ProvisionalCaller,
    phone_validator,
)
from .services import normalise_phone


def clean_phone(value):
    """Any common format -> 2547XXXXXXXX, or a validation error."""
    phone = normalise_phone(value)
    try:
        phone_validator(phone)
    except Exception:
        raise serializers.ValidationError("Not a valid Kenyan mobile number.")
    return phone


# ---------------------------------------------------------------- AdminUnit
class AdminUnitSerializer(serializers.ModelSerializer):
    """Read-only: counties, sub-counties and wards are synced from KIAMIS, not edited here.
    In views, use .select_related("parent__parent") to avoid one query per row."""

    level_display = serializers.CharField(source="get_level_display", read_only=True)
    parent_name = serializers.CharField(
        source="parent.name", read_only=True, default=None
    )
    county = serializers.SerializerMethodField()
    sub_county = serializers.SerializerMethodField()
    ward = serializers.SerializerMethodField()

    class Meta:
        model = AdminUnit
        fields = [
            "id",
            "name",
            "level",
            "level_display",
            "code",
            "parent",
            "parent_name",
            "county",
            "sub_county",
            "ward",
        ]
        read_only_fields = fields

    def _chain(self, obj):
        names, unit = {}, obj
        while unit is not None:
            names[unit.level] = unit.name
            unit = unit.parent
        return names

    def get_county(self, obj):
        return self._chain(obj).get("county")

    def get_sub_county(self, obj):
        return self._chain(obj).get("sub_county")

    def get_ward(self, obj):
        return self._chain(obj).get("ward")


# ------------------------------------------------------------- FarmerPhone
class FarmerPhoneSerializer(serializers.ModelSerializer):
    # FarmerRef lives in the KIAMIS database, so look it up there, not in the default one
    farmer = serializers.PrimaryKeyRelatedField(
        queryset=FarmerRef.objects.using("farmers").all()
    )

    class Meta:
        model = FarmerPhone
        fields = ["id", "farmer", "phone_number", "is_primary", "verified"]
        read_only_fields = ["id"]

    def validate_phone_number(self, value):
        return clean_phone(value)

    def _make_only_primary(self, phone):
        # a farmer has one primary number: setting this one clears the others
        if phone.is_primary:
            (
                FarmerPhone.objects.using("default")
                .filter(farmer_id=phone.farmer_id)
                .exclude(pk=phone.pk)
                .update(is_primary=False)
            )

    @transaction.atomic
    def create(self, validated_data):
        # The farmer object was read from the KIAMIS database. Store only its ID, and always
        # save the phone in our own database, never in KIAMIS.
        farmer = validated_data.pop("farmer")
        phone = FarmerPhone.objects.using("default").create(
            farmer_id=farmer.pk, **validated_data
        )
        self._make_only_primary(phone)
        return phone

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop("farmer", None)  # a number cannot be moved to another farmer
        phone = super().update(instance, validated_data)
        self._make_only_primary(phone)
        return phone


# --------------------------------------------------------------- FarmerRef
class FarmerRefSerializer(serializers.ModelSerializer):
    """Read-only: KIAMIS is the source of truth for farmers."""

    phones = serializers.SerializerMethodField()  # numbers we have cached

    class Meta:
        model = FarmerRef
        fields = [
            "farmer_id",
            "national_id",
            "full_name",
            "gender",
            "phone",
            "county",
            "subcounty",
            "ward",
            "county_code",
            "subcounty_code",
            "ward_code",
            "phones",
        ]
        read_only_fields = fields

    def get_phones(self, obj):
        # Farmers come from the KIAMIS database but their cached numbers are in ours, so
        # query ours explicitly. Views pass a ready-made map to avoid one query per farmer.
        by_farmer = self.context.get("phones_by_farmer")
        rows = (
            by_farmer.get(obj.pk, [])
            if by_farmer is not None
            else FarmerPhone.objects.using("default").filter(farmer_id=obj.pk)
        )
        return FarmerPhoneSerializer(rows, many=True).data


# -------------------------------------------------------- ProvisionalCaller
class ProvisionalCallerSerializer(serializers.ModelSerializer):
    # we normalise the number first, so uniqueness is checked on the cleaned value below
    phone_number = serializers.CharField(max_length=20)
    county = serializers.PrimaryKeyRelatedField(
        queryset=AdminUnit.objects.filter(level="county"),
        required=False,
        allow_null=True,
    )
    sub_county = serializers.PrimaryKeyRelatedField(
        queryset=AdminUnit.objects.filter(level="sub_county"),
        required=False,
        allow_null=True,
    )
    ward = serializers.PrimaryKeyRelatedField(
        queryset=AdminUnit.objects.filter(level="ward"), required=False, allow_null=True
    )

    county_name = serializers.CharField(
        source="county.name", read_only=True, default=None
    )
    sub_county_name = serializers.CharField(
        source="sub_county.name", read_only=True, default=None
    )
    ward_name = serializers.CharField(source="ward.name", read_only=True, default=None)
    status_display = serializers.CharField(
        source="get_registration_status_display", read_only=True
    )
    captured_by_name = serializers.CharField(
        source="captured_by.name", read_only=True, default=None
    )
    reference = serializers.CharField(read_only=True)  # PENDING-12, or the KIAMIS ID

    class Meta:
        model = ProvisionalCaller
        fields = [
            "id",
            "reference",
            "phone_number",
            "name",
            "national_id",
            "county",
            "county_name",
            "sub_county",
            "sub_county_name",
            "ward",
            "ward_name",
            "enterprise",
            "notes",
            "registration_status",
            "status_display",
            "kiamis_farmer",
            "captured_by",
            "captured_by_name",
            "created_at",
        ]
        # Status and the KIAMIS link change through ProvisionalCallerStatusSerializer
        read_only_fields = [
            "id",
            "registration_status",
            "kiamis_farmer",
            "captured_by",
            "created_at",
        ]

    def validate_phone_number(self, value):
        phone = clean_phone(value)
        clash = ProvisionalCaller.objects.filter(phone_number=phone)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                "This number is already captured as a provisional caller."
            )
        return phone

    def validate(self, attrs):
        get = lambda f: attrs[f] if f in attrs else getattr(self.instance, f, None)
        county, sub_county, ward = get("county"), get("sub_county"), get("ward")

        # the three must agree: the ward sits in the sub-county, which sits in the county
        if ward:
            if sub_county and sub_county.pk != ward.parent_id:
                raise serializers.ValidationError(
                    {"sub_county": "This ward is not in that sub-county."}
                )
            sub_county = ward.parent
            attrs["sub_county"] = sub_county  # filled in from the ward
        if sub_county:
            if county and county.pk != sub_county.parent_id:
                raise serializers.ValidationError(
                    {"county": "This sub-county is not in that county."}
                )
            attrs["county"] = sub_county.parent  # filled in from the sub-county
        return attrs

    def _complete(self, instance):
        """Number captured -> details captured, as soon as a name and ward are known."""
        if (
            instance.registration_status == ProvisionalCaller.Status.INCOMPLETE
            and instance.name
            and instance.ward_id
        ):
            instance.registration_status = ProvisionalCaller.Status.PENDING

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            validated_data["captured_by"] = request.user
        instance = ProvisionalCaller(**validated_data)
        self._complete(instance)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        self._complete(instance)
        request = self.context.get("request")
        if request and request.user.is_authenticated and not instance.captured_by_id:
            instance.captured_by = request.user
        instance.save()
        return instance


class ProvisionalCallerStatusSerializer(serializers.Serializer):
    """For staff to mark a caller Registered (with the KIAMIS ID), Rejected, or back to Pending."""

    registration_status = serializers.ChoiceField(
        choices=ProvisionalCaller.Status.choices
    )
    kiamis_farmer = serializers.CharField(
        required=False, allow_blank=True
    )  # KIAMIS farmer ID

    def validate(self, attrs):
        status = attrs["registration_status"]
        farmer_id = attrs.get("kiamis_farmer", "")
        if status == ProvisionalCaller.Status.REGISTERED:
            if not farmer_id:
                raise serializers.ValidationError(
                    {
                        "kiamis_farmer": "Give the KIAMIS farmer ID when marking a caller as registered."
                    }
                )
            if not FarmerRef.objects.using("farmers").filter(pk=farmer_id).exists():
                raise serializers.ValidationError(
                    {"kiamis_farmer": "No farmer with that ID in KIAMIS."}
                )
        return attrs

    def update(self, instance, validated_data):
        instance.registration_status = validated_data["registration_status"]
        if instance.registration_status == ProvisionalCaller.Status.REGISTERED:
            instance.kiamis_farmer_id = validated_data["kiamis_farmer"]
        instance.save()
        return instance
