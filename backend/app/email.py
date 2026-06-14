"""Pluggable transactional-email sender — currently just password resets.

One public coroutine, :func:`send_password_reset_email`, with three transports
selected at call time by environment configuration (first match wins):

  1. **Resend HTTP API** — when ``RESEND_API_KEY`` is set. POSTs to
     ``https://api.resend.com/emails`` with ``httpx`` (already a backend dep).
  2. **SMTP** — when ``SMTP_HOST`` + ``SMTP_USER`` + ``SMTP_PASS`` are all set.
     Uses stdlib :mod:`smtplib` (STARTTLS) run in a worker thread so the event
     loop is never blocked. No new dependency (``aiosmtplib`` is not installed).
  3. **Dev fallback** — otherwise. Logs the recipient + reset link so the flow
     works end-to-end locally with zero provider configuration.

Design rules (mirroring the rest of the backend):

* **Never raises to the caller.** A send failure (bad key, SMTP timeout,
  network blip) is logged and swallowed. The ``POST /auth/forgot`` route always
  returns ``{ok: true}``; surfacing transport errors there would turn the
  endpoint into a deliverability oracle / account-enumeration side channel.
* **Config via env, read through the existing Settings singleton when the field
  exists, else straight from ``os.environ``.** The provider creds
  (RESEND/SMTP/EMAIL_FROM) are NOT declared on
  :class:`app.infrastructure.Settings`, so we read them from ``os.environ``;
  ``EMAIL_FROM`` falls back to a sensible default. ``settings.extra="ignore"``
  means adding them to ``.env`` won't break Settings either way.
* **Dependency-light.** Only ``httpx`` (already present) and the stdlib.
"""
from __future__ import annotations

import asyncio
import logging
import os
from email.message import EmailMessage

_log = logging.getLogger("agentic_ai.email")

_RESEND_ENDPOINT = "https://api.resend.com/emails"

# Default From: address used when EMAIL_FROM is unset. Resend accepts its
# sandbox onboarding sender with no domain verification, which keeps the dev /
# first-run experience zero-config.
_DEFAULT_FROM = "Metric AI <onboarding@resend.dev>"

_SUBJECT = "Reset your Metric AI password"


def _from_address() -> str:
    return (os.environ.get("EMAIL_FROM") or "").strip() or _DEFAULT_FROM


def _plaintext_body(reset_url: str) -> str:
    return (
        "You (or someone) requested a password reset for your Metric AI "
        "account.\n\n"
        f"Reset your password using the link below (valid for a limited "
        f"time):\n\n{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email — your "
        "password won't change.\n"
    )


def _html_body(reset_url: str) -> str:
    return (
        '<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;'
        'color:#18181b;line-height:1.5">'
        "<p>You (or someone) requested a password reset for your "
        "<strong>Metric AI</strong> account.</p>"
        f'<p><a href="{reset_url}" '
        'style="display:inline-block;padding:10px 18px;background:#10b981;'
        'color:#022c22;border-radius:8px;text-decoration:none;font-weight:600">'
        "Reset password</a></p>"
        "<p>Or paste this link into your browser (valid for a limited "
        f'time):<br><a href="{reset_url}">{reset_url}</a></p>'
        "<p style=\"color:#71717a;font-size:13px\">If you didn't request this, "
        "you can safely ignore this email — your password won't change.</p>"
        "</div>"
    )


async def send_password_reset_email(to_email: str, reset_url: str) -> None:
    """Send a password-reset email to ``to_email`` containing ``reset_url``.

    Transport is chosen by environment (Resend → SMTP → dev-log fallback).
    NEVER raises: any transport failure is logged at WARNING and swallowed so
    the caller (``POST /auth/forgot``) can always return ``{ok: true}`` without
    leaking whether the address exists or whether delivery succeeded.
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return
    try:
        if os.environ.get("RESEND_API_KEY"):
            await _send_via_resend(to_email, reset_url)
        elif (
            os.environ.get("SMTP_HOST")
            and os.environ.get("SMTP_USER")
            and os.environ.get("SMTP_PASS")
        ):
            await asyncio.to_thread(_send_via_smtp, to_email, reset_url)
        else:
            _log_dev_fallback(to_email, reset_url)
    except Exception:
        # Swallow — see module/function docstring. A failed send must never
        # propagate to the /auth/forgot handler.
        _log.warning(
            "password-reset email to %r failed to send (swallowed)",
            to_email,
            exc_info=True,
        )


async def _send_via_resend(to_email: str, reset_url: str) -> None:
    import httpx

    api_key = os.environ["RESEND_API_KEY"]
    payload = {
        "from": _from_address(),
        "to": [to_email],
        "subject": _SUBJECT,
        "text": _plaintext_body(reset_url),
        "html": _html_body(reset_url),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    # Raise inside the try in send_password_reset_email so it's logged+swallowed
    # uniformly with every other transport's failures.
    resp.raise_for_status()
    _log.info("password-reset email sent via Resend to %r", to_email)


def _send_via_smtp(to_email: str, reset_url: str) -> None:
    """Blocking SMTP send (STARTTLS). Run via ``asyncio.to_thread``."""
    import smtplib

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]

    msg = EmailMessage()
    msg["Subject"] = _SUBJECT
    msg["From"] = _from_address()
    msg["To"] = to_email
    msg.set_content(_plaintext_body(reset_url))
    msg.add_alternative(_html_body(reset_url), subtype="html")

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls()
            smtp.ehlo()
        except smtplib.SMTPException:
            # Server without STARTTLS support — continue plain (e.g. local
            # MailHog / dev relay). Real providers always offer STARTTLS.
            pass
        smtp.login(user, password)
        smtp.send_message(msg)
    _log.info("password-reset email sent via SMTP to %r", to_email)


def _log_dev_fallback(to_email: str, reset_url: str) -> None:
    """No provider configured — log the link so local dev still works."""
    _log.warning(
        "[email-dev-fallback] No RESEND_API_KEY / SMTP_* configured. "
        "Password-reset link for %r:\n    %s",
        to_email,
        reset_url,
    )
