#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "testdata" / "leads_sample.json"
CSV_PATH = ROOT / "testdata" / "leads_sample.csv"

leads = json.loads(JSON_PATH.read_text(encoding="utf-8"))
for lead in leads:
    state = lead.pop("State", None)
    country = lead.pop("Country", None)
    if state:
        lead["StateCode"] = state
    if country:
        lead["CountryCode"] = "US"

JSON_PATH.write_text(json.dumps(leads, indent=2) + "\n", encoding="utf-8")

fields = [
    "FirstName",
    "LastName",
    "Company",
    "Title",
    "Email",
    "Phone",
    "LeadSource",
    "Status",
    "Industry",
    "City",
    "StateCode",
    "CountryCode",
    "Website",
    "Description",
]
with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for lead in leads:
        writer.writerow({key: lead.get(key, "") for key in fields})

print(f"Updated {len(leads)} leads with StateCode/CountryCode")
