#!/usr/bin/env python3
"""One-time setup: create the Project Snapshots database, Project Overview page,
and the three Project Task List (PTL) databases: Projects, Subprojects, PTL.

Usage:
    python setup_notion.py

Prereqs:
    1. Create an integration: https://www.notion.so/my-integrations
       Capabilities: Read content, Update content, Insert content.
    2. Copy the integration token into ~/.config/project-snapshots/config.json
       (copy config_example.json first).
    3. In Notion, open the page you want the databases and overview to live under,
       click "..." > "Connections" > add your integration.
    4. Run this script. It will prompt for that parent page's ID or URL.

Idempotent: if an ID is already in config, that DB/page isn't recreated.
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
    return pid


def create_projects_db(notion: Client, parent_page_id: str) -> tuple[str, str]:
    print("Creating 'Projects' database…")
    resp = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Projects"}}],
    )
    db_id = resp["id"]
    ds_id = resp["data_sources"][0]["id"]
    print(f"  database_id: {db_id}")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={
            "Status": {
                "select": {
                    "options": [
                        {"name": "Active"},
                        {"name": "Paused"},
                        {"name": "Archived"},
                    ]
                }
            },
            "World": {"multi_select": {"options": []}},
            "Folder Path": {"rich_text": {}},
        },
    )
    return db_id, ds_id


def create_subprojects_db(
    notion: Client,
    parent_page_id: str,
    projects_ds_id: str,
) -> tuple[str, str]:
    print("Creating 'Subprojects' database…")
    resp = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Subprojects"}}],
    )
    db_id = resp["id"]
    ds_id = resp["data_sources"][0]["id"]
    print(f"  database_id: {db_id}")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={
            "Parent Project": {
                "relation": {
                    "data_source_id": projects_ds_id,
                    "type": "dual_property",
                    "dual_property": {},
                }
            },
            "Status": {
                "select": {
                    "options": [
                        {"name": "Active"},
                        {"name": "Paused"},
                        {"name": "Done"},
                        {"name": "Shelved"},
                    ]
                }
            },
            "Target Date": {"date": {}},
            "Notes": {"rich_text": {}},
        },
    )
    return db_id, ds_id


def create_ptl_db(
    notion: Client,
    parent_page_id: str,
    subprojects_ds_id: str,
    snapshots_ds_id: str,
) -> tuple[str, str]:
    print("Creating 'Project Task List' database…")
    resp = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Project Task List"}}],
    )
    db_id = resp["id"]
    ds_id = resp["data_sources"][0]["id"]
    print(f"  database_id: {db_id}")
    # Notion auto-creates a "Name" title property; we want the title prop named "Title".
    # The default title prop on a fresh DB is often called "Name". Rename it by updating.
    try:
        notion.data_sources.update(
            data_source_id=ds_id,
            properties={"Name": {"name": "Title"}},
        )
    except Exception as e:
        print(f"  (note: could not rename title property, leaving default: {e})")

    notion.data_sources.update(
        data_source_id=ds_id,
        properties={
            "Subproject": {
                "relation": {
                    "data_source_id": subprojects_ds_id,
                    "type": "dual_property",
                    "dual_property": {},
                }
            },
            "Order": {"number": {"format": "number"}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Todo"},
                        {"name": "Done"},
                        {"name": "Skipped"},
                    ]
                }
            },
            "Completed At": {"date": {}},
            "Completed In Snapshot": {
                "relation": {
                    "data_source_id": snapshots_ds_id,
                    "type": "dual_property",
                    "dual_property": {},
                }
            },
            "Notes": {"rich_text": {}},
        },
    )
    # Add Project rollup via Subproject → Parent Project (title).
    try:
        notion.data_sources.update(
            data_source_id=ds_id,
            properties={
                "Project": {
                    "rollup": {
                        "relation_property_name": "Subproject",
                        "rollup_property_name": "Parent Project",
                        "function": "show_original",
                    }
                }
            },
        )
    except Exception as e:
        print(f"  (note: could not auto-create Project rollup: {e})")
    return db_id, ds_id


def main() -> None:
    cfg = load_config()
    token = cfg.get("notion_token", "").strip()
    if not token or token.startswith("secret_xxxx"):
        print(f"ERROR: fill in notion_token in {CONFIG_PATH} first.")
        sys.exit(1)

    notion = Client(auth=token)

    parent_id = cfg.get("parent_page_id")
    if not parent_id:
        raw = input("Paste the Notion parent page URL or ID: ")
        parent_id = extract_page_id(raw)
    print(f"Parent page ID: {parent_id}")

    try:
        notion.pages.retrieve(page_id=parent_id)
    except Exception as e:
        print(f"ERROR: cannot access that page. Did you share it with the integration? {e}")
        sys.exit(1)
    cfg["parent_page_id"] = parent_id
    save_config(cfg)

    # 1. Project Snapshots DB
    if cfg.get("database_id"):
        print(f"database_id already set ({cfg['database_id']}); skipping Snapshots DB.")
    else:
        db_id, ds_id = create_database(notion, parent_id)
        cfg["database_id"] = db_id
        cfg["data_source_id"] = ds_id
        save_config(cfg)

    # 2. Overview page
    if cfg.get("overview_page_id"):
        print(f"overview_page_id already set ({cfg['overview_page_id']}); skipping.")
    else:
        pid = create_overview_page(notion, parent_id)
        cfg["overview_page_id"] = pid
        save_config(cfg)

    # 3. Projects DB
    if cfg.get("projects_database_id"):
        print(f"projects_database_id already set ({cfg['projects_database_id']}); skipping.")
    else:
        db_id, ds_id = create_projects_db(notion, parent_id)
        cfg["projects_database_id"] = db_id
        cfg["projects_data_source_id"] = ds_id
        save_config(cfg)

    # 4. Subprojects DB (needs projects_ds)
    if cfg.get("subprojects_database_id"):
        print(f"subprojects_database_id already set ({cfg['subprojects_database_id']}); skipping.")
    else:
        db_id, ds_id = create_subprojects_db(notion, parent_id, cfg["projects_data_source_id"])
        cfg["subprojects_database_id"] = db_id
        cfg["subprojects_data_source_id"] = ds_id
        save_config(cfg)

    # 5. PTL DB (needs subprojects_ds + snapshots_ds)
    if cfg.get("ptl_database_id"):
        print(f"ptl_database_id already set ({cfg['ptl_database_id']}); skipping.")
    else:
        db_id, ds_id = create_ptl_db(
            notion, parent_id,
            cfg["subprojects_data_source_id"],
            cfg["data_source_id"],
        )
        cfg["ptl_database_id"] = db_id
        cfg["ptl_data_source_id"] = ds_id
        save_config(cfg)

    print("\nDone. Config saved to", CONFIG_PATH)
    print("\nNext: run migrate_to_ptl.py to backfill Projects/Subprojects from existing Snapshots.")


if __name__ == "__main__":
    main()
