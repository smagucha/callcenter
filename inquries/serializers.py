import uuid
from django.db import transaction
from rest_framework import serializers
from .models import (
    Call,
    Inquiry,
    RoutingRule,
    phone_validator,
    Escalation,
    CaseAction,
    SmsLog,
)
from .services import ALLOWED, log, normalise_phone  # apply_sla,
from django.contrib.auth import get_user_model


class InquirySerializer(serializers.ModelSerializer):
    # --- friendly read-only extras
    caller_type = serializers.SerializerMethodField()
    caller_name = serializers.CharField(read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    sub_category_name = serializers.CharField(
        source="sub_category.name", read_only=True
    )
    sub_sub_category_name = serializers.CharField(
        source="sub_subcatergory.name", read_only=True
    )
    commodity_name = serializers.CharField(
        source="commodity.name", read_only=True, default=None
    )
    assigned_to_name = serializers.CharField(
        source="assigned_to.name", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    is_overdue = serializers.BooleanField(read_only=True)

    # The API calls it sub_sub_category; the model field is currently misspelled
    # "sub_subcatergory". If you rename the model field, change only `source` here.
    sub_sub_category = serializers.PrimaryKeyRelatedField(
        source="sub_subcatergory",
        queryset=Inquiry._meta.get_field(
            "sub_subcatergory"
        ).remote_field.model.objects.all(),
    )

    class Meta:
        model = Inquiry
        fields = [
            "id",
            "ticket_no",
            "call",
            "farmer",
            "provisional_caller",
            "caller_type",
            "caller_name",
            "category",
            "category_name",
            "sub_category",
            "sub_category_name",
            "sub_sub_category",
            "sub_sub_category_name",
            "commodity",
            "commodity_name",
            "description",
            "priority",
            "priority_display",
            "status",
            "status_display",
            "escalation_level",
            "assigned_to",
            "assigned_to_name",
            "created_by",
            "first_response_due",
            "resolution_due",
            "first_response_at",
            "overdue_alerted_at",
            "is_overdue",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
        ]
        # Set by the system, never by the client. Status changes go through
        # InquiryStatusSerializer so the allowed transitions and audit log apply.
        read_only_fields = [
            "id",
            "ticket_no",
            "status",
            "escalation_level",
            "created_by",
            "first_response_due",
            "resolution_due",
            "first_response_at",
            "overdue_alerted_at",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
        ]
        extra_kwargs = {
            "priority": {"required": False}
        }  # defaults to the sub-category's priority

    def get_caller_type(self, obj):
        return "farmer" if obj.farmer_id else "provisional"

    # ------------------------------------------------------------- validation
    def validate(self, attrs):
        inst = self.instance

        # exactly one of farmer / provisional_caller (matches the database constraint)
        farmer = attrs["farmer"] if "farmer" in attrs else getattr(inst, "farmer", None)
        prov = (
            attrs["provisional_caller"]
            if "provisional_caller" in attrs
            else getattr(inst, "provisional_caller", None)
        )
        if bool(farmer) == bool(prov):
            raise serializers.ValidationError(
                "Provide exactly one of 'farmer' or 'provisional_caller'."
            )

        # the classification must be consistent: category > sub-category > sub-sub-category
        category = attrs.get("category") or getattr(inst, "category", None)
        sub = attrs.get("sub_category") or getattr(inst, "sub_category", None)
        sub_sub = attrs.get("sub_subcatergory") or getattr(
            inst, "sub_subcatergory", None
        )
        if sub and category and sub.category_id != category.id:
            raise serializers.ValidationError(
                {
                    "sub_category": "This sub-category does not belong to the chosen category."
                }
            )
        # assumes SubSubCategory has a ForeignKey called sub_category; adjust if yours differs
        if sub_sub and sub and getattr(sub_sub, "sub_category_id", sub.id) != sub.id:
            raise serializers.ValidationError(
                {
                    "sub_sub_category": "This sub-sub-category does not belong to the chosen sub-category."
                }
            )
        return attrs

    # ----------------------------------------------------------------- create
    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user if request and request.user.is_authenticated else None

        validated_data[
            "status"
        ] = Inquiry.Status.IN_PROGRESS  # there is no "New" status
        validated_data["escalation_level"] = RoutingRule.Level.WARD
        validated_data["created_by"] = user
        validated_data.setdefault(
            "priority", validated_data["sub_category"].default_priority
        )

        ticket = super().create(validated_data)
        apply_sla(ticket)  # fills first_response_due / resolution_due
        ticket.save(update_fields=["first_response_due", "resolution_due"])
        log(ticket, "created", user, to_value=ticket.priority)
        return ticket

    # ----------------------------------------------------------------- update
    @transaction.atomic
    def update(self, instance, validated_data):
        user = self.context["request"].user
        old_priority, old_description = instance.priority, instance.description
        ticket = super().update(instance, validated_data)

        if ticket.priority != old_priority:
            apply_sla(ticket)  # targets depend on priority
            ticket.save(update_fields=["first_response_due", "resolution_due"])
            log(ticket, "priority_changed", user, old_priority, ticket.priority)
        if ticket.description != old_description:
            log(ticket, "field_edited", user, notes="Description edited")
        return ticket


class InquiryStatusSerializer(serializers.Serializer):
    """Use for status changes: PATCH /inquiries/<id>/status/ {"status": "resolved", "notes": "..."}"""

    status = serializers.ChoiceField(choices=Inquiry.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_status(self, value):
        ticket = self.context["ticket"]
        if value not in ALLOWED[ticket.status]:
            raise serializers.ValidationError(
                f"Cannot move from '{ticket.status}' to '{value}'."
            )
        return value

    # then in the view:  change_status(ticket, v["status"], request.user, v.get("notes", ""))


class CallSerializer(serializers.ModelSerializer):
    caller_type = serializers.SerializerMethodField()
    caller_name = serializers.SerializerMethodField()
    county = serializers.SerializerMethodField()
    sub_county = serializers.SerializerMethodField()
    ward = serializers.SerializerMethodField()
    agent_name = serializers.CharField(
        source="agent.name", read_only=True, default=None
    )
    direction_display = serializers.CharField(
        source="get_direction_display", read_only=True
    )
    inquiry_count = serializers.IntegerField(
        read_only=True, default=0
    )  # annotated by the view

    class Meta:
        model = Call
        fields = [
            "id",
            "call_id",
            "phone_number",
            "direction",
            "direction_display",
            "start_time",
            "end_time",
            "duration",
            "recording_ref",
            "agent",
            "agent_name",
            "farmer",
            "provisional_caller",
            "caller_type",
            "caller_name",
            "county",
            "sub_county",
            "ward",
            "inquiry_count",
        ]
        # The phone system and the caller lookup fill these in, never the client
        read_only_fields = [
            "id",
            "call_id",
            "phone_number",
            "start_time",
            "duration",
            "farmer",
            "provisional_caller",
        ]

    # the caller is either a KIAMIS farmer or a provisional caller (or neither if the lookup failed)
    def _caller(self, obj):
        return obj.farmer or obj.provisional_caller

    def get_caller_type(self, obj):
        if obj.farmer_id:
            return "farmer"
        return "provisional" if obj.provisional_caller_id else None

    def get_caller_name(self, obj):
        c = self._caller(obj)
        return (
            (getattr(c, "full_name", None) or getattr(c, "name", None)) if c else None
        )

    def _unit_name(self, obj, field):
        c = self._caller(obj)
        unit = getattr(c, field, None) if c else None
        return unit.name if unit else None

    def get_county(self, obj):
        return self._unit_name(obj, "county")

    def get_sub_county(self, obj):
        return self._unit_name(obj, "sub_county")

    def get_ward(self, obj):
        return self._unit_name(obj, "ward")


class CallCreateSerializer(serializers.ModelSerializer):
    """Logs a call by hand (e.g. an outbound call, or one the phone system missed).
    The farmer / provisional caller is matched automatically after saving."""

    phone_number = serializers.CharField(max_length=20)  # any format; normalised below

    class Meta:
        model = Call
        fields = [
            "call_id",
            "phone_number",
            "direction",
            "start_time",
            "end_time",
            "recording_ref",
        ]
        extra_kwargs = {"call_id": {"required": False}}  # generated if left out

    def validate_phone_number(self, value):
        phone = normalise_phone(value)  # 0712..., +254712... -> 254712...
        phone_validator(phone)  # raises if not 2547XXXXXXXX / 2541XXXXXXXX
        return phone

    def validate(self, attrs):
        start, end = attrs.get("start_time"), attrs.get("end_time")
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_time": "End time cannot be before the start time."}
            )
        return attrs

    def create(self, validated_data):
        validated_data.setdefault("call_id", f"manual-{uuid.uuid4().hex[:12]}")
        validated_data["agent"] = self.context["request"].user  # the agent logging it
        return super().create(validated_data)  # saving fires the caller-matching signal


class EscalationSerializer(serializers.ModelSerializer):
    ticket_no = serializers.CharField(source="ticket.ticket_no", read_only=True)
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assigned_to_name = serializers.CharField(
        source="assigned_to.name", read_only=True, default=None
    )
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = Escalation
        fields = [
            "id",
            "ticket",
            "ticket_no",
            "level",
            "level_display",
            "assigned_to",
            "assigned_to_name",
            "assigned_at",
            "due_at",
            "status",
            "status_display",
            "is_overdue",
            "reason",
        ]
        read_only_fields = [
            "id",
            "assigned_at",
            "status",
        ]  # a new escalation always starts Open
        extra_kwargs = {
            "level": {
                "required": False
            },  # defaults to one level above the ticket's current level
            "due_at": {"required": False},  # defaults from the ticket's priority
        }

    def get_is_overdue(self, obj):
        # true as soon as the due time passes, even before the 15-minute job marks it Overdue
        return (
            obj.status
            in (
                Escalation.Status.OPEN,
                Escalation.Status.ACKNOWLEDGED,
                Escalation.Status.OVERDUE,
            )
            and obj.due_at < timezone.now()
        )

    def validate(self, attrs):
        ticket = attrs["ticket"]
        if ticket.status in (Inquiry.Status.RESOLVED, Inquiry.Status.CLOSED):
            raise serializers.ValidationError(
                {
                    "ticket": "A resolved or closed ticket cannot be escalated. Reopen it first."
                }
            )
        level = attrs.get("level")
        if level is not None and level <= ticket.escalation_level:
            raise serializers.ValidationError(
                {"level": "Must be higher than the ticket's current escalation level."}
            )
        return attrs

    @staticmethod
    def _route(ticket, level):
        try:
            return resolve_officer(
                ticket, level
            )  # needs the ticket's ward and a routing rule
        except AttributeError:
            return None

    @transaction.atomic
    def create(self, validated_data):
        user = self.context["request"].user
        ticket = validated_data["ticket"]
        level = validated_data.get(
            "level", min(ticket.escalation_level + 1, RoutingRule.Level.NATIONAL)
        )
        officer = validated_data.get("assigned_to") or self._route(ticket, level)
        if officer is None:
            raise serializers.ValidationError(
                {"assigned_to": "No officer found for this level. Choose one."}
            )
        reason = validated_data.get("reason", "")
        due_at = validated_data.get("due_at") or (
            timezone.now() + timedelta(hours=ESCALATION_HOURS[ticket.priority])
        )

        # the new escalation replaces any that are still open on this ticket
        ticket.escalations.filter(
            status__in=["open", "acknowledged", "overdue"]
        ).update(status=Escalation.Status.RESOLVED)
        escalation = Escalation.objects.create(
            ticket=ticket,
            level=level,
            assigned_to=officer,
            due_at=due_at,
            reason=reason,
        )

        previous_officer = ticket.assigned_to_id
        ticket.escalation_level = level
        ticket.status = Inquiry.Status.ESCALATED
        ticket.assigned_to = officer
        ticket.save()
        log(
            ticket,
            "escalated",
            user,
            previous_officer or "",
            officer.pk,
            f"Level {level}. {reason}",
        )
        return escalation


class RoutingRuleSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    admin_unit_name = serializers.CharField(
        source="admin_unit.name", read_only=True, default=None
    )
    # the whole location of the rule, worked out from the admin unit's parents
    ward = serializers.SerializerMethodField()
    sub_county = serializers.SerializerMethodField()
    county = serializers.SerializerMethodField()

    class Meta:
        model = RoutingRule
        fields = [
            "id",
            "category",
            "category_name",
            "level",
            "level_display",
            "admin_unit",
            "admin_unit_name",
            "ward",
            "sub_county",
            "county",
            "officer",
            "officer_email",
            "officer_phone",
        ]
        read_only_fields = ["id"]

    # ward / sub-county / county names for whichever level the rule sits at
    def _chain(self, obj):
        unit, chain = obj.admin_unit, {}
        while unit is not None:
            chain[unit.level] = unit.name
            unit = unit.parent
        return chain

    def get_ward(self, obj):
        return self._chain(obj).get("ward")

    def get_sub_county(self, obj):
        return self._chain(obj).get("sub_county")

    def get_county(self, obj):
        return self._chain(obj).get("county")

    def validate_officer_phone(self, value):
        if not value:
            return value
        phone = normalise_phone(value)  # 0712..., +254712... -> 254712...
        phone_validator(phone)
        return phone

    def validate(self, attrs):
        get = lambda f: attrs[f] if f in attrs else getattr(self.instance, f, None)
        category, level, unit = get("category"), get("level"), get("admin_unit")

        # the area must be at the same level as the rule
        needed = {
            RoutingRule.Level.WARD: "ward",
            RoutingRule.Level.SUB_COUNTY: "sub_county",
            RoutingRule.Level.COUNTY: "county",
        }.get(level)
        if level == RoutingRule.Level.NATIONAL:
            if unit is not None:
                raise serializers.ValidationError(
                    {"admin_unit": "National rules have no area. Leave it empty."}
                )
        elif unit is None or unit.level != needed:
            raise serializers.ValidationError(
                {
                    "admin_unit": f"A {RoutingRule.Level(level).label} rule needs a {needed.replace('_', '-')} as its area."
                }
            )

        # only one officer per category, level and area, otherwise routing would have to guess
        clash = RoutingRule.objects.filter(
            category=category, level=level, admin_unit=unit
        )
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                "A rule already exists for this category, level and area."
            )
        return attrs


class CaseActionSerializer(serializers.ModelSerializer):
    """Read-only: the audit trail is written by the system, never edited through the API."""

    ticket_no = serializers.CharField(source="ticket.ticket_no", read_only=True)
    user_name = serializers.SerializerMethodField()
    action_type_display = serializers.CharField(
        source="get_action_type_display", read_only=True
    )

    class Meta:
        model = CaseAction
        fields = [
            "id",
            "ticket",
            "ticket_no",
            "user",
            "user_name",
            "action_type",
            "action_type_display",
            "from_value",
            "to_value",
            "notes",
            "timestamp",
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        return (
            obj.user.name if obj.user_id else "System"
        )  # no user = done automatically


class CaseNoteSerializer(serializers.Serializer):
    """The only thing a client may add to the audit trail: a note on a ticket."""

    ticket = serializers.PrimaryKeyRelatedField(queryset=Inquiry.objects.all())
    notes = serializers.CharField(max_length=2000)

    def create(self, validated_data):
        return log(
            validated_data["ticket"],
            "note",
            self.context["request"].user,
            notes=validated_data["notes"],
        )


class SmsLogSerializer(serializers.ModelSerializer):
    ticket_no = serializers.CharField(
        source="ticket.ticket_no", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    sent_by_name = serializers.CharField(
        source="sent_by.name", read_only=True, default=None
    )

    class Meta:
        model = SmsLog
        fields = [
            "id",
            "ticket",
            "ticket_no",
            "phone_number",
            "message",
            "status",
            "status_display",
            "provider_ref",
            "error",
            "sent_by",
            "sent_by_name",
            "created_at",
            "sent_at",
        ]
        read_only_fields = fields


class SendSmsSerializer(serializers.Serializer):
    """Who gets the text: phone_number, else the user's phone, else the ticket's caller."""

    message = serializers.CharField(max_length=459)  # 3 linked SMS segments
    phone_number = serializers.CharField(max_length=20, required=False)
    user = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.filter(is_active=True), required=False
    )
    ticket = serializers.PrimaryKeyRelatedField(
        queryset=Inquiry.objects.all(), required=False
    )

    def validate(self, attrs):
        if attrs.get("phone_number"):
            phone = normalise_phone(attrs["phone_number"])
        elif attrs.get("user"):
            phone = (
                normalise_phone(attrs["user"].phone) if attrs["user"].phone else None
            )
            if not phone:
                raise serializers.ValidationError(
                    {"user": "This user has no phone number on file."}
                )
        elif attrs.get("ticket"):
            phone = ticket_caller_phone(attrs["ticket"])
            if not phone:
                raise serializers.ValidationError(
                    {"ticket": "No phone number found for this ticket's caller."}
                )
        else:
            raise serializers.ValidationError(
                "Send a phone_number, a user, or a ticket to message its caller."
            )
        try:
            phone_validator(phone)  # 2547XXXXXXXX / 2541XXXXXXXX
        except Exception:
            raise serializers.ValidationError(
                {"phone_number": "Not a valid Kenyan mobile number."}
            )
        attrs["phone"] = phone
        return attrs

    def create(self, validated_data):
        return send_sms_message(
            phone=validated_data["phone"],
            message=validated_data["message"],
            user=self.context["request"].user,
            ticket=validated_data.get("ticket"),
        )
