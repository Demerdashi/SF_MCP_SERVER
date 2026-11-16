#!/usr/bin/env python3
"""Create all leads from testdata/leads_sample.json via the Salesforce MCP bridge."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import load_settings
from salesforce_mcp_bridge.sf_client import SalesforceMcpClient


def _content_text(result) -> str:
    return "".join(getattr(block, "text", "") or "" for block in (result.content or []))


async def create_one(client: SalesforceMcpClient, lead: dict) -> dict:
    name = f"{lead.get('FirstName', '')} {lead.get('LastName', '')}".strip()
    try:
        result = await client.call_tool(
            "createSobjectRecord",
            {"sobject-name": "Lead", "body": lead},
        )
        text = _content_text(result)
        is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
        if is_error:
            return {"ok": False, "name": name, "error": text}
        data = json.loads(text) if text.strip().startswith("{") else {"raw": text}
        if data.get("success") is False or data.get("errors"):
            return {"ok": False, "name": name, "error": data}
        return {"ok": True, "name": name, "id": data.get("id"), "raw": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": name, "error": str(exc)}


async def main() -> None:
    leads_path = ROOT / "testdata" / "leads_sample.json"
    out_path = ROOT / "testdata" / "leads_created.json"
    leads = json.loads(leads_path.read_text(encoding="utf-8"))

    settings = load_settings()
    auth = AuthManager(settings)
    client = SalesforceMcpClient(settings, auth)
    results: list[dict] = []
    try:
        for lead in leads:
            row = await create_one(client, lead)
            results.append(row)
            if row["ok"]:
                print(f"OK  {row['name']} -> {row['id']}")
            else:
                print(f"FAIL {row['name']} -> {row.get('error')}")
    finally:
        await client.close()

    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r["ok"])
    print(f"SUMMARY {ok}/{len(results)} created")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
