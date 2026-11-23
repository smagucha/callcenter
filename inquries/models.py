from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone
from servicedesk.models import AdminUnit, FarmerRef, ProvisionalCaller
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator

phone_validator = RegexValidator(r"^254[17]\d{8}$", "Use the format 2547XXXXXXXX.")

PRIORITY_CHOICES = [
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("critical", "Critical"),
]


# =====================================================================
# commodity, category, sub-category, SLA, routing
# =====================================================================
class Commodity(models.Model):
    class Type(models.TextChoices):
        CROP = "crop", "Crop"
        LIVESTOCK = "livestock", "Livestock"
        FISHERIES = "fisheries", "Fisheries"

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=10, choices=Type.choices)

    class Meta:
        unique_together = ("name", "type")
        verbose_name_plural = "commodities"

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    first_response_hours = models.PositiveIntegerField(default=4)
    sla_hours = models.PositiveIntegerField(default=72)  # resolution target
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class SubCategory(models.Model):
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="sub_categories"
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    sla_hours = models.PositiveIntegerField(
        null=True, blank=True, help_text="Leave empty to use the category's target"
    )
    default_priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default="medium"
    )
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("category", "name")
        verbose_name_plural = "sub-categories"

    def __str__(self):
        return f"{self.category} / {self.name}"


class ServiceLevelTarget(models.Model):
    """Optional override of the category's targets for one priority, e.g. Critical = 2 hours."""

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="priority_slas"
    )
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, blank=True)
    first_response_hours = models.PositiveIntegerField(default=1)
    resolution_hours = models.PositiveIntegerField(default=24)

    class Meta:
        unique_together = ("category", "priority")


class RoutingRule(models.Model):
    """Who receives a ticket at each escalation level for a category and area."""

    class Level(models.IntegerChoices):
        WARD = 0, "Ward"
        SUB_COUNTY = 1, "Sub-County"
        COUNTY = 2, "County"
        NATIONAL = 3, "National"

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="routing_rules"
    )
    level = models.PositiveSmallIntegerField(choices=Level.choices)
    admin_unit = models.ForeignKey(
        AdminUnit,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="routing_rules",
        help_text="The ward / sub-county / county at this level. Empty for National.",
    )
    officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="routing_rules"
    )  # kiamis user replace this line

    class Meta:
        indexes = [models.Index(fields=["category", "level"])]


# =====================================================================
# call
# =====================================================================
class Call(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    call_id = models.CharField(max_length=100, unique=True)  # from the phone system
    phone_number = models.CharField(max_length=12, db_index=True)
    start_time = models.DateTimeField(default=timezone.now, db_index=True)
    end_time = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="calls",
    )
    recording_ref = models.CharField(max_length=255, blank=True)
    direction = models.CharField(
        max_length=8, choices=Direction.choices, default=Direction.INBOUND
    )

    # Set automatically by the signal; both stay empty if the lookup could not run
    farmer = models.ForeignKey(
        FarmerRef,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        # related_name="calls",
    )
    provisional_caller = models.ForeignKey(
        ProvisionalCaller,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="calls",
    )

    class Meta:
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.phone_number} @ {self.start_time:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.end_time and self.duration is None:
            self.duration = int((self.end_time - self.start_time).total_seconds())
        super().save(*args, **kwargs)


# =====================================================================
# inquiry (ticket)
# =====================================================================


class Inquiry(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        ESCALATED = "escalated", "Escalated"
        AWAITING_FARMER = "awaiting_farmer", "Awaiting farmer"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"

    ticket_no = models.CharField(
        max_length=20, unique=True, null=True, blank=True, editable=False
    )
    call = models.ForeignKey(
        Call, null=True, blank=True, on_delete=models.SET_NULL, related_name="inquiries"
    )

    # Exactly one of these (see the constraint below)
    farmer = models.ForeignKey(
        FarmerRef,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inquiries",
    )
    provisional_caller = models.ForeignKey(
        ProvisionalCaller,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inquiries",
    )

    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="inquiries"
    )
    sub_category = models.ForeignKey(
        SubCategory, on_delete=models.PROTECT, related_name="inquiries"
    )
    commodity = models.ForeignKey(
        Commodity,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inquiries",
    )
    description = models.TextField()
    ward = models.ForeignKey(
        AdminUnit,
        on_delete=models.PROTECT,
        related_name="inquiries",
        limit_choices_to={"level": "ward"},
    )  # prefilled from the caller, editable

    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default="medium", db_index=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, db_index=True)
    escalation_level = models.PositiveSmallIntegerField(
        choices=RoutingRule.Level.choices, default=RoutingRule.Level.WARD
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_inquiries",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="created_inquiries",
    )

    # service level tracking
    first_response_due = models.DateTimeField(null=True, blank=True)
    resolution_due = models.DateTimeField(null=True, blank=True, db_index=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    overdue_alerted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "inquiries"
        indexes = [
            models.Index(fields=["ward", "status"]),
            models.Index(fields=["category", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="inquiry_has_exactly_one_caller",
                check=(
                    models.Q(farmer__isnull=False, provisional_caller__isnull=True)
                    | models.Q(farmer__isnull=True, provisional_caller__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return self.ticket_no or f"Inquiry #{self.pk}"

    @property
    def caller_name(self):
        c = self.farmer or self.provisional_caller
        return getattr(c, "full_name", None) or getattr(c, "name", "")

    @property
    def is_overdue(self):
        return (
            self.status not in (self.Status.RESOLVED, self.Status.CLOSED)
            and self.resolution_due is not None
            and timezone.now() > self.resolution_due
        )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.ticket_no:
            self.ticket_no = f"TKT-{self.created_at:%Y}-{self.pk:06d}"
            super().save(update_fields=["ticket_no"])


# =====================================================================
# case_action: append-only audit trail
# =====================================================================
class CaseAction(models.Model):
    class ActionType(models.TextChoices):
        CREATED = "created", "Created"
        STATUS_CHANGED = "status_changed", "Status changed"
        PRIORITY_CHANGED = "priority_changed", "Priority changed"
        ASSIGNED = "assigned", "Assigned"
        ESCALATED = "escalated", "Escalated"
        FIELD_EDITED = "field_edited", "Field edited"
        NOTE = "note", "Note"
        SMS_SENT = "sms_sent", "SMS sent"
        SLA_BREACHED = "sla_breached", "SLA breached"
        REOPENED = "reopened", "Reopened"

    ticket = models.ForeignKey(
        Inquiry, on_delete=models.PROTECT, related_name="actions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )  # null = system
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    notes = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["timestamp"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Audit records cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit records cannot be deleted.")


# =====================================================================
# escalation
# =====================================================================
class Escalation(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"
        OVERDUE = "overdue", "Overdue"

    ticket = models.ForeignKey(
        Inquiry, on_delete=models.CASCADE, related_name="escalations"
    )
    level = models.PositiveSmallIntegerField(choices=RoutingRule.Level.choices)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="escalations",
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField()
    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-assigned_at"]


class SmsLog(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    ticket = models.ForeignKey(
        Inquiry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sms_logs",
    )
    phone_number = models.CharField(max_length=12, validators=[phone_validator])
    message = models.TextField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED
    )
    provider_ref = models.CharField(max_length=100, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)


class Feedback(models.Model):
    ticket = models.OneToOneField(
        Inquiry, on_delete=models.CASCADE, related_name="feedback"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
