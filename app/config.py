"""Impostazioni salvate nel DB, cifratura delle password, modelli delle email."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from .db import DATA_DIR, Setting

# ---------------------------------------------------------------- cifratura

def _load_key() -> bytes:
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    with open(path, "rb") as f:
        return f.read().strip()


_fernet = Fernet(_load_key())


def encrypt(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode() if plain else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


# ---------------------------------------------------------------- modelli email

DEFAULT_SUBJECT = "Request for erasure of personal data (Art. 17 GDPR) - {full_name}"

DEFAULT_BODY = """Dear {broker_name} Privacy / Data Protection Team,

I am writing to exercise my rights under the EU General Data Protection Regulation (GDPR). As a resident of the European Union, the GDPR applies to any processing of my personal data, including by organisations established outside the EU (Art. 3(2) GDPR).

I hereby request that you:

1. Erase all personal data you hold about me without undue delay (Art. 17 GDPR).
2. Stop any processing of my personal data for direct marketing, profiling or sale/sharing; I object to such processing (Art. 21 GDPR). Any consent I may have given is withdrawn (Art. 7(3) GDPR).
3. Inform every recipient to whom my data has been disclosed of this erasure (Art. 19 GDPR).
4. Confirm in writing, to this email address, once the erasure has been completed.

If you are also subject to the California Consumer Privacy Act or other US state privacy laws, please additionally treat this message as a request to delete and to opt out of the sale and sharing of my personal information.

The data you can use to identify me:
Name: {full_name}
Email: {email}
{extra}
Please do not use this request to collect additional data about me, and use the information above only to locate and delete my records. If you hold no data about me, please let me know.

You are required to respond within one month of receipt (Art. 12(3) GDPR). If I receive no response I reserve the right to lodge a complaint with the competent supervisory authority (Garante per la protezione dei dati personali).

Kind regards,
{full_name}
{date}
"""

DEFAULT_REMINDER_SUBJECT = "Reminder: erasure request (Art. 17 GDPR) sent on {original_date} - {full_name}"

DEFAULT_REMINDER_BODY = """Dear {broker_name} Privacy / Data Protection Team,

On {original_date} I sent you a request for erasure of my personal data under Art. 17 GDPR (copy below). The one-month deadline set by Art. 12(3) GDPR has passed and I have not received a response.

Please complete the erasure and confirm it to this email address within 14 days. Otherwise I will lodge a complaint with the Italian supervisory authority (Garante per la protezione dei dati personali).

Name: {full_name}
Email: {email}
{extra}
Kind regards,
{full_name}
{date}
"""

DEFAULTS = {
    "subject": DEFAULT_SUBJECT,
    "body": DEFAULT_BODY,
    "reminder_subject": DEFAULT_REMINDER_SUBJECT,
    "reminder_body": DEFAULT_REMINDER_BODY,
    "reminder_days": "35",        # giorni dopo l'invio per il sollecito
    "close_after_days": "30",     # giorni dopo il sollecito per chiudere come "nessuna risposta"
    "send_interval_seconds": "45",  # pausa minima tra due invii della stessa casella
    "send_window": "8-21",        # ore (ora locale) in cui si inviano mail
    "bounce_disable_after": "2",  # rimbalzi dopo cui il broker viene disattivato
    "imap_poll_minutes": "15",
}


def get_setting(s, key: str) -> str:
    row = s.get(Setting, key)
    return row.value if row is not None else DEFAULTS.get(key, "")


def get_int(s, key: str) -> int:
    try:
        return int(get_setting(s, key))
    except ValueError:
        return int(DEFAULTS[key])


def set_setting(s, key: str, value: str) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value
