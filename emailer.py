import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("FeeComparisonScraper")


def send_email(subject, html_body, config):
    """Sends the rendered report as an HTML email.

    Reads SMTP transport details from environment variables (set as GHA
    secrets in production); falls back to config.yaml's settings.email
    for the From/To addresses when the corresponding env var isn't set.
    Raises on failure so the caller can decide how to handle it.
    """
    email_cfg = config.get("settings", {}).get("email", {})

    host = os.environ.get("EMAIL_HOST")
    port = int(os.environ.get("EMAIL_PORT", "587"))
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")

    sender = user or email_cfg.get("sender")
    recipients_raw = os.environ.get("EMAIL_TO") or email_cfg.get("receiver", "")
    recipients = [addr.strip() for addr in recipients_raw.split(",") if addr.strip()]

    if not host or not user or not password or not recipients:
        raise RuntimeError(
            "Missing email configuration: EMAIL_HOST, EMAIL_USER, EMAIL_PASS, "
            "and at least one recipient (EMAIL_TO or settings.email.receiver) are required."
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText("This report requires an HTML-capable email client to view.", "plain"))
    message.attach(MIMEText(html_body, "html"))

    logger.info(f"Sending email to {recipients} via {host}:{port}")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(user, password)
            server.sendmail(sender, recipients, message.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, recipients, message.as_string())

    logger.info("Email sent successfully.")
