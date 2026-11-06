"""Modelli e sessione database (SQLite in /data)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
engine = create_engine(
    f"sqlite:///{DATA_DIR}/oblivion.db",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Identity(Base):
    """Una delle caselle email dell'utente: e' sia mittente sia il dato da cancellare."""
    __tablename__ = "identities"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    extra_identifiers: Mapped[str] = mapped_column(Text, default="")  # telefono, indirizzo, vecchie email...
    smtp_host: Mapped[str] = mapped_column(String(200), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_security: Mapped[str] = mapped_column(String(10), default="ssl")  # ssl | starttls | none
    smtp_user: Mapped[str] = mapped_column(String(200), default="")
    smtp_password_enc: Mapped[str] = mapped_column(Text, default="")
    imap_host: Mapped[str] = mapped_column(String(200), default="")
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_user: Mapped[str] = mapped_column(String(200), default="")
    imap_password_enc: Mapped[str] = mapped_column(Text, default="")
    imap_folders: Mapped[str] = mapped_column(String(300), default="INBOX")
    # False solo per Proton Mail Bridge, che usa un certificato autofirmato sulla rete locale
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_months: Mapped[int] = mapped_column(Integer, default=6)
    daily_limit: Mapped[int] = mapped_column(Integer, default=80)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_campaign_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_campaign_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_imap_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Broker(Base):
    __tablename__ = "brokers"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str] = mapped_column(String(200), default="", index=True)
    website: Mapped[str] = mapped_column(String(300), default="")
    privacy_url: Mapped[str] = mapped_column(String(500), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="manual")  # cppa | datarequests | manual
    jurisdiction: Mapped[str] = mapped_column(String(5), default="eu")
    method: Mapped[str] = mapped_column(String(20), default="email")  # email | webform | letter
    categories: Mapped[list] = mapped_column(JSON, default=list)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    user_modified: Mapped[bool] = mapped_column(Boolean, default=False)  # l'utente ha toccato "enabled"
    bounce_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_upstream: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual
    total: Mapped[int] = mapped_column(Integer, default=0)
    identity: Mapped[Identity] = relationship()


# Stati di una richiesta
STATUSES = {
    "queued": "In coda",
    "sent": "Inviata, in attesa",
    "reminded": "Sollecito inviato",
    "replied": "Ha risposto",
    "needs_action": "Serve un tuo intervento",
    "confirmed": "Cancellazione confermata",
    "no_data": "Nessun dato trovato",
    "refused": "Rifiutata",
    "no_response": "Nessuna risposta",
    "bounced": "Email non recapitata",
    "failed": "Errore di invio",
    "cancelled": "Annullata",
}
OPEN_STATUSES = {"sent", "reminded", "replied"}
DONE_STATUSES = {"confirmed", "no_data", "refused", "no_response", "bounced", "cancelled"}


class Request(Base):
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id", ondelete="CASCADE"), index=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    status_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    to_email: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    identity: Mapped[Identity] = relationship()
    broker: Mapped[Broker] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="request", order_by="Message.date", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # out | in
    message_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    from_addr: Mapped[str] = mapped_column(String(300), default="")
    to_addr: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(20), default="")
    request: Mapped[Request] = relationship(back_populates="messages")


class SeenMail(Base):
    """UID IMAP gia' elaborati, per non rileggere la stessa mail."""
    __tablename__ = "seen_mail"
    __table_args__ = (UniqueConstraint("identity_id", "folder", "uidvalidity", "uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id", ondelete="CASCADE"))
    folder: Mapped[str] = mapped_column(String(200))
    uidvalidity: Mapped[str] = mapped_column(String(40))
    uid: Mapped[str] = mapped_column(String(40))


class UnmatchedMail(Base):
    """Mail arrivate da domini di broker ma non abbinate con certezza a una richiesta."""
    __tablename__ = "unmatched_mail"
    id: Mapped[int] = mapped_column(primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id", ondelete="CASCADE"))
    date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    from_addr: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    """Registro attivita' mostrato in dashboard."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    text: Mapped[str] = mapped_column(Text)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def log_event(s, text: str, level: str = "info") -> None:
    s.add(Event(text=text[:2000], level=level))


# colonne aggiunte dopo la prima versione: (tabella, colonna, definizione SQL)
_MIGRATIONS = [
    ("identities", "tls_verify", "BOOLEAN NOT NULL DEFAULT 1"),
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, col, ddl in _MIGRATIONS:
            cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
