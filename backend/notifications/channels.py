"""External delivery channels: email (AWS SES) and SMS (AWS SNS).

Both are best-effort and config-gated. When a channel is disabled or AWS is not
configured, the channel logs what it *would* send and returns False — so the app
runs and tests pass with no AWS credentials. boto3 is imported lazily and
optionally; its absence simply disables real delivery.
"""

from __future__ import annotations

import logging

from backend import config

logger = logging.getLogger(__name__)

try:  # boto3 is optional in dev/test environments.
    import boto3  # type: ignore
except Exception:  # pragma: no cover - exercised only where boto3 is absent
    boto3 = None  # type: ignore

_ses_client = None
_sns_client = None


def _ses():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses", region_name=config.AWS_REGION)
    return _ses_client


def _sns():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns", region_name=config.AWS_REGION)
    return _sns_client


def email_available() -> bool:
    return bool(
        config.NOTIFICATIONS_EMAIL_ENABLED
        and boto3
        and config.AWS_REGION
        and config.SES_SENDER_EMAIL
    )


def sms_available() -> bool:
    return bool(config.NOTIFICATIONS_SMS_ENABLED and boto3 and config.AWS_REGION)


def send_email(*, to: str, subject: str, body: str) -> bool:
    """Send an email via SES. Returns True only on a real, successful send."""
    if not email_available():
        logger.info(
            "[notify:email:log] to=%s subject=%r (delivery disabled)", to, subject
        )
        return False
    try:
        _ses().send_email(
            Source=config.SES_SENDER_EMAIL,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        return True
    except Exception:  # pragma: no cover - network/credentials failure path
        logger.exception("[notify:email] send failed to=%s", to)
        return False


def send_sms(*, to: str, body: str) -> bool:
    """Send an SMS via SNS. Returns True only on a real, successful send."""
    if not sms_available():
        logger.info("[notify:sms:log] to=%s body=%r (delivery disabled)", to, body)
        return False
    try:
        _sns().publish(PhoneNumber=to, Message=body)
        return True
    except Exception:  # pragma: no cover - network/credentials failure path
        logger.exception("[notify:sms] send failed to=%s", to)
        return False
