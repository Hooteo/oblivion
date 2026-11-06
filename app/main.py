"""Dashboard web (FastAPI + Jinja2)."""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import timezone
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.base import BaseHTTPMiddleware

from . import brokers as broker_db
from . import inbox, mailer, scheduler, sources
from .config import DEFAULTS, encrypt, get_setting, set_setting
from .db import (
    DATA_DIR, STATUSES, Broker, Campaign, Event, Identity, Message, Request, UnmatchedMail,
    init_db, log_event, session_scope, utcnow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oblivion")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
BASE = os.path.dirname(__file__)
PAGE = 100

# ------------------------------------------------------------------ auth


def _dashboard_password() -> str:
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if pw:
        return pw
    path = os.path.join(DATA_DIR, "dashboard-password.txt")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(18))
        log.warning("DASHBOARD_PASSWORD non impostata: password generata in %s", path)
    return open(path).read().strip()


USER = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = _dashboard_password()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in ("/healthz",) or request.url.path.startswith("/static/"):
            return await call_next(request)
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, USER) and secrets.compare_digest(p, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return PlainTextResponse("Autenticazione richiesta", 401,
                                     {"WWW-Authenticate": 'Basic realm="oblivion"'})
        if request.method == "POST":  # protezione CSRF: il POST deve arrivare dalla dashboard stessa
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Richiesta da origine non valida", 403)
        return await call_next(request)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with session_scope() as s:
        broker_db.load_bundled(s)
    sched = scheduler.start_scheduler()
    log.info("Oblivion avviato")
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Oblivion", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _local(dt, fmt="%d/%m/%Y %H:%M"):
    if not dt:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime(fmt)


tpl.env.filters["local"] = _local
tpl.env.filters["day"] = lambda dt: _local(dt, "%d/%m/%Y")
tpl.env.globals["STATUSES"] = STATUSES


def render(request: HttpRequest, name: str, **ctx):
    with session_scope() as s:
        ctx.setdefault("unmatched_count", s.scalar(select(func.count(UnmatchedMail.id))) or 0)
    return tpl.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg})
    return RedirectResponse(url, status_code=303)


@app.get("/healthz")
def healthz():
    with session_scope() as s:
        errs = [i.label for i in s.scalars(select(Identity).where(Identity.enabled.is_(True))) if i.last_error]
    return JSONResponse({"status": "warn" if errs else "ok", "identity_errors": errs})


# ------------------------------------------------------------------ dashboard


def _stats(s, identity_id: int | None = None) -> dict:
    q = select(Request.status, func.count(Request.id))
    if identity_id:
        q = q.where(Request.identity_id == identity_id)
    counts = dict(s.execute(q.group_by(Request.status)).all())
    return {k: counts.get(k, 0) for k in STATUSES} | {"total": sum(counts.values())}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: HttpRequest):
    with session_scope() as s:
        idents = s.scalars(select(Identity).order_by(Identity.id)).all()
        cards = []
        for i in idents:
            camp = s.scalars(select(Campaign).where(Campaign.identity_id == i.id)
                             .order_by(Campaign.started_at.desc())).first()
            cards.append({"i": i, "st": _stats(s, i.id), "camp": camp,
                          "smtp": scheduler.smtp_ready(i), "imap": scheduler.imap_ready(i)})
        replies = s.scalars(select(Message).where(Message.direction == "in")
                            .order_by(Message.date.desc()).limit(15)).all()
        for m in replies:
            _ = m.request.broker.name, m.request.identity.label
        action = s.scalars(select(Request).where(Request.status == "needs_action")
                           .order_by(Request.last_reply_at.desc()).limit(15)).all()
        for r in action:
            _ = r.broker.name, r.identity.label
        events = s.scalars(select(Event).order_by(Event.at.desc()).limit(12)).all()
        brokers_on = s.scalar(select(func.count(Broker.id)).where(
            Broker.enabled.is_(True), Broker.method == "email", Broker.email != "")) or 0
        return render(request, "dashboard.html", cards=cards, total=_stats(s), replies=replies,
                      action=action, events=events, brokers_on=brokers_on,
                      msg=request.query_params.get("msg", ""))


# ------------------------------------------------------------------ richieste


@app.get("/requests", response_class=HTMLResponse)
def requests_list(request: HttpRequest, identity: int = 0, status: str = "", q: str = "", page: int = 1):
    with session_scope() as s:
        qry = select(Request).join(Broker)
        if identity:
            qry = qry.where(Request.identity_id == identity)
        if status == "open":
            qry = qry.where(Request.status.in_(["sent", "reminded"]))
        elif status == "answered":
            qry = qry.where(Request.reply_count > 0)
        elif status:
            qry = qry.where(Request.status == status)
        if q:
            like = f"%{q}%"
            qry = qry.where(or_(Broker.name.ilike(like), Broker.email.ilike(like)))
        total = s.scalar(select(func.count()).select_from(qry.subquery())) or 0
        rows = s.scalars(qry.order_by(Request.last_reply_at.desc().nulls_last(),
                                      Request.sent_at.desc().nulls_last(), Request.id.desc())
                         .offset((page - 1) * PAGE).limit(PAGE)).all()
        for r in rows:
            _ = r.broker.name, r.identity.label
        idents = s.scalars(select(Identity).order_by(Identity.id)).all()
        return render(request, "requests.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
                      idents=idents, f_identity=identity, f_status=status, q=q,
                      msg=request.query_params.get("msg", ""))


@app.get("/requests/{rid}", response_class=HTMLResponse)
def request_detail(request: HttpRequest, rid: int):
    with session_scope() as s:
        r = s.get(Request, rid)
        if not r:
            raise HTTPException(404)
        _ = r.broker.name, r.identity.label, list(r.messages)
        return render(request, "request.html", r=r, msg=request.query_params.get("msg", ""))


@app.post("/requests/{rid}/status")
def request_status(rid: int, status: str = Form(...), notes: str = Form("")):
    with session_scope() as s:
        r = s.get(Request, rid)
        if not r or status not in STATUSES:
            raise HTTPException(400)
        if status != r.status:
            r.status, r.status_manual = status, True
        r.notes = notes
    return back(f"/requests/{rid}", "Salvato")


@app.post("/requests/{rid}/auto")
def request_auto(rid: int):
    with session_scope() as s:
        r = s.get(Request, rid)
        if r:
            r.status_manual = False
    return back(f"/requests/{rid}", "Stato di nuovo gestito automaticamente")


@app.post("/requests/{rid}/resend")
def request_resend(rid: int):
    with session_scope() as s:
        r = s.get(Request, rid)
        if not r:
            raise HTTPException(404)
        r.to_email = r.broker.email
        r.status, r.status_manual, r.attempts, r.error, r.reminded_at = "queued", False, 0, "", None
    return back(f"/requests/{rid}", "Rimesso in coda")


# ------------------------------------------------------------------ broker


@app.get("/brokers", response_class=HTMLResponse)
def brokers_list(request: HttpRequest, q: str = "", source: str = "", state: str = "", page: int = 1):
    with session_scope() as s:
        qry = select(Broker)
        if q:
            like = f"%{q}%"
            qry = qry.where(or_(Broker.name.ilike(like), Broker.email.ilike(like), Broker.website.ilike(like)))
        if source:
            qry = qry.where(Broker.source == source)
        if state == "on":
            qry = qry.where(Broker.enabled.is_(True))
        elif state == "off":
            qry = qry.where(Broker.enabled.is_(False))
        elif state == "manual":
            qry = qry.where(Broker.method != "email")
        elif state == "bounced":
            qry = qry.where(Broker.bounce_count > 0)
        total = s.scalar(select(func.count()).select_from(qry.subquery())) or 0
        rows = s.scalars(qry.order_by(func.lower(Broker.name)).offset((page - 1) * PAGE).limit(PAGE)).all()
        counts = dict(s.execute(select(Broker.enabled, func.count(Broker.id)).group_by(Broker.enabled)).all())
        return render(request, "brokers.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
                      q=q, source=source, state=state, on=counts.get(True, 0), off=counts.get(False, 0),
                      refreshing=_refreshing.is_set(), msg=request.query_params.get("msg", ""))


@app.post("/brokers/{bid}/toggle")
def broker_toggle(request: HttpRequest, bid: int):
    with session_scope() as s:
        b = s.get(Broker, bid)
        if b:
            b.enabled, b.user_modified = not b.enabled, True
    return RedirectResponse(request.headers.get("referer") or "/brokers", status_code=303)


@app.post("/brokers/bulk")
def broker_bulk(request: HttpRequest, action: str = Form(...), q: str = Form(""), source: str = Form("")):
    with session_scope() as s:
        qry = select(Broker).where(Broker.method == "email", Broker.email != "")
        if q:
            qry = qry.where(or_(Broker.name.ilike(f"%{q}%"), Broker.email.ilike(f"%{q}%")))
        if source:
            qry = qry.where(Broker.source == source)
        n = 0
        for b in s.scalars(qry):
            b.enabled, b.user_modified = action == "enable", True
            n += 1
    return back("/brokers?" + urlencode({"q": q, "source": source}), f"{n} broker aggiornati")


@app.post("/brokers/save")
def broker_save(bid: int = Form(0), name: str = Form(...), email: str = Form(""), website: str = Form(""),
                privacy_url: str = Form(""), notes: str = Form(""), jurisdiction: str = Form("eu")):
    with session_scope() as s:
        b = s.get(Broker, bid) if bid else None
        if b is None:
            b = Broker(slug="manual-" + sources._slug(name) + "-" + secrets.token_hex(3), source="manual",
                       categories=["manual"], countries=[], user_modified=True)
            s.add(b)
        b.name, b.email, b.website, b.privacy_url, b.notes = name.strip(), email.strip().lower(), \
            website.strip(), privacy_url.strip(), notes
        b.jurisdiction = jurisdiction
        b.method = "email" if b.email else "webform"
        b.enabled = bool(b.email)
        b.bounce_count = 0
    return back("/brokers?" + urlencode({"q": name}), "Broker salvato")


_refreshing = threading.Event()


def _refresh_worker():
    try:
        recs = sources.fetch_all()
        with session_scope() as s:
            broker_db.sync(s, recs, "fonti online")
    except Exception as e:
        with session_scope() as s:
            log_event(s, f"Aggiornamento database broker fallito: {e}", "error")
    finally:
        _refreshing.clear()


@app.post("/brokers/refresh")
def broker_refresh():
    if not _refreshing.is_set():
        _refreshing.set()
        threading.Thread(target=_refresh_worker, daemon=True).start()
    return back("/brokers", "Aggiornamento avviato: ricarica la pagina tra un minuto")


# ------------------------------------------------------------------ caselle email

PRESETS = {
    "gmail": ("smtp.gmail.com", 465, "ssl", "imap.gmail.com", 993, "INBOX,[Gmail]/Spam"),
    "outlook": ("smtp-mail.outlook.com", 587, "starttls", "outlook.office365.com", 993, "INBOX,Junk"),
    "icloud": ("smtp.mail.me.com", 587, "starttls", "imap.mail.me.com", 993, "INBOX,Junk"),
    "yahoo": ("smtp.mail.yahoo.com", 465, "ssl", "imap.mail.yahoo.com", 993, "INBOX,Bulk"),
    "libero": ("smtp.libero.it", 465, "ssl", "imapmail.libero.it", 993, "INBOX,Spam"),
    "virgilio": ("out.virgilio.it", 465, "ssl", "in.virgilio.it", 993, "INBOX,Spam"),
    "aruba": ("smtps.aruba.it", 465, "ssl", "imaps.aruba.it", 993, "INBOX,Spam"),
    "tiscali": ("smtp.tiscali.it", 465, "ssl", "imap.tiscali.it", 993, "INBOX,Spam"),
    "fastmail": ("smtp.fastmail.com", 465, "ssl", "imap.fastmail.com", 993, "INBOX,Spam"),
}


_APP_PW = re.compile(r"^[a-zA-Z]{4}( [a-zA-Z]{4}){3}$")


def normalize_password(pw: str) -> str:
    """Le password per le app di Google/Apple si copiano come 'abcd efgh ijkl mnop': togliamo gli spazi."""
    pw = pw.strip()
    return pw.replace(" ", "") if _APP_PW.match(pw) else pw


@app.get("/identities", response_class=HTMLResponse)
def identities(request: HttpRequest):
    with session_scope() as s:
        rows = s.scalars(select(Identity).order_by(Identity.id)).all()
        return render(request, "identities.html", rows=rows, msg=request.query_params.get("msg", ""))


@app.get("/identities/new", response_class=HTMLResponse)
@app.get("/identities/{iid}/edit", response_class=HTMLResponse)
def identity_form(request: HttpRequest, iid: int = 0):
    with session_scope() as s:
        i = s.get(Identity, iid) if iid else None
        return render(request, "identity_form.html", i=i, presets=PRESETS,
                      msg=request.query_params.get("msg", ""))


@app.post("/identities/save")
def identity_save(
    iid: int = Form(0), label: str = Form(...), full_name: str = Form(...), email: str = Form(...),
    extra_identifiers: str = Form(""), smtp_host: str = Form(""), smtp_port: int = Form(465),
    smtp_security: str = Form("ssl"), smtp_user: str = Form(""), smtp_password: str = Form(""),
    imap_host: str = Form(""), imap_port: int = Form(993), imap_user: str = Form(""),
    imap_password: str = Form(""), imap_same_password: str = Form(""), imap_folders: str = Form("INBOX"),
    interval_months: int = Form(6), daily_limit: int = Form(80), enabled: str = Form(""),
    start_now: str = Form(""),
):
    with session_scope() as s:
        i = s.get(Identity, iid) if iid else None
        dup = s.scalars(select(Identity).where(Identity.email == email.strip().lower())).first()
        if dup and (i is None or dup.id != i.id):
            return back(f"/identities/{iid}/edit" if iid else "/identities/new", "Questa email è già configurata")
        new = i is None
        if new:
            i = Identity()
            s.add(i)
        i.label, i.full_name, i.email = label.strip(), full_name.strip(), email.strip().lower()
        i.extra_identifiers = extra_identifiers.strip()
        i.smtp_host, i.smtp_port, i.smtp_security = smtp_host.strip(), smtp_port, smtp_security
        i.smtp_user = smtp_user.strip()
        smtp_password, imap_password = normalize_password(smtp_password), normalize_password(imap_password)
        if smtp_password:
            i.smtp_password_enc = encrypt(smtp_password)
        i.imap_host, i.imap_port, i.imap_user = imap_host.strip(), imap_port, imap_user.strip()
        if imap_same_password:
            i.imap_password_enc = i.smtp_password_enc
        elif imap_password:
            i.imap_password_enc = encrypt(imap_password)
        i.imap_folders = imap_folders.strip() or "INBOX"
        changed_interval = i.interval_months != interval_months
        i.interval_months = 3 if interval_months == 3 else 6 if interval_months == 6 else max(1, interval_months)
        i.daily_limit = max(1, min(daily_limit, 2000))
        i.enabled = bool(enabled)
        i.last_error = ""
        if changed_interval and i.last_campaign_at:
            i.next_campaign_at = scheduler.add_months(i.last_campaign_at, i.interval_months)
        s.flush()
        if start_now and scheduler.smtp_ready(i):
            scheduler.start_campaign(s, i, "manual")
        log_event(s, f"Casella {i.label} ({i.email}) {'creata' if new else 'modificata'}")
    return back("/identities", "Salvato" + (" e campagna avviata" if start_now else ""))


@app.post("/identities/{iid}/delete")
def identity_delete(iid: int):
    with session_scope() as s:
        i = s.get(Identity, iid)
        if i:
            log_event(s, f"Casella {i.label} ({i.email}) eliminata con tutto lo storico")
            s.delete(i)
    return back("/identities", "Eliminata")


@app.post("/identities/{iid}/start")
def identity_start(iid: int):
    with session_scope() as s:
        i = s.get(Identity, iid)
        if not i or not scheduler.smtp_ready(i):
            return back("/identities", "Configura prima l'SMTP")
        c = scheduler.start_campaign(s, i, "manual")
        n = c.total
    return back("/", f"Campagna avviata: {n} richieste in coda")


@app.post("/identities/{iid}/pause")
def identity_pause(iid: int):
    with session_scope() as s:
        i = s.get(Identity, iid)
        if i:
            i.enabled = not i.enabled
            log_event(s, f"Casella {i.label} {'riattivata' if i.enabled else 'in pausa'}")
    return back("/identities")


@app.post("/identities/{iid}/test")
def identity_test(iid: int):
    out = []
    with session_scope() as s:
        i = s.get(Identity, iid)
        if not i:
            raise HTTPException(404)
        try:
            c = mailer.smtp_connect(i)
            c.noop()
            c.quit()
            out.append("SMTP OK")
        except Exception as e:
            out.append(f"SMTP ERRORE: {e}")
        if scheduler.imap_ready(i):
            try:
                c = inbox.imap_connect(i)
                typ, folders = c.list()
                c.logout()
                out.append(f"IMAP OK ({len(folders or [])} cartelle)")
            except Exception as e:
                out.append(f"IMAP ERRORE: {e}")
        else:
            out.append("IMAP non configurato: le risposte non verranno lette")
    return back(f"/identities/{iid}/edit", " · ".join(out))


@app.get("/identities/{iid}/folders", response_class=PlainTextResponse)
def identity_folders(iid: int):
    with session_scope() as s:
        i = s.get(Identity, iid)
        c = inbox.imap_connect(i)
        typ, folders = c.list()
        c.logout()
    return "\n".join(f.decode(errors="replace") for f in folders or [])


@app.post("/identities/{iid}/check")
def identity_check(iid: int):
    st = scheduler.job_imap(iid)
    return back("/", f"Controllate {st['checked']} mail: {st['matched']} risposte abbinate, "
                     f"{st['unmatched']} da verificare")


@app.post("/identities/{iid}/test-send")
def identity_test_send(iid: int):
    """Invia la richiesta di esempio alla casella stessa, per vedere come arriva."""
    with session_scope() as s:
        i = s.get(Identity, iid)
        fake = SimpleNamespace(identity=i, broker=SimpleNamespace(name="Example Broker Inc."),
                               to_email=i.email, messages=[], sent_at=None)
        msg = mailer.build_message(s, fake)
        try:
            c = mailer.smtp_connect(i)
            c.send_message(msg)
            c.quit()
            m = f"Mail di prova inviata a {i.email}"
        except Exception as e:
            m = f"Invio fallito: {e}"
    return back(f"/identities/{iid}/edit", m)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: HttpRequest, identity: int = 0, reminder: int = 0):
    with session_scope() as s:
        i = s.get(Identity, identity) if identity else s.scalars(select(Identity)).first()
        if i is None:
            i = Identity(full_name="Mario Rossi", email="mario.rossi@example.com", extra_identifiers="")
        b = Broker(name="Example Broker Inc.")
        subj_t = get_setting(s, "reminder_subject" if reminder else "subject")
        body_t = get_setting(s, "reminder_body" if reminder else "body")
        subject = mailer.render(subj_t, i, b, utcnow())
        body = mailer.render(body_t, i, b, utcnow())
    return render(request, "preview.html", subject=subject, body=body, reminder=reminder)


# ------------------------------------------------------------------ mail non abbinate


@app.get("/unmatched", response_class=HTMLResponse)
def unmatched(request: HttpRequest):
    with session_scope() as s:
        rows = s.scalars(select(UnmatchedMail).order_by(UnmatchedMail.date.desc()).limit(200)).all()
        return render(request, "unmatched.html", rows=rows, msg=request.query_params.get("msg", ""))


@app.post("/unmatched/{uid}/assign")
def unmatched_assign(uid: int, broker_query: str = Form(...)):
    with session_scope() as s:
        u = s.get(UnmatchedMail, uid)
        if not u:
            raise HTTPException(404)
        like = f"%{broker_query.strip()}%"
        req = s.scalars(select(Request).join(Broker).where(
            Request.identity_id == u.identity_id, Request.sent_at.is_not(None),
            or_(Broker.name.ilike(like), Broker.email.ilike(like))).order_by(Request.sent_at.desc())).first()
        if not req:
            return back("/unmatched", f"Nessuna richiesta inviata a un broker che corrisponde a '{broker_query}'")
        import email.message as em
        msg = em.EmailMessage()
        msg["From"], msg["Subject"] = u.from_addr, u.subject
        cls = inbox.classify(msg, u.body)
        inbox.apply_reply(s, req, msg, u.body, cls)
        s.delete(u)
        rid = req.id
    return back(f"/requests/{rid}", "Mail abbinata")


@app.post("/unmatched/{uid}/delete")
def unmatched_delete(uid: int):
    with session_scope() as s:
        u = s.get(UnmatchedMail, uid)
        if u:
            s.delete(u)
    return back("/unmatched")


# ------------------------------------------------------------------ impostazioni


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: HttpRequest):
    with session_scope() as s:
        vals = {k: get_setting(s, k) for k in DEFAULTS}
    return render(request, "settings.html", vals=vals, msg=request.query_params.get("msg", ""))


@app.post("/settings")
async def settings_save(request: HttpRequest):
    form = await request.form()
    with session_scope() as s:
        if form.get("reset"):
            for k in ("subject", "body", "reminder_subject", "reminder_body"):
                set_setting(s, k, DEFAULTS[k])
            return back("/settings", "Testi ripristinati")
        for k in DEFAULTS:
            if k in form:
                set_setting(s, k, str(form[k]).replace("\r\n", "\n"))
    return back("/settings", "Impostazioni salvate")


@app.get("/events", response_class=HTMLResponse)
def events(request: HttpRequest):
    with session_scope() as s:
        rows = s.scalars(select(Event).order_by(Event.at.desc()).limit(500)).all()
        return render(request, "events.html", rows=rows)

