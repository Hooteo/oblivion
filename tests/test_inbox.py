import email
import email.policy
from datetime import timedelta

import pytest

from app import inbox
from app.db import Base, Broker, Campaign, Identity, Message, Request, engine, session_scope, utcnow


def mk(raw: str):
    return email.message_from_string(raw, policy=email.policy.default)


@pytest.fixture()
def setup():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope() as s:
        i = Identity(label="t", full_name="Mario Rossi", email="mario@example.it")
        b1 = Broker(slug="a", name="Acme Data Inc.", email="privacy@acmedata.com", website="https://acmedata.com")
        b2 = Broker(slug="b", name="Zeta Marketing LLC", email="privacy@zeta.co.uk", website="https://www.zeta.co.uk")
        b3 = Broker(slug="c", name="Omega Analytics", email="dsar@omega-analytics.com")
        s.add_all([i, b1, b2, b3])
        s.flush()
        c = Campaign(identity_id=i.id, total=3)
        s.add(c)
        s.flush()
        for n, b in enumerate([b1, b2, b3]):
            r = Request(campaign_id=c.id, identity_id=i.id, broker_id=b.id, to_email=b.email,
                        status="sent", sent_at=utcnow() - timedelta(days=2))
            s.add(r)
            s.add(Message(request=r, direction="out", message_id=f"<req{n}@example.it>", body="x"))
    yield


def test_base_domain():
    assert inbox.base_domain("privacy@mail.acme.com") == "acme.com"
    assert inbox.base_domain("https://www.zeta.co.uk/privacy") == "zeta.co.uk"


@pytest.mark.parametrize("text,expected", [
    ("Your personal data has been deleted from our systems.", "confirmed"),
    ("We have no records about you in our database.", "no_data"),
    ("To proceed, please verify your identity by clicking the link below.", "needs_action"),
    ("This right is only available to California residents.", "refused"),
    ("Thank you, we received your request. Ticket #123.", "replied"),
    ("I tuoi dati sono stati cancellati.", "confirmed"),
])
def test_classify(text, expected):
    assert inbox.classify(mk(f"From: a@b.com\nSubject: Re: request\n\n{text}"), text) == expected


def test_quoted_request_is_ignored():
    body = ("Thanks, ticket opened.\n\nOn Mon, 3 Nov 2026 at 10:00, Mario wrote:\n"
            "> If you hold no data about me, please let me know.\n"
            "Dear Acme Privacy / Data Protection Team,\nIf you hold no data about me please verify")
    assert inbox.classify(mk("From: a@b.com\nSubject: Re: x\n\n" + body), body) == "replied"


def test_match_by_in_reply_to(setup):
    with session_scope() as s:
        i = s.query(Identity).one()
        raw = "From: someone@other.com\nIn-Reply-To: <req1@example.it>\nSubject: Re\n\nok"
        r = inbox.match_request(s, i, mk(raw), raw.encode(), "ok")
        assert r.broker.name == "Zeta Marketing LLC"


def test_match_by_domain(setup):
    with session_scope() as s:
        i = s.query(Identity).one()
        raw = "From: Support <help@support.acmedata.com>\nSubject: Your request\n\nhi"
        r = inbox.match_request(s, i, mk(raw), raw.encode(), "hi")
        assert r.broker.slug == "a"


def test_shared_platform_needs_name(setup):
    with session_scope() as s:
        i = s.query(Identity).one()
        raw = "From: noreply@privacyportal.onetrust.com\nSubject: Your privacy request\n\nhello"
        assert inbox.match_request(s, i, mk(raw), raw.encode(), "hello") is None
        raw2 = "From: noreply@privacyportal.onetrust.com\nSubject: Omega Analytics privacy request\n\nhello"
        r = inbox.match_request(s, i, mk(raw2), raw2.encode(), "hello")
        assert r.broker.slug == "c"


def test_bounce_updates_status(setup):
    raw = ("From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
           "Subject: Delivery Status Notification (Failure)\n\n"
           "Your message to dsar@omega-analytics.com couldn't be delivered.\n")
    with session_scope() as s:
        i = s.query(Identity).one()
        st = {"checked": 0, "matched": 0, "unmatched": 0}
        inbox.handle_raw(s, i, raw.encode(), st)
        assert st["matched"] == 1
        r = s.query(Request).join(Broker).filter(Broker.slug == "c").one()
        assert r.status == "bounced" and r.broker.bounce_count == 1


def test_weaker_reply_does_not_downgrade(setup):
    with session_scope() as s:
        i = s.query(Identity).one()
        st = {"checked": 0, "matched": 0, "unmatched": 0}
        inbox.handle_raw(s, i, b"From: privacy@acmedata.com\nSubject: Re\n\nYour data has been deleted.", st)
        inbox.handle_raw(s, i, b"From: privacy@acmedata.com\nSubject: Survey\n\nHow did we do?", st)
        r = s.query(Request).join(Broker).filter(Broker.slug == "a").one()
        assert r.status == "confirmed" and r.reply_count == 2
