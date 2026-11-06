#!/usr/bin/env python3
"""Rigenera data/brokers.json dalle fonti pubbliche.

Fonti:
  - California Privacy Protection Agency, registro ufficiale dei data broker
    https://cppa.ca.gov/data_broker_registry/  (dati pubblici)
  - datarequests.org / Datenanfragen.de, database aziende (CC0)
    https://github.com/datenanfragen/data      (categorie ads, addresses, credit agency)

Uso:  python scripts/build_brokers.py [--out data/brokers.json]
La stessa logica viene usata dal pulsante "Aggiorna database" della dashboard.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.sources import fetch_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/brokers.json")
    args = ap.parse_args()
    brokers = fetch_all()
    pathlib.Path(args.out).write_text(json.dumps(brokers, indent=1, ensure_ascii=False))
    with_mail = sum(1 for b in brokers if b["email"])
    print(f"{len(brokers)} broker scritti in {args.out} ({with_mail} con email)")


if __name__ == "__main__":
    main()
