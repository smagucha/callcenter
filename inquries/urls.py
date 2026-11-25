from django.urls import path
from . import views

urlpatterns = [
    path("inquiries/", views.inquiry_list),
    path("calls/latest/", views.call_list),
    # path("calls/<int:call_id>/register/", views.complete_registration),
    path("escalations/", views.escalation_list),
    #     path("routingrule/", views.routingrule),
    path(
        "case-actions/", views.case_action_list
    ),  # audit trail (filtered); POST adds a note
    path("inquiries/<int:ticket_id>/actions/", views.inquiry_timeline),
    path("sms/", views.sms_list),  # SMS log (GET) and send a text (POST)
    path("webhooks/sms-delivery/", views.sms_delivery_webhook),
]
