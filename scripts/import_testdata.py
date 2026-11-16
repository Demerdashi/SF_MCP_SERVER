#!/usr/bin/env python3
"""Import testdata/*.csv into Salesforce via the Hosted MCP bridge."""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import load_settings
from salesforce_mcp_bridge.sf_client import SalesforceMcpClient

TESTDATA = ROOT / "testdata"
OUT_DIR = TESTDATA / "import_results"
HOLDING_ACCOUNT_NAME = "Imported People (MCP Test)"

COUNTRY_CODES = {
    "united states": "US",
    "usa": "US",
    "us": "US",
    "finland": "FI",
    "new zealand": "NZ",
    "germany": "DE",
    "france": "FR",
    "united kingdom": "GB",
    "uk": "GB",
    "canada": "CA",
    "australia": "AU",
    "brazil": "BR",
    "india": "IN",
    "japan": "JP",
    "netherlands": "NL",
    "spain": "ES",
    "italy": "IT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "ireland": "IE",
    "switzerland": "CH",
    "mexico": "MX",
    "singapore": "SG",
    "south korea": "KR",
    "china": "CN",
    "papua new guinea": "PG",
}


def text_of(result: Any) -> str:
    return "".join(getattr(b, "text", "") or "" for b in (result.content or []))


def parse_result(result: Any) -> dict[str, Any]:
    text = text_of(result)
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    payload: Any
    try:
        payload = json.loads(text) if text.strip().startswith(("{", "[")) else {"raw": text}
    except json.JSONDecodeError:
        payload = {"raw": text}
    if is_error:
        return {"ok": False, "error": payload}
    if isinstance(payload, dict) and payload.get("success") is False:
        return {"ok": False, "error": payload}
    if isinstance(payload, dict) and payload.get("errors"):
        return {"ok": False, "error": payload}
    return {"ok": True, "data": payload}


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    if len(key) == 2 and key.isalpha():
        return key.upper()
    return COUNTRY_CODES.get(key)


def truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class Importer:
    def __init__(self, client: SalesforceMcpClient) -> None:
        self.client = client
        self.pricebook_id: str | None = None
        self.people_account_id: str | None = None
        self.summary: dict[str, Any] = {}

    async def create(self, sobject: str, body: dict[str, Any]) -> dict[str, Any]:
        # Drop Nones
        payload = {k: v for k, v in body.items() if v is not None and v != ""}
        last_error: Any = None
        for attempt in range(1, 4):
            try:
                result = await self.client.call_tool(
                    "createSobjectRecord",
                    {"sobject-name": sobject, "body": payload},
                )
                parsed = parse_result(result)
                if parsed["ok"]:
                    data = parsed["data"]
                    return {"ok": True, "id": data.get("id"), "raw": data}

                # Reuse an existing match when Salesforce duplicate rules block insert.
                existing_id = _duplicate_match_id(parsed.get("error"))
                if existing_id:
                    return {
                        "ok": True,
                        "id": existing_id,
                        "raw": {"id": existing_id, "reused": True},
                        "reused": True,
                    }
                return {"ok": False, "error": parsed["error"], "body": payload}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                print(f"  retry {attempt}/3 after error: {last_error[:160]}")
                await self.client.close()
                await asyncio.sleep(min(2 * attempt, 6))
        return {"ok": False, "error": last_error, "body": payload}

    async def update(self, sobject: str, record_id: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in body.items() if v is not None and v != ""}
        result = await self.client.call_tool(
            "updateSobjectRecord",
            {"sobject-name": sobject, "id": record_id, "body": payload},
        )
        return parse_result(result)

    async def soql(self, q: str) -> dict[str, Any]:
        result = await self.client.call_tool("soqlQuery", {"q": q})
        return parse_result(result)

    async def ensure_pricebook(self) -> str | None:
        found = await self.soql(
            "SELECT Id, Name, IsStandard, IsActive FROM Pricebook2 WHERE IsStandard = true LIMIT 1"
        )
        if not found["ok"]:
            return None
        records = (found["data"] or {}).get("records") or []
        if not records:
            return None
        pb_id = records[0]["Id"]
        if not records[0].get("IsActive"):
            await self.update("Pricebook2", pb_id, {"IsActive": True})
        self.pricebook_id = pb_id
        return pb_id

    async def ensure_people_account(self) -> str:
        if self.people_account_id:
            return self.people_account_id
        q = (
            "SELECT Id FROM Account WHERE Name = "
            f"'{HOLDING_ACCOUNT_NAME.replace(chr(39), chr(39)+chr(39))}' LIMIT 1"
        )
        found = await self.soql(q)
        records = ((found.get("data") or {}).get("records") or []) if found.get("ok") else []
        if records:
            self.people_account_id = records[0]["Id"]
            return self.people_account_id
        created = await self.create(
            "Account",
            {
                "Name": HOLDING_ACCOUNT_NAME,
                "Description": "Holding account for people-100.csv MCP import",
            },
        )
        if not created["ok"]:
            raise RuntimeError(f"Could not create holding account: {created['error']}")
        self.people_account_id = created["id"]
        return self.people_account_id

    async def import_file(self, filename: str, handler) -> None:
        path = TESTDATA / filename
        if not path.exists():
            self.summary[filename] = {"ok": 0, "fail": 0, "skipped": True, "reason": "missing"}
            return
        rows = read_csv(path)
        results: list[dict[str, Any]] = []
        ok = fail = 0
        print(f"\n=== {filename} ({len(rows)} rows) ===")
        for index, row in enumerate(rows, start=1):
            try:
                item = await handler(row)
            except Exception as exc:  # noqa: BLE001
                item = {"ok": False, "error": str(exc)}
            results.append(item)
            if item.get("ok"):
                ok += 1
                if ok <= 3 or ok == len(rows) or index % 50 == 0:
                    print(f"  OK [{index}/{len(rows)}] {item.get('label')} -> {item.get('id')}")
            else:
                fail += 1
                if fail <= 5 or index % 50 == 0:
                    print(f"  FAIL [{index}/{len(rows)}] {item.get('label')} -> {item.get('error')}")
        out = OUT_DIR / f"{path.stem}_created.json"
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        self.summary[filename] = {"ok": ok, "fail": fail, "total": len(rows), "out": str(out)}
        print(f"SUMMARY {filename}: {ok}/{len(rows)} ok, {fail} fail")

    async def customers(self, row: dict[str, str]) -> dict[str, Any]:
        company = clean(row.get("Company")) or f"Customer {row.get('Customer Id') or row.get('Index')}"
        customer_id = clean(row.get("Customer Id")) or clean(row.get("Index")) or "row"
        account_name = truncate(f"{company} [{customer_id}]", 255)
        label = f"{clean(row.get('First Name'))} {clean(row.get('Last Name'))}".strip()
        cc = country_code(clean(row.get("Country")))
        account = await self.create(
            "Account",
            {
                "Name": account_name,
                "Phone": clean(row.get("Phone 1")),
                "Website": clean(row.get("Website")),
                "BillingCity": clean(row.get("City")),
                "BillingCountryCode": cc,
                "Description": f"Imported from customers-100.csv id={customer_id}",
            },
        )
        if not account["ok"]:
            # retry without country code
            account = await self.create(
                "Account",
                {
                    "Name": account_name,
                    "Phone": clean(row.get("Phone 1")),
                    "Website": clean(row.get("Website")),
                    "BillingCity": clean(row.get("City")),
                    "Description": f"Imported from customers-100.csv id={customer_id}",
                },
            )
        if not account["ok"]:
            return {"ok": False, "label": label or company, "error": account["error"]}
        contact = await self.create(
            "Contact",
            {
                "AccountId": account["id"],
                "FirstName": truncate(clean(row.get("First Name")), 40),
                "LastName": truncate(clean(row.get("Last Name")) or "Unknown", 80),
                "Email": clean(row.get("Email")),
                "Phone": clean(row.get("Phone 1")),
                "OtherPhone": clean(row.get("Phone 2")),
                "MailingCity": clean(row.get("City")),
                "MailingCountryCode": cc,
            },
        )
        if not contact["ok"]:
            contact = await self.create(
                "Contact",
                {
                    "AccountId": account["id"],
                    "FirstName": truncate(clean(row.get("First Name")), 40),
                    "LastName": truncate(clean(row.get("Last Name")) or "Unknown", 80),
                    "Email": clean(row.get("Email")),
                    "Phone": clean(row.get("Phone 1")),
                },
            )
        return {
            "ok": contact["ok"],
            "label": label or company,
            "id": contact.get("id"),
            "accountId": account["id"],
            "error": contact.get("error"),
        }

    async def enterprise_customers(self, row: dict[str, str]) -> dict[str, Any]:
        company = clean(row.get("Company Name")) or f"Enterprise {row.get('Customer ID')}"
        customer_id = clean(row.get("Customer ID")) or clean(row.get("Index")) or "row"
        account_name = truncate(f"{company} [{customer_id}]", 255)
        label = company
        cc = country_code(clean(row.get("Country")))
        desc_parts = [
            clean(row.get("Company Description")),
            clean(row.get("Notes")),
            f"CustomerID={customer_id}",
            f"Support={row.get('Support Level')}",
            f"ContractValue={row.get('Contract Value')}",
        ]
        account = await self.create(
            "Account",
            {
                "Name": account_name,
                "Website": clean(row.get("Website")),
                "Phone": clean(row.get("Phone Primary")),
                "BillingCity": clean(row.get("City")),
                "BillingCountryCode": cc,
                "BillingStreet": truncate(clean(row.get("Address")), 255),
                "NumberOfEmployees": _to_int(row.get("Number of Employees")),
                "AnnualRevenue": _to_float(row.get("Annual Revenue")),
                "Description": truncate(" | ".join(p for p in desc_parts if p), 32000),
            },
        )
        if not account["ok"]:
            account = await self.create(
                "Account",
                {
                    "Name": account_name,
                    "Website": clean(row.get("Website")),
                    "Phone": clean(row.get("Phone Primary")),
                    "BillingCity": clean(row.get("City")),
                    "Description": truncate(company, 255),
                },
            )
        if not account["ok"]:
            return {"ok": False, "label": label, "error": account["error"]}

        contact = await self.create(
            "Contact",
            {
                "AccountId": account["id"],
                "FirstName": truncate(clean(row.get("Primary Contact First Name")), 40),
                "LastName": truncate(
                    clean(row.get("Primary Contact Last Name")) or "Unknown", 80
                ),
                "Email": clean(row.get("Email Primary")),
                "Phone": clean(row.get("Phone Primary")),
                "Department": clean(row.get("Department")),
                "MailingCity": clean(row.get("City")),
                "MailingCountryCode": cc,
            },
        )
        if not contact["ok"]:
            contact = await self.create(
                "Contact",
                {
                    "AccountId": account["id"],
                    "FirstName": truncate(clean(row.get("Primary Contact First Name")), 40),
                    "LastName": truncate(
                        clean(row.get("Primary Contact Last Name")) or "Unknown", 80
                    ),
                    "Email": clean(row.get("Email Primary")),
                    "Phone": clean(row.get("Phone Primary")),
                },
            )
        return {
            "ok": contact["ok"],
            "label": label,
            "id": contact.get("id"),
            "accountId": account["id"],
            "error": contact.get("error"),
        }

    async def organizations(self, row: dict[str, str]) -> dict[str, Any]:
        base_name = clean(row.get("Name")) or f"Org {row.get('Organization Id')}"
        org_id = clean(row.get("Organization Id")) or clean(row.get("Index")) or "row"
        name = truncate(f"{base_name} [{org_id}]", 255)
        cc = country_code(clean(row.get("Country")))
        body = {
            "Name": name,
            "Website": clean(row.get("Website")),
            "BillingCountryCode": cc,
            "NumberOfEmployees": _to_int(row.get("Number of employees")),
            "Description": truncate(
                " | ".join(
                    p
                    for p in [
                        clean(row.get("Description")),
                        f"Founded={row.get('Founded')}",
                        f"Industry={row.get('Industry')}",
                        f"OrgId={org_id}",
                    ]
                    if p
                ),
                32000,
            ),
        }
        created = await self.create("Account", body)
        if not created["ok"]:
            body.pop("BillingCountryCode", None)
            created = await self.create("Account", body)
        return {
            "ok": created["ok"],
            "label": name,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def messy_companies(self, row: dict[str, str]) -> dict[str, Any]:
        raw_name = clean(row.get("Raw Company Name")) or f"Company {row.get('Row ID')}"
        base_name = re.sub(r"\s+", " ", raw_name).strip(" ,.")
        row_id = clean(row.get("Row ID")) or clean(row.get("Index")) or "row"
        name = truncate(f"{base_name} [{row_id}]", 255)
        cc = country_code(clean(row.get("Country")))
        body = {
            "Name": name,
            "Website": clean(row.get("Website")),
            "BillingCountryCode": cc,
            "NumberOfEmployees": _to_int(
                re.sub(r"[^\d]", "", clean(row.get("Employee Count")) or "") or None
            ),
            "Description": truncate(
                " | ".join(
                    p
                    for p in [
                        clean(row.get("Company Description")),
                        clean(row.get("CRM Notes")),
                        f"Source={row.get('Source System')}",
                        f"Stage={row.get('Lifecycle Stage')}",
                        f"RowID={row_id}",
                    ]
                    if p
                ),
                32000,
            ),
        }
        created = await self.create("Account", body)
        if not created["ok"]:
            body.pop("BillingCountryCode", None)
            body.pop("NumberOfEmployees", None)
            created = await self.create("Account", body)
        return {
            "ok": created["ok"],
            "label": name,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def people(self, row: dict[str, str]) -> dict[str, Any]:
        account_id = await self.ensure_people_account()
        label = f"{clean(row.get('First Name'))} {clean(row.get('Last Name'))}".strip()
        created = await self.create(
            "Contact",
            {
                "AccountId": account_id,
                "FirstName": truncate(clean(row.get("First Name")), 40),
                "LastName": truncate(clean(row.get("Last Name")) or "Unknown", 80),
                "Email": clean(row.get("Email")),
                "Phone": clean(row.get("Phone")),
                "Title": clean(row.get("Job Title")),
                "Description": f"Imported from people-100.csv id={row.get('User Id')}",
            },
        )
        return {
            "ok": created["ok"],
            "label": label,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def products(self, row: dict[str, str]) -> dict[str, Any]:
        base = clean(row.get("Name")) or f"Product {row.get('Internal ID') or row.get('Index')}"
        code = clean(row.get("EAN")) or clean(row.get("Internal ID")) or clean(row.get("Index"))
        name = truncate(f"{base} [{code}]", 255)
        product = await self.create(
            "Product2",
            {
                "Name": name,
                "Description": truncate(clean(row.get("Description")), 4000),
                "ProductCode": truncate(code, 255),
                "IsActive": True,
            },
        )
        if not product["ok"]:
            product = await self.create(
                "Product2",
                {"Name": name, "IsActive": True},
            )
        if not product["ok"]:
            return {"ok": False, "label": name, "error": product["error"]}

        entry_id = None
        price = _to_float(row.get("Price"))
        if self.pricebook_id and price is not None:
            entry = await self.create(
                "PricebookEntry",
                {
                    "Pricebook2Id": self.pricebook_id,
                    "Product2Id": product["id"],
                    "UnitPrice": price,
                    "IsActive": True,
                    "UseStandardPrice": False,
                },
            )
            if entry["ok"]:
                entry_id = entry["id"]
        return {
            "ok": True,
            "label": name,
            "id": product["id"],
            "pricebookEntryId": entry_id,
        }

    async def product_catalog_ai(self, row: dict[str, str]) -> dict[str, Any]:
        base = clean(row.get("Product Title")) or f"AI Product {row.get('Product ID')}"
        code = clean(row.get("Product ID")) or clean(row.get("Index"))
        name = truncate(f"{base} [{code}]", 255)
        created = await self.create(
            "Product2",
            {
                "Name": name,
                "Description": truncate(clean(row.get("Raw Description")), 4000),
                "ProductCode": truncate(code, 255),
                "IsActive": True,
            },
        )
        if not created["ok"]:
            created = await self.create(
                "Product2",
                {
                    "Name": name,
                    "ProductCode": truncate(code, 255),
                    "IsActive": True,
                },
            )
        return {
            "ok": created["ok"],
            "label": name,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def offers(self, row: dict[str, str]) -> dict[str, Any]:
        ean = clean(row.get("ean")) or f"offer-{row.get('index')}"
        name = f"Offer {ean}"
        product = await self.create(
            "Product2",
            {
                "Name": truncate(name, 255),
                "ProductCode": truncate(ean, 255),
                "Description": f"Stock={row.get('stock')}; imported from offers-1000.csv",
                "IsActive": True,
            },
        )
        if not product["ok"]:
            return {"ok": False, "label": name, "error": product["error"]}
        entry_id = None
        price = _to_float(row.get("price"))
        if self.pricebook_id and price is not None:
            entry = await self.create(
                "PricebookEntry",
                {
                    "Pricebook2Id": self.pricebook_id,
                    "Product2Id": product["id"],
                    "UnitPrice": price,
                    "IsActive": True,
                    "UseStandardPrice": False,
                },
            )
            if entry["ok"]:
                entry_id = entry["id"]
        return {
            "ok": True,
            "label": name,
            "id": product["id"],
            "pricebookEntryId": entry_id,
        }

    async def support_tickets(self, row: dict[str, str]) -> dict[str, Any]:
        subject = clean(row.get("Ticket Subject")) or f"Ticket {row.get('Ticket ID')}"
        priority = _map_priority(clean(row.get("Priority Hint")))
        created = await self.create(
            "Case",
            {
                "Subject": truncate(subject, 255),
                "Description": truncate(
                    "\n\n".join(
                        p
                        for p in [
                            clean(row.get("Ticket Text")),
                            f"Ticket ID: {row.get('Ticket ID')}",
                            f"Customer Plan: {row.get('Customer Plan')}",
                        ]
                        if p
                    ),
                    32000,
                ),
                "Priority": priority,
                "Origin": "Web",
                "Status": "New",
            },
        )
        return {
            "ok": created["ok"],
            "label": subject,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def reviews(self, row: dict[str, str]) -> dict[str, Any]:
        product = clean(row.get("Product Name")) or "Unknown product"
        subject = truncate(f"Review: {product} ({row.get('Rating')}/5)", 255)
        created = await self.create(
            "Case",
            {
                "Subject": subject,
                "Description": truncate(
                    "\n\n".join(
                        p
                        for p in [
                            clean(row.get("Review Text")),
                            f"Review ID: {row.get('Review ID')}",
                            f"Rating: {row.get('Rating')}",
                        ]
                        if p
                    ),
                    32000,
                ),
                "Origin": "Web",
                "Status": "New",
                "Priority": "Medium",
            },
        )
        return {
            "ok": created["ok"],
            "label": subject,
            "id": created.get("id"),
            "error": created.get("error"),
        }

    async def research_questions(self, row: dict[str, str]) -> dict[str, Any]:
        entity = clean(row.get("Entity Name")) or "Research"
        subject = truncate(f"Research: {entity}", 255)
        created = await self.create(
            "Task",
            {
                "Subject": subject,
                "Description": truncate(
                    "\n\n".join(
                        p
                        for p in [
                            clean(row.get("Research Question")),
                            f"Domain: {row.get('Domain')}",
                            f"Source: {row.get('Source URL')}",
                            f"Research ID: {row.get('Research ID')}",
                        ]
                        if p
                    ),
                    32000,
                ),
                "Status": "Not Started",
                "Priority": "Normal",
            },
        )
        return {
            "ok": created["ok"],
            "label": subject,
            "id": created.get("id"),
            "error": created.get("error"),
        }


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(re.sub(r"[^\d.]", "", str(value)) or "nan"))
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(value)))
    except Exception:
        return None


def _map_priority(value: str | None) -> str:
    if not value:
        return "Medium"
    lowered = value.lower()
    if "urgent" in lowered or "critical" in lowered or "high" in lowered:
        return "High"
    if "low" in lowered:
        return "Low"
    return "Medium"


def _duplicate_match_id(error: Any) -> str | None:
    """Extract a matched record Id from a Salesforce DUPLICATES_DETECTED payload."""
    items: list[Any]
    if isinstance(error, list):
        items = error
    elif isinstance(error, dict):
        items = [error]
    else:
        return None

    for item in items:
        if not isinstance(item, dict):
            continue
        dup = item.get("duplicateResult") or {}
        for match in dup.get("matchResults") or []:
            for record in match.get("matchRecords") or []:
                rec = record.get("record") or {}
                rec_id = rec.get("Id")
                if rec_id:
                    return str(rec_id)
    return None


FILE_HANDLERS = {
    "customers-100.csv": "customers",
    "enterprise-customers-100.csv": "enterprise_customers",
    "organizations-100.csv": "organizations",
    "messy-company-data-100.csv": "messy_companies",
    "people-100.csv": "people",
    "products-100.csv": "products",
    "product-catalog-ai-100.csv": "product_catalog_ai",
    "offers-1000.csv": "offers",
    "support-tickets-100.csv": "support_tickets",
    "customer-reviews-100.csv": "reviews",
    "research-questions-ai-100.csv": "research_questions",
}


async def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    selected = args or list(FILE_HANDLERS.keys())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    auth = AuthManager(settings)
    client = SalesforceMcpClient(settings, auth)
    importer = Importer(client)
    try:
        await importer.ensure_pricebook()
        print("Pricebook:", importer.pricebook_id)

        for filename in selected:
            handler_name = FILE_HANDLERS.get(filename)
            if not handler_name:
                print(f"Skipping unknown file: {filename}")
                continue
            handler = getattr(importer, handler_name)
            await importer.import_file(filename, handler)
    finally:
        await client.close()

    summary_path = OUT_DIR / "summary.json"
    # Merge with prior summary if present.
    prior: dict[str, Any] = {}
    if summary_path.exists():
        try:
            prior = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior = {}
    prior.update(importer.summary)
    summary_path.write_text(json.dumps(prior, indent=2), encoding="utf-8")
    print("\n==== FINAL SUMMARY ====")
    for name, stats in prior.items():
        print(name, stats)
    print("Wrote", summary_path)


if __name__ == "__main__":
    asyncio.run(main())
