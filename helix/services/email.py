"""Transactional email via Resend."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"


def _uid(prefix: str = "em_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def send_email(
    db: Session | None,
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    template: str = "",
) -> dict[str, Any]:
    """Send email through Resend. Logs to email_log when db is provided."""
    settings = get_settings()
    log_id = _uid()
    status = "queued"
    provider_id = None
    error = None

    if not settings.resend_api_key:
        status = "skipped"
        error = "RESEND_API_KEY not configured — email not sent"
        logger.warning("%s (to=%s subject=%s)", error, to, subject)
        # In local dev without Resend, still succeed so flows are testable
        result = {"ok": False, "skipped": True, "error": error, "log_id": log_id}
    else:
        payload: dict[str, Any] = {
            "from": settings.resend_from_email,
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        if settings.resend_reply_to:
            payload["reply_to"] = settings.resend_reply_to

        try:
            resp = httpx.post(
                RESEND_API,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            if resp.status_code >= 400:
                status = "error"
                error = resp.text[:800]
                logger.error("Resend error %s: %s", resp.status_code, error)
                result = {"ok": False, "error": error, "log_id": log_id}
            else:
                body = resp.json()
                provider_id = body.get("id")
                status = "sent"
                result = {"ok": True, "provider_id": provider_id, "log_id": log_id}
        except Exception as e:  # noqa: BLE001
            status = "error"
            error = str(e)
            logger.exception("Resend request failed")
            result = {"ok": False, "error": error, "log_id": log_id}

    if db is not None:
        db.add(
            m.EmailLog(
                id=log_id,
                to_email=to,
                subject=subject,
                template=template,
                status=status,
                provider_id=provider_id,
                error=error,
            )
        )
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    return result


def _base_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#0a0b10;font-family:Segoe UI,Arial,sans-serif;color:#f4f1ea;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0b10;padding:32px 16px;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#161821;border-radius:16px;border:1px solid rgba(255,255,255,0.08);padding:32px;">
        <tr><td>
          <div style="font-size:22px;font-weight:700;margin-bottom:8px;color:#e8a87c;">Helix</div>
          <div style="font-size:13px;color:#8f8890;margin-bottom:24px;">Training data studio</div>
          {body_html}
          <p style="margin-top:28px;font-size:12px;color:#6d6770;">
            If you didn’t request this, you can ignore this email.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_verification_email(db: Session, to: str, name: str, link: str) -> dict[str, Any]:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Confirm your email</h1>
      <p style="color:#c8c2b6;line-height:1.5;">Hi {name or "there"},</p>
      <p style="color:#c8c2b6;line-height:1.5;">Thanks for joining Helix. Confirm your email to activate your account.</p>
      <p style="margin:28px 0;">
        <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Confirm email
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">Or paste this link:<br>{link}</p>
    """
    return send_email(
        db,
        to=to,
        subject="Confirm your Helix account",
        html=_base_html("Confirm email", body),
        text=f"Confirm your Helix account: {link}",
        template="verify_email",
    )


def send_password_reset_email(db: Session, to: str, name: str, link: str) -> dict[str, Any]:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Reset your password</h1>
      <p style="color:#c8c2b6;line-height:1.5;">Hi {name or "there"},</p>
      <p style="color:#c8c2b6;line-height:1.5;">We received a request to reset your Helix password. This link expires in 24 hours.</p>
      <p style="margin:28px 0;">
        <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Choose a new password
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">Or paste this link:<br>{link}</p>
    """
    return send_email(
        db,
        to=to,
        subject="Reset your Helix password",
        html=_base_html("Reset password", body),
        text=f"Reset your Helix password: {link}",
        template="reset_password",
    )


def send_set_password_email(db: Session, to: str, name: str, link: str) -> dict[str, Any]:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Create your password</h1>
      <p style="color:#c8c2b6;line-height:1.5;">Hi {name or "there"},</p>
      <p style="color:#c8c2b6;line-height:1.5;">Your Helix account is ready. Set a password to start collecting training data.</p>
      <p style="margin:28px 0;">
        <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Create password
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">Or paste this link:<br>{link}</p>
    """
    return send_email(
        db,
        to=to,
        subject="Create your Helix password",
        html=_base_html("Create password", body),
        text=f"Create your Helix password: {link}",
        template="set_password",
    )


def send_admin_approval_request_email(
    db: Session,
    *,
    admin_email: str,
    user_email: str,
    user_name: str,
    user_id: str,
    created_at: str,
    approve_link: str,
) -> dict[str, Any]:
    """Notify admin that a user verified email and needs approval."""
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">New user awaiting approval</h1>
      <p style="color:#c8c2b6;line-height:1.5;">
        A user confirmed their email and is waiting for you to activate their Helix account.
      </p>
      <table cellpadding="0" cellspacing="0" style="width:100%;margin:18px 0;border-collapse:collapse;">
        <tr>
          <td style="padding:8px 0;color:#8f8890;width:120px;">Name</td>
          <td style="padding:8px 0;color:#f4f1ea;"><strong>{user_name or "—"}</strong></td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#8f8890;">Email</td>
          <td style="padding:8px 0;color:#f4f1ea;"><strong>{user_email}</strong></td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#8f8890;">User ID</td>
          <td style="padding:8px 0;color:#f4f1ea;font-family:monospace;font-size:12px;">{user_id}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#8f8890;">Signed up</td>
          <td style="padding:8px 0;color:#f4f1ea;">{created_at}</td>
        </tr>
      </table>
      <p style="margin:28px 0;">
        <a href="{approve_link}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Approve account
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">
        Or paste this secure link (expires in a few days):<br>{approve_link}
      </p>
    """
    return send_email(
        db,
        to=admin_email,
        subject=f"Approve Helix user: {user_email}",
        html=_base_html("Approve user", body),
        text=(
            f"Approve Helix user {user_name} <{user_email}> (id={user_id}).\n"
            f"Signed up: {created_at}\n"
            f"Approve: {approve_link}"
        ),
        template="admin_approve_request",
    )


def send_account_activated_email(
    db: Session, *, to: str, name: str, login_url: str
) -> dict[str, Any]:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">Your account is active</h1>
      <p style="color:#c8c2b6;line-height:1.5;">Hi {name or "there"},</p>
      <p style="color:#c8c2b6;line-height:1.5;">
        An administrator approved your Helix account. You can sign in and start collecting training data.
      </p>
      <p style="margin:28px 0;">
        <a href="{login_url}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Sign in to Helix
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">Or open:<br>{login_url}</p>
    """
    return send_email(
        db,
        to=to,
        subject="Your Helix account is active",
        html=_base_html("Account activated", body),
        text=f"Your Helix account is active. Sign in: {login_url}",
        template="account_activated",
    )


def send_invite_email(
    db: Session, to: str, name: str, inviter: str, workspace: str, link: str
) -> dict[str, Any]:
    body = f"""
      <h1 style="font-size:20px;margin:0 0 12px;">You’re invited to Helix</h1>
      <p style="color:#c8c2b6;line-height:1.5;">Hi {name or "there"},</p>
      <p style="color:#c8c2b6;line-height:1.5;">
        <strong>{inviter}</strong> invited you to the workspace <strong>{workspace}</strong>.
      </p>
      <p style="margin:28px 0;">
        <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#f0b58a,#e8a87c);color:#1a1210;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px;">
          Accept invite
        </a>
      </p>
      <p style="color:#8f8890;font-size:13px;word-break:break-all;">Or paste this link:<br>{link}</p>
    """
    return send_email(
        db,
        to=to,
        subject=f"Invitation to {workspace} on Helix",
        html=_base_html("Invitation", body),
        text=f"You're invited to {workspace} on Helix: {link}",
        template="invite",
    )
