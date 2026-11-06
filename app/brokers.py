"""Sincronizzazione del database broker (file incluso o fonti online)."""
from __future__ import annotations

import json
import pathlib

from sqlalchemy import select

from .db import Broker, log_event

BUNDLED = pathlib.Path(__file__).resolve().parent.parent / "data" / "brokers.json"
FIELDS = ("name", "email", "website", "privacy_url", "address", "source", "jurisdiction",
          "method", "categories", "countries")


def sync(s, records: list[dict], origin: str) -> dict:
    """Inserisce/aggiorna i broker. Non tocca quelli manuali ne' le scelte dell'utente."""
    existing = {b.slug: b for b in s.scalars(select(Broker))}
    stats = {"added": 0, "updated": 0, "removed": 0}
    slugs = set()
    for r in records:
        slugs.add(r["slug"])
        b = existing.get(r["slug"])
        if b is None:
            b = Broker(slug=r["slug"], enabled=bool(r.get("default_enabled")))
            s.add(b)
            stats["added"] += 1
        else:
            stats["updated"] += 1
            if not b.user_modified and b.bounce_count == 0:
                b.enabled = bool(r.get("default_enabled"))
            b.removed_upstream = False
        for f in FIELDS:
            if f in r:
                setattr(b, f, r[f] if r[f] is not None else "")
    for slug, b in existing.items():
        if b.source != "manual" and slug not in slugs and not b.removed_upstream:
            b.removed_upstream = True
            if not b.user_modified:
                b.enabled = False
            stats["removed"] += 1
    log_event(s, f"Database broker aggiornato da {origin}: {stats['added']} nuovi, "
                 f"{stats['updated']} aggiornati, {stats['removed']} non piu' presenti nelle fonti")
    return stats


def load_bundled(s) -> None:
    if s.scalars(select(Broker.id)).first() is None and BUNDLED.exists():
        sync(s, json.loads(BUNDLED.read_text()), "file incluso")
