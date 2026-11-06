"""Scarica e normalizza gli elenchi pubblici di data broker (solo stdlib)."""
from __future__ import annotations

import csv
import io
import json
import re
import tarfile
import urllib.request

CPPA_CSV = "https://cppa.ca.gov/data_broker_registry/registry.csv"
DATENANFRAGEN_TGZ = "https://codeload.github.com/datenanfragen/data/tar.gz/refs/heads/master"

# Categorie datarequests.org considerate "data broker"
DD_CATEGORIES = {"ads", "addresses", "credit agency"}
# Paesi per cui il broker e' rilevante per un residente in Italia
RELEVANT = {"all", "it", "eu"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UA = "oblivion/1.0 (+https://github.com/Hooteo/oblivion)"


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL fissi
        return r.read()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def _first_email(text: str | None) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0).strip(".").lower() if m else ""


def _url(text: str | None) -> str:
    t = (text or "").strip()
    if t and not t.startswith(("http://", "https://")):
        t = "https://" + t
    return t if "." in t else ""


def fetch_cppa() -> list[dict]:
    raw = _get(CPPA_CSV).decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        name = (row.get("Data broker name:") or "").strip()
        if not name:
            continue
        dba = (row.get("Doing Business As (DBA), if applicable:") or "").strip()
        email = _first_email(row.get("Data broker primary contact email address:"))
        privacy_url = ""
        for k, v in row.items():
            if k and k.startswith("Data broker's primary website that contains details"):
                privacy_url = _url(v)
        fcra = (row.get("The data broker or any of its subsidiaries is regulated by the federal Fair Credit Reporting Act") or "").lower() == "yes"
        cats = ["us-registry"] + (["fcra"] if fcra else [])
        out.append({
            "slug": "cppa-" + (row.get("Broker ID") or _slug(name)).strip(),
            "name": f"{name} ({dba})" if dba and dba.lower() != name.lower() else name,
            "email": email,
            "website": _url(row.get("Data broker primary website:")),
            "privacy_url": privacy_url,
            "source": "cppa",
            "jurisdiction": "us",
            "categories": cats,
            "countries": ["us"],
            "method": "email" if email else "webform",
            "default_enabled": bool(email),
        })
    return out


def fetch_datenanfragen() -> list[dict]:
    raw = _get(DATENANFRAGEN_TGZ, timeout=120)
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar:
            if not (m.isfile() and "/companies/" in m.name and m.name.endswith(".json")):
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            try:
                d = json.load(f)
            except ValueError:
                continue
            cats = set(d.get("categories") or [])
            if not cats & DD_CATEGORIES:
                continue
            countries = d.get("relevant-countries") or ["all"]
            email = _first_email(d.get("email"))
            medium = d.get("suggested-transport-medium") or ("email" if email else "letter")
            relevant = bool(set(countries) & RELEVANT)
            enabled = bool(email) and relevant and "credit agency" not in cats
            out.append({
                "slug": "dd-" + d["slug"],
                "name": d.get("name") or d["slug"],
                "email": email,
                "website": _url(d.get("web")),
                "privacy_url": _url((d.get("sources") or [""])[0]),
                "source": "datarequests",
                "jurisdiction": "eu",
                "categories": sorted(cats),
                "countries": countries,
                "method": "email" if email and medium != "webform" else medium,
                "address": d.get("address") or "",
                "default_enabled": enabled,
            })
    return out


def fetch_all() -> list[dict]:
    seen: dict[str, dict] = {}
    result: list[dict] = []
    for b in fetch_datenanfragen() + fetch_cppa():
        key = b["email"] or b["slug"]
        if key in seen:
            prev = seen[key]
            prev["categories"] = sorted(set(prev["categories"]) | set(b["categories"]))
            prev["countries"] = sorted(set(prev["countries"]) | set(b["countries"]))
            prev["default_enabled"] = prev["default_enabled"] or b["default_enabled"]
            continue
        seen[key] = b
        result.append(b)
    result.sort(key=lambda b: b["name"].lower())
    return result
