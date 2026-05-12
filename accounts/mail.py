from django.conf import settings
from django.core.mail import send_mail


def send_system_email(subject, body, recipients):
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
        fail_silently=False,
    )
