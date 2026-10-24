import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger(__name__)


def send_mail(to, subject, body, html=None):
    """SMTP if EMAIL_HOST is set, else Amazon SES (the Lambda's own permission, no password), else just logged."""
    if not settings.email_host:
        if settings.ses_from:
            import boto3

            content = {"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body},
                                                                       **({"Html": {"Data": html}} if html else {})}}}
            boto3.client("sesv2", region_name=settings.ses_region).send_email(
                FromEmailAddress=settings.ses_from, Destination={"ToAddresses": [to]}, Content=content)
            return
        log.warning("No email set up; email to %s not sent:\n%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = settings.default_from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.email_host, settings.email_port, timeout=20) as smtp:
        if settings.email_use_tls:
            smtp.starttls()
        if settings.email_user:
            smtp.login(settings.email_user, settings.email_password)
        smtp.send_message(msg)
