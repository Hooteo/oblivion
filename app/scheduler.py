"""Job periodici: campagne ogni 3/6 mesi, coda di invio, solleciti, lettura IMAP."""
from __future__ import annotations

import calendar
import logging
import smtplib
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from . import inbox, mailer
from .config import get_int, get_setting
from .db import (
    Broker, Campaign, Identity, Message, Request, log_event, session_scope, utcnow,
)

log = logging.getLogger("privacy-sweep")
_send_lock = threading.Lock()
_imap_lock = threading.Lock()
_backoff: dict[int, datetime] = {}  # identity_id -> non inviare prima di


def add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    y, m = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def local_day_start_utc() -> datetime:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def in_send_window(s) -> bool:
    try:
        a, b = (int(x) for x in get_setting(s, "send_window").split("-"))
    except ValueError:
        return True
    return a <= datetime.now().hour < b


def smtp_ready(i: Identity) -> bool:
    return bool(i.smtp_host and i.smtp_password_enc)


def imap_ready(i: Identity) -> bool:
    return bool(i.imap_host and i.imap_password_enc)


# ------------------------------------------------------------------ campagne

def start_campaign(s, identity: Identity, trigger: str = "schedule") -> Campaign:
    # le richieste di una campagna precedente ancora in coda non hanno piu' senso
    for r in s.scalars(select(Request).where(Request.identity_id == identity.id, Request.status == "queued")):
        r.status = "cancelled"
    brokers = s.scalars(select(Broker).where(
        Broker.enabled.is_(True), Broker.method == "email", Broker.email != "")).all()
    camp = Campaign(identity_id=identity.id, trigger=trigger, total=len(brokers))
    s.add(camp)
    s.flush()
    for b in brokers:
        s.add(Request(campaign_id=camp.id, identity_id=identity.id, broker_id=b.id, to_email=b.email))
    now = utcnow()
    identity.last_campaign_at = now
    identity.next_campaign_at = add_months(now, identity.interval_months)
    log_event(s, f"{identity.label}: nuova campagna ({trigger}) verso {len(brokers)} broker; "
                 f"prossima il {identity.next_campaign_at:%d/%m/%Y}")
    return camp


def job_schedule() -> None:
    with session_scope() as s:
        now = utcnow()
        for i in s.scalars(select(Identity).where(Identity.enabled.is_(True))):
            if i.next_campaign_at and i.next_campaign_at <= now and smtp_ready(i):
                start_campaign(s, i, "schedule")
        close_after = get_int(s, "close_after_days")
        stale = s.scalars(select(Request).where(
            Request.status == "reminded", Request.status_manual.is_(False),
            Request.reminded_at < now - timedelta(days=close_after)))
        for r in stale:
            r.status = "no_response"


# ------------------------------------------------------------------ invio

def _next_job(s, identity: Identity) -> tuple[Request, bool] | None:
    days = get_int(s, "reminder_days")
    due = s.scalars(select(Request).join(Broker).where(
        Request.identity_id == identity.id, Request.status == "sent", Request.status_manual.is_(False),
        Request.reminded_at.is_(None), Request.sent_at < utcnow() - timedelta(days=days),
        Broker.enabled.is_(True),
    ).order_by(Request.sent_at)).first()
    if due:
        return due, True
    q = s.scalars(select(Request).join(Broker).where(
        Request.identity_id == identity.id, Request.status == "queued", Broker.enabled.is_(True),
    ).order_by(Request.id)).first()
    return (q, False) if q else None


def job_send() -> None:
    if not _send_lock.acquire(blocking=False):
        return
    try:
        with session_scope() as s:
            if not in_send_window(s):
                return
            interval = get_int(s, "send_interval_seconds")
            day_start = local_day_start_utc()
            for i in s.scalars(select(Identity).where(Identity.enabled.is_(True))).all():
                if not smtp_ready(i) or _backoff.get(i.id, datetime.min) > utcnow():
                    continue
                sent_today = s.scalar(select(func.count(Message.id)).join(Request).where(
                    Request.identity_id == i.id, Message.direction == "out", Message.date >= day_start)) or 0
                if sent_today >= i.daily_limit:
                    continue
                last = s.scalar(select(func.max(Message.date)).join(Request).where(
                    Request.identity_id == i.id, Message.direction == "out"))
                if last and (utcnow() - last).total_seconds() < interval:
                    continue
                job = _next_job(s, i)
                if not job:
                    continue
                req, reminder = job
                _send_one(s, i, req, reminder)
                s.commit()
    finally:
        _send_lock.release()


def _send_one(s, identity: Identity, req: Request, reminder: bool) -> None:
    req.attempts += 1
    try:
        conn = mailer.smtp_connect(identity)
        try:
            mailer.send_request(s, conn, req, reminder)
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        identity.last_error = ""
    except smtplib.SMTPRecipientsRefused as e:
        req.status, req.error = "bounced", f"Destinatario rifiutato: {e}"[:1000]
        req.broker.bounce_count += 1
        if req.broker.bounce_count >= get_int(s, "bounce_disable_after"):
            req.broker.enabled = False
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused) as e:
        identity.last_error = f"SMTP: {e}"[:1000]
        _backoff[identity.id] = utcnow() + timedelta(hours=1)
        req.attempts -= 1
        log_event(s, f"{identity.label}: errore SMTP, invii sospesi 1 ora: {e}", "error")
    except Exception as e:  # rete, limite giornaliero del provider, ecc.
        identity.last_error = f"{type(e).__name__}: {e}"[:1000]
        _backoff[identity.id] = utcnow() + timedelta(minutes=20)
        req.error = identity.last_error
        if req.attempts >= 4:
            req.status = "failed"
        log_event(s, f"{identity.label}: invio a {req.broker.name} fallito: {e}", "warn")


# ------------------------------------------------------------------ IMAP

def job_imap(identity_id: int | None = None) -> dict:
    total = {"checked": 0, "matched": 0, "unmatched": 0}
    if not _imap_lock.acquire(blocking=False):
        return total
    try:
        with session_scope() as s:
            q = select(Identity).where(Identity.enabled.is_(True))
            if identity_id:
                q = select(Identity).where(Identity.id == identity_id)
            for i in s.scalars(q).all():
                if not imap_ready(i):
                    continue
                try:
                    st = inbox.poll_identity(s, i)
                    for k in total:
                        total[k] += st[k]
                    if st["matched"]:
                        log_event(s, f"{i.label}: {st['matched']} nuove risposte da broker")
                except Exception as e:
                    i.last_error = f"IMAP: {e}"[:1000]
                    log_event(s, f"{i.label}: lettura IMAP fallita: {e}", "warn")
                s.commit()
    finally:
        _imap_lock.release()
    return total


def start_scheduler() -> BackgroundScheduler:
    with session_scope() as s:
        poll = max(5, get_int(s, "imap_poll_minutes"))
    sch = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    sch.add_job(job_send, "interval", seconds=20, id="send")
    sch.add_job(job_schedule, "interval", minutes=10, id="schedule", next_run_time=datetime.now())
    sch.add_job(job_imap, "interval", minutes=poll, id="imap")
    sch.start()
    return sch
