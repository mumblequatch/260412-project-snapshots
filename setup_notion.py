#!/usr/bin/env python3
"""One-time setup: create the Project Snapshots database and Project Overview page.

Usage:
    python setup_notion.py

Prereqs:
    1. Create an integration: https://www.notion.so/my-integrations
       Capabilities: Read content, Update content, Insert content.
    2. Copy the integration token into ~/.config/project-snapshots/config.json
       (copy config_example.json first).
    3. In Notion, open the page you want the database and overview to live under,
       click "..." > "Connections" > add your integration.
    4. Run this script. It will prompt for that parent page's ID or URL.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from notion_client import Client

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"

DEFAULT_PROJECTS = [
    "The Other City",
    "Tales from Ovid",
    "VO Business",
    "Cantoworks",
    "Benjamin Projects",
]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} does not exist.")
        print("Create it by copying config_example.json and filling in notion_token.")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def extract_page_id(raw: str) -> str:
    raw = raw.strip()
    # Notion URLs: https://www.notion.so/Name-<32hexchars>?pvs=... or with dashes
    m = re.search(r"([0-9a-fA-F]{32})", raw.replace("-", ""))
    if not m:
        print(f"ERROR: could not find a 32-char page ID in: {raw}")
        sys.exit(1)
    h = m.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def create_database(notion: Client, parent_page_id: str) -> tuple[str, str]:
    print("Creating 'Project Snapshots' database…")
    resp = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Project Snapshots"}}],
    )
    db_id = resp["id"]
    ds_id = resp["data_sources"][0]["id"]
    print(f"  database_id: {db_id}")
    print(f"  data_source_id: {ds_id}")
    print(f"  URL: {resp.get('url', '(no url)')}")

    print("Adding properties to data source…")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={
            "Project": {
                "select": {
                    "options": [{"name": p} for p in DEFAULT_PROJECTS],
                }
            },
            "Status": {"rich_text": {}},
            "Next Action": {"rich_text": {}},
            "Open Questions": {"rich_text": {}},
            "Latest Files": {"rich_text": {}},
            "Session Notes": {"rich_text": {}},
            "Timestamp": {"date": {}},
        },
    )
    return db_id, ds_id


def create_overview_page(notion: Client, parent_page_id: str) -> str:
    print("Creating 'Project Overview' page…")
    resp = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        properties={
            "title": [{"type": "text", "text": {"content": "Project Overview"}}]
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "No snapshots yet."}}
                    ]
                },
            }
        ],
    )
    pid = resp["id"]
    print(f"  overview_page_id: {pid}")
    print(f"  URL: {resp.get('url', '(no url)')}")
    return pid


def main() -> None:
    cfg = load_config()
    token = cfg.get("notion_token", "").strip()
    if not token or token.startswith("secret_xxxx"):
        print(f"ERROR: fill in notion_token in {CONFIG_PATH} first.")
        sys.exit(1)

    notion = Client(auth=token)

    raw = input("Paste the Notion parent page URL or ID: ")
    parent_id = extract_page_id(raw)
    print(f"Parent page ID: {parent_id}")

    try:
        notion.pages.retrieve(page_id=parent_id)
    except Exception as e:
        print(f"ERROR: cannot access that page. Did you share it with the integration? {e}")
        sys.exit(1)

    if cfg.get("database_id"):
        print(f"database_id already set ({cfg['database_id']}); skipping DB create.")
        db_id = cfg["database_id"]
    else:
        db_id, ds_id = create_database(notion, parent_id)
        cfg["database_id"] = db_id
        cfg["data_source_id"] = ds_id
        save_config(cfg)

    if cfg.get("overview_page_id"):
        print(f"overview_page_id already set ({cfg['overview_page_id']}); skipping.")
    else:
        pid = create_overview_page(notion, parent_id)
        cfg["overview_page_id"] = pid
        save_config(cfg)

    print("\nDone. Config saved to", CONFIG_PATH)


if __name__ == "__main__":
    main()
