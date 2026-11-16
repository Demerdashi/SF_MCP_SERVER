#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json

from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import load_settings
from salesforce_mcp_bridge.sf_client import SalesforceMcpClient


def text_of(result) -> str:
    return "".join(getattr(b, "text", "") or "" for b in (result.content or []))


def required_fields(schema_payload: dict, object_name: str) -> list[str]:
    details = schema_payload.get("details") or {}
    obj = details.get(object_name) or details
    fields = obj.get("fields") if isinstance(obj, dict) else None
    if not fields and isinstance(details, dict):
        for value in details.values():
            if isinstance(value, dict) and isinstance(value.get("fields"), list):
                fields = value["fields"]
                break
    required: list[str] = []
    if isinstance(fields, list):
        for field in fields:
            if field.get("required") and field.get("createable", True):
                required.append(str(field.get("name")))
    return required


async def main() -> None:
    settings = load_settings()
    auth = AuthManager(settings)
    client = SalesforceMcpClient(settings, auth)
    try:
        pb = await client.call_tool(
            "soqlQuery",
            {
                "q": (
                    "SELECT Id, Name, IsStandard, IsActive FROM Pricebook2 "
                    "WHERE IsStandard = true LIMIT 5"
                )
            },
        )
        print("PRICEBOOK", text_of(pb))
        for obj in ["Account", "Contact", "Case", "Product2", "Task"]:
            result = await client.call_tool("getObjectSchema", {"objects": obj})
            payload = json.loads(text_of(result))
            print(obj, "required", required_fields(payload, obj))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
