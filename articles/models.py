from django.db import models
from inquries.models import Category
from django.conf import settings


class FaqArticle(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        ARCHIVED = "archived", "Archived"

    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="faqs"
    )
    title = models.CharField(max_length=200)
    content_en = models.TextField()
    content_sw = models.TextField(blank=True)  # Kiswahili
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_faqs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title
