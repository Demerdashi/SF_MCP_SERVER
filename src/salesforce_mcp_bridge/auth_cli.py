"""One-shot browser login CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Authenticate the Salesforce MCP bridge via browser OAuth (PKCE)."
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to a .env file (defaults to discovering .env in cwd).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    settings = load_settings(env_file=args.env_file)
    auth = AuthManager(settings)
    tokens = asyncio.run(auth.login_interactive())
    print(
        json.dumps(
            {
                "ok": True,
                "instance_url": tokens.instance_url,
                "token_path": str(settings.token_path),
                "mcp_url": settings.mcp_url,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
