"""Composizione e invio delle richieste via SMTP."""
from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import decrypt, get_setting
from .db import Broker, Identity, Message, Request, utcnow


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, identity: Identity, broker: Broker, original: datetime | None = None) -> str:
    extra = (identity.extra_identifiers or "").strip()
    return template.format_map(_Safe(
        full_name=identity.full_name,
        email=identity.email,
        extra=(extra + "\n") if extra else "",
        broker_name=broker.name,
        date=datetime.now().strftime("%d/%m/%Y"),
        original_date=original.strftime("%d/%m/%Y") if original else "",
    ))


def smtp_connect(identity: Identity) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if identity.smtp_security == "ssl":
        conn = smtplib.SMTP_SSL(identity.smtp_host, identity.smtp_port, timeout=60, context=ctx)
    else:
        conn = smtplib.SMTP(identity.smtp_host, identity.smtp_port, timeout=60)
        conn.ehlo()
        if identity.smtp_security == "starttls":
            conn.starttls(context=ctx)
            conn.ehlo()
    user = identity.smtp_user or identity.email
    pwd = decrypt(identity.smtp_password_enc)
    if pwd:
        conn.user, conn.password = user, pwd
        conn.ehlo_or_helo_if_needed()
        if "plain" in (conn.esmtp_features.get("auth") or "").lower():
            # Solo AUTH PLAIN: con login() smtplib, dopo un 535, ritenta con AUTH LOGIN e
            # Gmail chiude la connessione, nascondendo l'errore vero ("password non accettata").
            try:
                conn.auth("PLAIN", conn.auth_plain)
            except smtplib.SMTPAuthenticationError as e:
                raise smtplib.SMTPAuthenticationError(e.smtp_code, auth_hint(identity, e)) from None
        else:
            conn.login(user, pwd)
    return conn


def auth_hint(identity: Identity, err: Exception) -> str:
    msg = getattr(err, "smtp_error", b"") or str(err)
    msg = msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)
    msg = " ".join(msg.split())
    host = (identity.smtp_host + identity.imap_host).lower()
    if "gmail" in host or "google" in host:
        msg += (" → Gmail accetta solo una password per le app (16 lettere), da creare su "
                "https://myaccount.google.com/apppasswords con la verifica in due passaggi attiva.")
    elif any(h in host for h in ("yahoo", "icloud", "me.com", "outlook", "office365")):
        msg += " → Questo provider richiede una password per le app, non la password normale."
    return msg


def build_message(s, req: Request, reminder: bool = False) -> EmailMessage:
    ident, broker = req.identity, req.broker
    if reminder:
        subject = render(get_setting(s, "reminder_subject"), ident, broker, req.sent_at)
        body = render(get_setting(s, "reminder_body"), ident, broker, req.sent_at)
        first_out = next((m for m in req.messages if m.direction == "out"), None)
        if first_out:
            body += "\n\n----- Original request -----\n" + first_out.body
    else:
        subject = render(get_setting(s, "subject"), ident, broker)
        body = render(get_setting(s, "body"), ident, broker)

    msg = EmailMessage()
    msg["From"] = formataddr((ident.full_name, ident.email))
    msg["To"] = req.to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=ident.email.split("@")[-1])
    if reminder:
        prev = [m.message_id for m in req.messages if m.direction == "out" and m.message_id]
        if prev:
            msg["In-Reply-To"] = prev[-1]
            msg["References"] = " ".join(prev)
    msg.set_content(body)
    return msg


def send_request(s, conn: smtplib.SMTP, req: Request, reminder: bool = False) -> None:
    """Invia la richiesta (o il sollecito) e la registra. Solleva eccezioni SMTP."""
    msg = build_message(s, req, reminder)
    conn.send_message(msg)
    now = utcnow()
    s.add(Message(
        request=req, direction="out", message_id=msg["Message-ID"], date=now,
        from_addr=req.identity.email, to_addr=req.to_email,
        subject=str(msg["Subject"]), body=msg.get_content(),
    ))
    if reminder:
        req.reminded_at = now
        if not req.status_manual:
            req.status = "reminded"
    else:
        req.sent_at = now
        req.status = "sent"
    req.error = ""
