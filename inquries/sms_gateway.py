"""
SMS gateway. Written for Africa's Talking (pip install requests). To use another provider,
rewrite send() only: it must return (ok, provider_ref, error).

settings.py:
    SMS_USERNAME = "sandbox"            # your Africa's Talking username
    SMS_API_KEY = "..."                 # from the environment, not the code
    SMS_SENDER_ID = ""                  # optional approved sender name
    SMS_SANDBOX = True                  # False once you go live
    SMS_WEBHOOK_SECRET = "long-random-string"   # protects the delivery-report URL
"""
import requests
from django.conf import settings

SUCCESS_CODES = (100, 101, 102)  # processed / sent / queued


def send(phone, message):
    """phone is 2547XXXXXXXX. Returns (ok, provider_ref, error)."""
    base = (
        "https://api.sandbox.africastalking.com"
        if getattr(settings, "SMS_SANDBOX", True)
        else "https://api.africastalking.com"
    )
    data = {"username": settings.SMS_USERNAME, "to": f"+{phone}", "message": message}
    if getattr(settings, "SMS_SENDER_ID", ""):
        data["from"] = settings.SMS_SENDER_ID
    try:
        r = requests.post(
            f"{base}/version1/messaging",
            data=data,
            timeout=10,
            headers={"apiKey": settings.SMS_API_KEY, "Accept": "application/json"},
        )
        r.raise_for_status()
        recipients = r.json()["SMSMessageData"]["Recipients"]
    except Exception as exc:  # network, bad key, bad response
        return False, "", f"Gateway error: {exc}"[:255]
    if not recipients:
        return False, "", "The gateway accepted no recipients."
    first = recipients[0]
    if first.get("statusCode") in SUCCESS_CODES:
        return True, first.get("messageId", ""), ""
    return False, "", str(first.get("status", "Rejected"))[:255]
