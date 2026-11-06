"""Lettura delle risposte via IMAP, abbinamento alle richieste e classificazione."""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import re
import ssl
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import select

from .config import decrypt, get_int
from .db import (
    Broker, Identity, Message, Request, SeenMail, UnmatchedMail, log_event, utcnow,
)

# ------------------------------------------------------------------ domini

_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or", "gv", "ltd", "plc"}

# Piattaforme DSAR/helpdesk usate da molti broker: il dominio non identifica il broker
SHARED_DOMAINS = {
    "onetrust.com", "trustarc.com", "transcend.io", "zendesk.com", "freshdesk.com",
    "salesforce.com", "force.com", "hubspot.com", "intercom.io", "helpscout.net",
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "mailchimp.com", "sendgrid.net", "amazonses.com", "mcsv.net", "service-now.com",
    "kustomer.com", "gorgias.com", "privacy-center.org", "ketch.com", "securiti.ai",
    "osano.com", "datagrail.io", "mineos.ai", "truyo.com", "wirewheel.io",
}


def base_domain(addr_or_url: str) -> str:
    t = (addr_or_url or "").lower().strip()
    t = t.split("@")[-1]
    t = re.sub(r"^[a-z]+://", "", t).split("/")[0].split(":")[0]
    parts = [p for p in t.split(".") if p]
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ------------------------------------------------------------------ testo

_QUOTE_MARKERS = [
    r"^-{2,}\s*original (message|request)",
    r"^-{2,}\s*messaggio originale",
    r"^on .{5,200} wrote:\s*$",
    r"^il giorno .{5,200} ha scritto:?\s*$",
    r"^am .{5,200} schrieb .{0,100}:\s*$",
    r"^le .{5,200} a écrit\s*:\s*$",
    r"^from:\s.*$",
    r"^da:\s.*$",
    r"^dear .{1,300} privacy / data protection team",
    r"^i am writing to exercise my rights",
]
_QUOTE_RE = re.compile("|".join(_QUOTE_MARKERS), re.I | re.M)


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", h)).strip()


def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # charset sconosciuto
        content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content


def strip_quoted(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    m = _QUOTE_RE.search(text)
    return text[: m.start()] if m and m.start() > 0 else text


# ------------------------------------------------------------------ classificazione

_RULES: list[tuple[str, list[str]]] = [
    ("needs_action", [
        r"verify (your|the) (identity|email|request)", r"confirm (your|the) (identity|email address|request)",
        r"click (on )?(the|this) (link|button)", r"please (provide|send|submit|upload)",
        r"additional information", r"(web ?form|online form|privacy (portal|center|centre))",
        r"submit (your|a) request (via|through|using|at)", r"fill (out|in)", r"(identity|id) verification",
        r"proof of (identity|residence)", r"copy of (your|an?) (id|identity|passport)",
        r"verifica (la tua|della) identit", r"documento d.identit", r"clicca (sul|il) link",
        r"compila(re)? (il|un) modulo", r"bitte bestätigen", r"identitätsnachweis",
    ]),
    ("no_data", [
        r"no (records?|data|information|personal (data|information)) (about|on|for|associated|relating|matching|linked)",
        r"(do|did) not (hold|have|find|store|process|maintain) any", r"don.t (hold|have|store) any",
        r"(could|were|was) not (find|locate|identify) any", r"unable to (find|locate|identify) any",
        r"not (present|found) in our (database|records|systems?)", r"no matching (records?|profile)",
        r"nessun dato", r"non (abbiamo|risultano|deteniamo|trattiamo) (alcun|dati)",
        r"keine (daten|personenbezogenen)", r"no (personal )?data (was|were) found",
    ]),
    ("confirmed", [
        r"(has|have) been (deleted|erased|removed|suppressed|purged|anonymi[sz]ed)",
        r"(we|we.ve|we have) (deleted|erased|removed|suppressed|processed your (deletion|erasure|opt))",
        r"(deletion|erasure|removal|opt[- ]out) (request )?(is|has been|was) (complete|completed|processed|fulfilled)",
        r"successfully (deleted|removed|erased|processed|opted)",
        r"(request|richiesta) (has been|was|è stata) (completed|fulfilled|completata|evasa)",
        r"(sono stati|è stato|sono state) (cancellat|eliminat|rimoss)", r"abbiamo (cancellato|eliminato|rimosso)",
        r"wurden gelöscht", r"haben (ihre|deine) daten gelöscht",
    ]),
    ("refused", [
        r"(unable|not able) to (process|honou?r|fulfil|comply)", r"cannot (process|honou?r|fulfil|comply)",
        r"(only|solely) (available|applies|apply) to (california|us|u\.s\.) residents",
        r"not a (california|us) resident", r"(request|it) (has been|was|is) (denied|declined|rejected)",
        r"legal obligation to retain", r"non (possiamo|è possibile) (procedere|dare seguito)",
    ]),
]
_COMPILED = [(st, re.compile("|".join(p), re.I)) for st, p in _RULES]

# la classificazione piu' "forte" vince; "replied" e' il default
STRENGTH = {"replied": 1, "needs_action": 2, "refused": 3, "no_data": 4, "confirmed": 4, "bounced": 5}


def is_bounce(msg: EmailMessage) -> bool:
    frm = (msg.get("From") or "").lower()
    subj = (msg.get("Subject") or "").lower()
    if msg.get_content_type() == "multipart/report":
        return True
    if any(x in frm for x in ("mailer-daemon", "postmaster@", "mail delivery")):
        return True
    return bool(re.search(r"undeliver|delivery status notification \(failure\)|mail delivery failed|"
                          r"returned mail|delivery has failed|non recapitabile|unzustellbar", subj))


def classify(msg: EmailMessage, text: str) -> str:
    if is_bounce(msg):
        return "bounced"
    if (msg.get("Auto-Submitted") or "").lower().startswith("auto-replied") and len(text) < 600:
        return "replied"
    fresh = strip_quoted(text)[:6000]
    subj = msg.get("Subject") or ""
    hits = [st for st, rx in _COMPILED if rx.search(fresh) or rx.search(subj)]
    if not hits:
        return "replied"
    # se il broker conferma la cancellazione o dice di non avere dati, prevale sul "verifica"
    for st in ("confirmed", "no_data", "refused", "needs_action"):
        if st in hits:
            return st
    return "replied"


# ------------------------------------------------------------------ abbinamento

_MSGID_RE = re.compile(r"<[^<>\s@]+@[^<>\s]+>")


def _ref_tokens(msg: EmailMessage, raw: bytes) -> set[str]:
    toks = set()
    for h in ("In-Reply-To", "References"):
        toks.update(_MSGID_RE.findall(str(msg.get(h) or "")))
    # i rimbalzi e molte risposte riportano gli header originali nel corpo
    toks.update(_MSGID_RE.findall(raw.decode("utf-8", errors="replace")))
    return toks


def match_request(s, identity: Identity, msg: EmailMessage, raw: bytes, text: str) -> Request | None:
    toks = _ref_tokens(msg, raw)
    if toks:
        hit = s.scalars(
            select(Message).join(Request).where(
                Message.direction == "out", Message.message_id.in_(toks), Request.identity_id == identity.id,
            ).order_by(Message.date.desc())
        ).first()
        if hit:
            return hit.request

    recent = s.scalars(
        select(Request).join(Broker).where(
            Request.identity_id == identity.id, Request.sent_at.is_not(None),
            Request.sent_at > utcnow() - timedelta(days=400),
        ).order_by(Request.sent_at.desc())
    ).all()

    if is_bounce(msg):
        low = raw.decode("utf-8", errors="replace").lower()
        for r in recent:
            if r.to_email and r.to_email.lower() in low:
                return r
        return None

    senders = [a for _, a in getaddresses([str(msg.get("From") or ""), str(msg.get("Reply-To") or "")]) if a]
    domains = {base_domain(a) for a in senders} - {""}
    candidates: dict[int, Request] = {}
    for r in recent:
        b = r.broker
        bd = {base_domain(b.email), base_domain(b.website)} - {""}
        if domains & bd and not (domains & bd) <= SHARED_DOMAINS:
            candidates.setdefault(b.id, r)
    if len(candidates) == 1:
        return next(iter(candidates.values()))

    # dominio condiviso o ambiguo: proviamo con il nome del broker nel testo
    hay = ((msg.get("Subject") or "") + "\n" + text[:4000]).lower()
    pool = list(candidates.values()) or ([r for r in recent] if domains & SHARED_DOMAINS else [])
    named = {}
    for r in pool:
        name = re.sub(r"\s*\(.*\)$", "", r.broker.name).strip().lower()
        name = re.sub(r",?\s+(inc|llc|ltd|gmbh|s\.?p\.?a|s\.?r\.?l|corp|co|sas|b\.?v|ag|limited)\.?$", "", name)
        if len(name) >= 4 and re.search(r"\b" + re.escape(name) + r"\b", hay):
            named.setdefault(r.broker_id, r)
    if len(named) == 1:
        return next(iter(named.values()))
    return None


def apply_reply(s, req: Request, msg: EmailMessage, text: str, cls: str) -> None:
    try:
        date = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        date = utcnow()
    s.add(Message(
        request=req, direction="in", message_id=str(msg.get("Message-ID") or "")[:300], date=date,
        from_addr=str(msg.get("From") or "")[:300], to_addr=str(msg.get("To") or "")[:300],
        subject=str(msg.get("Subject") or "")[:500], body=text[:20000], classification=cls,
    ))
    if cls == "bounced":
        req.broker.bounce_count += 1
        limit = get_int(s, "bounce_disable_after")
        if req.broker.bounce_count >= limit and req.broker.enabled:
            req.broker.enabled = False
            log_event(s, f"Broker {req.broker.name} disattivato: {req.broker.bounce_count} rimbalzi su {req.broker.email}", "warn")
    else:
        req.reply_count += 1
        req.last_reply_at = date
    if req.status_manual:
        return
    cur = req.status
    if cur in ("sent", "reminded", "no_response", "failed") or STRENGTH.get(cls, 0) >= STRENGTH.get(cur, 0):
        req.status = cls


# ------------------------------------------------------------------ IMAP

def imap_connect(identity: Identity) -> imaplib.IMAP4:
    if identity.imap_port != 993:
        conn = imaplib.IMAP4(identity.imap_host, identity.imap_port, timeout=60)
        if "STARTTLS" in conn.capabilities:
            conn.starttls(ssl_context=ssl.create_default_context())
    else:
        conn = imaplib.IMAP4_SSL(identity.imap_host, identity.imap_port,
                                 ssl_context=ssl.create_default_context(), timeout=60)
    conn.login(identity.imap_user or identity.email, decrypt(identity.imap_password_enc))
    return conn


def _quote_folder(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def poll_identity(s, identity: Identity, max_fetch: int = 400) -> dict:
    stats = {"checked": 0, "matched": 0, "unmatched": 0}
    first_sent = s.scalars(
        select(Request.sent_at).where(Request.identity_id == identity.id, Request.sent_at.is_not(None))
        .order_by(Request.sent_at)
    ).first()
    if first_sent is None:
        return stats
    since_dt = max(first_sent, (identity.last_imap_check or first_sent) - timedelta(days=3))
    since = since_dt.strftime("%d-%b-%Y")

    conn = imap_connect(identity)
    try:
        for folder in [f.strip() for f in identity.imap_folders.split(",") if f.strip()]:
            typ, _ = conn.select(_quote_folder(folder), readonly=True)
            if typ != "OK":
                log_event(s, f"{identity.label}: cartella IMAP '{folder}' non trovata", "warn")
                continue
            uidv = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
            uidv = uidv.decode() if isinstance(uidv, bytes) else str(uidv)
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            if typ != "OK":
                continue
            uids = [u.decode() for u in (data[0] or b"").split()]
            seen = set(s.scalars(select(SeenMail.uid).where(
                SeenMail.identity_id == identity.id, SeenMail.folder == folder, SeenMail.uidvalidity == uidv)))
            todo = [u for u in uids if u not in seen][:max_fetch]
            for uid in todo:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                s.add(SeenMail(identity_id=identity.id, folder=folder, uidvalidity=uidv, uid=uid))
                if not raw:
                    continue
                stats["checked"] += 1
                handle_raw(s, identity, raw, stats)
            s.commit()
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    identity.last_imap_check = utcnow()
    return stats


def handle_raw(s, identity: Identity, raw: bytes, stats: dict) -> None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    frm = " ".join(a for _, a in getaddresses([str(msg.get("From") or "")])).lower()
    if identity.email.lower() in frm:
        return  # copia di una nostra mail
    text = body_text(msg)
    req = match_request(s, identity, msg, raw, text)
    if req is None:
        # memorizziamo solo mail che sembrano pertinenti (dominio di un broker o rimbalzo)
        doms = {base_domain(a) for a in frm.split()}
        known = doms and s.scalars(select(Broker.id).where(Broker.email.like(f"%@{next(iter(doms))}"))).first()
        if is_bounce(msg) or known:
            s.add(UnmatchedMail(identity_id=identity.id, from_addr=str(msg.get("From") or "")[:300],
                                subject=str(msg.get("Subject") or "")[:500], body=text[:20000]))
            stats["unmatched"] += 1
        return
    cls = classify(msg, text)
    apply_reply(s, req, msg, text, cls)
    stats["matched"] += 1
