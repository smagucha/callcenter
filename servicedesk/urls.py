from django.urls import path

from . import (
    call_views,
    caller_views,
    case_action_views,
    escalation_views,
    inquiry_views,
    routing_views,
    sms_views,
    views,
)

urlpatterns = [
    path(
        "webhooks/call/", views.call_webhook
    ),  # give these two URLs to the phone provider
    path("webhooks/call-ended/", views.call_ended_webhook),
    path("agent/", views.agent_screen),  # agents keep this page open
    path("admin-units/", caller_views.admin_unit_list),  # counties, sub-counties, wards
    path("farmers/", caller_views.farmer_list),  # KIAMIS farmers (search)
    path("farmers/<str:farmer_id>/", caller_views.farmer_detail),
    path("farmer-phones/", caller_views.farmer_phone_list),  # cached numbers: list, add
    path(
        "farmer-phones/<int:pk>/", caller_views.farmer_phone_detail
    ),  # one number: view, edit, delete
    path(
        "provisional-callers/", caller_views.provisional_caller_list
    ),  # callers not in KIAMIS: list, create
    path("provisional-callers/<int:pk>/", caller_views.provisional_caller_detail),
    path(
        "provisional-callers/<int:pk>/status/", caller_views.provisional_caller_status
    ),
    path("calls/<int:call_id>/register/", agent_api_views.complete_registration),
]
