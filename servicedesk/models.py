from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.conf import settings
from django.utils import timezone

phone_validator = RegexValidator(r"^254[17]\d{8}$", "Use the format 2547XXXXXXXX.")


class AdminUnit(models.Model):
    class Level(models.TextChoices):
        COUNTY = "county", "County"
        SUB_COUNTY = "sub_county", "Sub-County"
        WARD = "ward", "Ward"

    name = models.CharField(max_length=100)
    level = models.CharField(max_length=12, choices=Level.choices, db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    code = models.CharField(
        max_length=20, blank=True, null=True, unique=True
    )  # KIAMIS code, used for syncing

    class Meta:
        ordering = ["name"]
        managed = False
        db_table = "AdminUnit"

    def __str__(self):
        return f"{self.name} ({self.get_level_display()})"


# =====================================================================
# KIAMIS tables (read-only, other database). Never migrated. Adjust names to the real schema.
# =====================================================================
class FarmerRef(models.Model):
    farmer_id = models.CharField(max_length=64, primary_key=True)
    national_id = models.CharField(max_length=20, blank=True, null=True)
    full_name = models.CharField(max_length=150)
    gender = models.CharField(max_length=10, blank=True, null=True)
    phone = models.CharField(max_length=20)
    county_code = models.CharField(max_length=20, blank=True, null=True)
    subcounty_code = models.CharField(max_length=20, blank=True, null=True)
    ward_code = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "farmers"


class FarmerPhone(models.Model):
    farmer = models.ForeignKey(
        FarmerRef, on_delete=models.CASCADE, related_name="phones"
    )
    phone_number = models.CharField(
        max_length=12, validators=[phone_validator], db_index=True
    )
    is_primary = models.BooleanField(default=False)
    verified = models.BooleanField(default=False)

    class Meta:
        unique_together = ("farmer", "phone_number")


class ProvisionalCaller(models.Model):
    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Number captured, details needed"
        PENDING = "pending", "Details captured, awaiting KIAMIS"
        REGISTERED = "registered", "Registered in KIAMIS"
        REJECTED = "rejected", "Rejected"

    phone_number = models.CharField(
        max_length=12, unique=True, validators=[phone_validator]
    )
    name = models.CharField(max_length=150, blank=True)
    national_id = models.CharField(max_length=20, blank=True)
    county = models.ForeignKey(
        AdminUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sub_county = models.ForeignKey(
        AdminUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ward = models.ForeignKey(
        AdminUnit, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    enterprise = models.CharField(max_length=150, blank=True)  # what they farm
    notes = models.TextField(blank=True)
    registration_status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.INCOMPLETE, db_index=True
    )
    kiamis_farmer = models.ForeignKey(
        FarmerRef, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )  # filled once registered
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def reference(self):
        return self.kiamis_farmer_id or f"PENDING-{self.pk}"
