#!/usr/bin/env python3
"""Populate a Project DB row's page body with scaffolding for manual linked views.

The Notion public API cannot create `linked_database` blocks (the in-page
filtered views you get from typing `/linked` in the Notion UI). So this
script lays down section headings + placeholder hints + database-mention
links. The user then adds a real linked view under each heading once, in
the Notion UI, and deletes the placeholder line.

Layout populated per Project page:
  1. Notes           — heading + empty paragraph for freeform notes.
  2. Subprojects     — heading + placeholder + Subprojects DB link.
  3. Tasks           — heading + placeholder + PTL DB link.
  4. Recent Snapshots— heading + placeholder + PS DB link.
  5. Done tasks      — collapsed toggle with placeholder + PTL DB link inside.

Usage:
    python new_project_page.py                 # populate all empty Projects
    python new_project_page.py --force         # wipe + repopulate ALL (destructive)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from notion_client import Client

from notion_client_helpers import query_all, title_of

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"

PLACEHOLDER = "↳ Add a linked view here (Notion UI: /linked → pick DB → filter to this Project), then delete this line."


# ── Block factories ──────────────────────────────────────────────────────────

def h2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def para(text: str = "") -> dict:
    rt = [{"type": "text", "text": {"content": text}}] if text else []
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt}}


def italic_para(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": text},
                "annotations": {"italic": True, "color": "gray"},
            }],
        },
    }


def db_link_para(label: str, database_id: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {"type": "text", "text": {"content": f"{label} "}},
                {"type": "mention", "mention": {"type": "database", "database": {"id": database_id}}},
            ],
        },
    }


def toggle(title: str, children: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": title}}],
            "children": children,
        },
    }


# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


# ── Section builders (scaffolding only) ──────────────────────────────────────

def build_notes_section() -> list[dict]:
    return [h2("Notes"), para("")]


def build_scaffold_section(heading: str, db_label: str, database_id: str) -> list[dict]:
    return [
        h2(heading),
        italic_para(PLACEHOLDER),
        db_link_para(db_label, database_id),
    ]


def build_done_tasks_toggle(cfg: dict) -> list[dict]:
    children = [
        italic_para(PLACEHOLDER),
        db_link_para("Open Task List DB →", cfg["ptl_database_id"]),
    ]
    return [toggle("Done tasks", children)]


# ── Page-level operations ────────────────────────────────────────────────────

def _has_real_content(notion: Client, page_id: str) -> bool:
    resp = notion.blocks.children.list(block_id=page_id, page_size=5)
    children = resp.get("results", [])
    if not children:
        return False
    if len(children) == 1:
        b = children[0]
        if b.get("type") == "paragraph":
            rt = b.get("paragraph", {}).get("rich_text") or []
            if not rt:
                return False
    return True


def _archive_all_children(notion: Client, page_id: str) -> int:
    count = 0
    while True:
        resp = notion.blocks.children.list(block_id=page_id, page_size=100)
        kids = resp.get("results", [])
        if not kids:
            break
        for b in kids:
            try:
                notion.blocks.delete(block_id=b["id"])
                count += 1
            except Exception as e:
                print(f"    warn: could not delete block {b['id']}: {e}")
        if not resp.get("has_more"):
            break
    return count


def populate_project_page(notion: Client, cfg: dict, project_page_id: str, *, force: bool = False) -> bool:
    if not force and _has_real_content(notion, project_page_id):
        return False

    if force:
        n = _archive_all_children(notion, project_page_id)
        if n:
            print(f"    [force] archived {n} existing blocks")

    blocks: list[dict] = []
    blocks += build_notes_section()
    blocks += build_scaffold_section("Subprojects", "Open Subprojects DB →", cfg["subprojects_database_id"])
    blocks += build_scaffold_section("Tasks", "Open Task List DB →", cfg["ptl_database_id"])
    blocks += build_scaffold_section("Recent Snapshots", "Open Project Snapshots DB →", cfg["database_id"])
    blocks += build_done_tasks_toggle(cfg)

    for i in range(0, len(blocks), 95):
        chunk = blocks[i:i + 95]
        notion.blocks.children.append(block_id=project_page_id, children=chunk)

    return True


def populate_all_empty(notion: Client, cfg: dict, *, force: bool = False) -> int:
    projects_ds = cfg.get("projects_data_source_id")
    if not projects_ds:
        return 0
    projects = query_all(notion, projects_ds)
    if not projects:
        return 0
    count = 0
    for p in projects:
        name = title_of(p) or "(untitled)"
        try:
            did = populate_project_page(notion, cfg, p["id"], force=force)
            if did:
                count += 1
                print(f"  populated: {name}")
            else:
                print(f"  skipped (already populated): {name}")
        except Exception as e:
            print(f"  ERROR populating {name}: {e}")
    return count


def main() -> None:
    force = "--force" in sys.argv
    cfg = load_config()
    for k in ("notion_token", "projects_data_source_id", "subprojects_database_id",
              "ptl_database_id", "database_id"):
        if not cfg.get(k):
            print(f"ERROR: config missing '{k}'. Run setup_notion.py / migrate_to_ptl.py first.")
            sys.exit(1)
    notion = Client(auth=cfg["notion_token"])
    if force:
        print("FORCE mode: wiping and repopulating all Project page bodies.")
    n = populate_all_empty(notion, cfg, force=force)
    print(f"\nDone. Populated {n} project page(s).")


if __name__ == "__main__":
    main()
