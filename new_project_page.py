#!/usr/bin/env python3
"""Populate a Project DB row's page body with a useful layout.

LINKED-VIEW MECHANISM — IMPORTANT:

  The Notion public API does NOT support creating `linked_database` blocks
  (a.k.a. "linked views" — the in-page views that let you re-filter/re-sort
  an existing DB without cloning it). Only `child_database` is creatable,
  and child_database creates a BRAND-NEW empty database inline, which is
  not what we want.

  Given that, this module renders each section as:
    1. A heading_2 title for the section.
    2. A rendered snapshot of the filtered query results, written as
       bulleted_list_item / paragraph blocks at population time.
    3. A small "Open <DB name> →" paragraph containing a link_to_page
       mention to the underlying database, so the user can click through
       to the full DB when they need live filtering.

  Re-running with --force wipes the body and re-renders, so this acts as a
  "refresh" button. Day-to-day the user can click the Open link to get a
  live view, or re-run this script to refresh the snapshot in place.

Usage:
    python new_project_page.py                 # populate all empty Projects
    python new_project_page.py --force         # destructive: wipe + repopulate ALL
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from notion_client import Client

from notion_client_helpers import (
    query_all,
    title_of,
    relation_ids,
    select_name,
    number_of,
)

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"

MAX_RECENT_SNAPSHOTS = 10
MAX_TASKS_DISPLAYED = 50
MAX_DONE_TASKS_DISPLAYED = 50


# ── Block factories ──────────────────────────────────────────────────────────

def h2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }


def para(text: str = "") -> dict:
    rt = [{"type": "text", "text": {"content": text}}] if text else []
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rt},
    }


def bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
        },
    }


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
                {
                    "type": "mention",
                    "mention": {"type": "database", "database": {"id": database_id}},
                },
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


# ── Section builders ─────────────────────────────────────────────────────────

def build_notes_section() -> list[dict]:
    return [h2("Notes"), para("")]


def build_subprojects_section(notion: Client, cfg: dict, project_id: str) -> list[dict]:
    blocks: list[dict] = [h2("Subprojects")]
    rows = query_all(
        notion, cfg["subprojects_data_source_id"],
        filter={"property": "Parent Project", "relation": {"contains": project_id}},
        sorts=[{"property": "Name", "direction": "ascending"}],
    )
    # Sort Active first, then by name (Notion doesn't let us sort by status
    # in a custom order, so we do it client-side).
    status_rank = {"Active": 0, "Paused": 1, "Done": 2, "Shelved": 3}
    rows.sort(key=lambda r: (status_rank.get(select_name(r, "Status"), 9), title_of(r).lower()))
    if not rows:
        blocks.append(italic_para("No subprojects yet."))
    else:
        for r in rows:
            status = select_name(r, "Status") or "—"
            blocks.append(bullet(f"{title_of(r)}  ·  {status}"))
    blocks.append(db_link_para("Open Subprojects DB →", cfg["subprojects_database_id"]))
    return blocks


def build_tasks_section(notion: Client, cfg: dict, subproject_ids_by_name: list[tuple[str, str]]) -> list[dict]:
    """Tasks (Up Next): Status=Todo, scoped to this Project's subprojects.

    Notion filter API cannot filter by rollup=Project directly in a useful
    way, so we filter by the set of Subproject IDs under this project (OR of
    contains). Sort by Subproject name asc (client-side) then Order asc.
    """
    blocks: list[dict] = [h2("Tasks")]
    if not subproject_ids_by_name:
        blocks.append(italic_para("No subprojects yet — add one to start a task list."))
        blocks.append(db_link_para("Open Task List DB →", cfg["ptl_database_id"]))
        return blocks

    sub_id_to_name = {sid: sname for sname, sid in subproject_ids_by_name}
    or_filter = [
        {"property": "Subproject", "relation": {"contains": sid}}
        for _, sid in subproject_ids_by_name
    ]
    filt = {
        "and": [
            {"property": "Status", "select": {"equals": "Todo"}},
            {"or": or_filter} if len(or_filter) > 1 else or_filter[0],
        ]
    }
    rows = query_all(
        notion, cfg["ptl_data_source_id"],
        filter=filt,
        sorts=[{"property": "Order", "direction": "ascending"}],
    )

    def _sort_key(row: dict) -> tuple:
        sub_ids = relation_ids(row, "Subproject")
        sub_name = sub_id_to_name.get(sub_ids[0], "") if sub_ids else ""
        ord_n = number_of(row, "Order")
        return (sub_name.lower(), ord_n if ord_n is not None else 1e9)

    rows.sort(key=_sort_key)
    rows = rows[:MAX_TASKS_DISPLAYED]

    if not rows:
        blocks.append(italic_para("No open tasks. Add one in the Task List DB to predetermine your next session."))
    else:
        current_sub = None
        for r in rows:
            sub_ids = relation_ids(r, "Subproject")
            sub_name = sub_id_to_name.get(sub_ids[0], "") if sub_ids else "(no subproject)"
            if sub_name != current_sub:
                blocks.append(para(sub_name))
                current_sub = sub_name
            title = title_of(r, "Title")
            ord_n = number_of(r, "Order")
            prefix = f"[{int(ord_n)}] " if ord_n is not None else ""
            blocks.append(bullet(f"{prefix}{title}"))
    blocks.append(db_link_para("Open Task List DB →", cfg["ptl_database_id"]))
    return blocks


def build_recent_snapshots_section(notion: Client, cfg: dict, subproject_ids: list[str]) -> list[dict]:
    blocks: list[dict] = [h2("Recent Snapshots")]
    if not subproject_ids:
        blocks.append(italic_para("No snapshots yet."))
        blocks.append(db_link_para("Open Project Snapshots DB →", cfg["database_id"]))
        return blocks

    or_filter = [
        {"property": "Subproject", "relation": {"contains": sid}}
        for sid in subproject_ids
    ]
    filt = {"or": or_filter} if len(or_filter) > 1 else or_filter[0]
    rows = query_all(
        notion, cfg["data_source_id"],
        filter=filt,
        sorts=[{"property": "Timestamp", "direction": "descending"}],
        page_size=MAX_RECENT_SNAPSHOTS,
    )
    rows = rows[:MAX_RECENT_SNAPSHOTS]

    if not rows:
        blocks.append(italic_para("No snapshots yet."))
    else:
        for r in rows:
            ts = ""
            ts_prop = r.get("properties", {}).get("Timestamp", {}).get("date") or {}
            if ts_prop:
                ts = ts_prop.get("start", "")[:10]
            # PS row has a title property — use title_of with fallback
            label = title_of(r) or "(untitled snapshot)"
            status_txt = ""
            stp = r.get("properties", {}).get("Status", {}).get("rich_text") or []
            if stp:
                status_txt = "".join(t.get("plain_text", "") for t in stp)
            line = f"{ts}  ·  {label}"
            if status_txt:
                line += f"  —  {status_txt[:120]}"
            blocks.append(bullet(line))
    blocks.append(db_link_para("Open Project Snapshots DB →", cfg["database_id"]))
    return blocks


def build_done_tasks_toggle(notion: Client, cfg: dict, subproject_ids_by_name: list[tuple[str, str]]) -> list[dict]:
    if not subproject_ids_by_name:
        return [toggle("Done tasks", [italic_para("No done tasks.")])]

    sub_id_to_name = {sid: sname for sname, sid in subproject_ids_by_name}
    or_filter = [
        {"property": "Subproject", "relation": {"contains": sid}}
        for _, sid in subproject_ids_by_name
    ]
    filt = {
        "and": [
            {"property": "Status", "select": {"equals": "Done"}},
            {"or": or_filter} if len(or_filter) > 1 else or_filter[0],
        ]
    }
    rows = query_all(
        notion, cfg["ptl_data_source_id"],
        filter=filt,
        sorts=[{"property": "Completed At", "direction": "descending"}],
    )
    rows = rows[:MAX_DONE_TASKS_DISPLAYED]

    children: list[dict] = []
    if not rows:
        children.append(italic_para("No done tasks yet."))
    else:
        for r in rows:
            sub_ids = relation_ids(r, "Subproject")
            sub_name = sub_id_to_name.get(sub_ids[0], "") if sub_ids else ""
            title = title_of(r, "Title")
            ca = r.get("properties", {}).get("Completed At", {}).get("date") or {}
            ca_s = (ca.get("start") or "")[:10] if ca else ""
            line = f"{ca_s}  ·  {sub_name}  ·  {title}" if sub_name else f"{ca_s}  ·  {title}"
            children.append(bullet(line))
    return [toggle("Done tasks", children)]


# ── Page-level operations ────────────────────────────────────────────────────

def _has_real_content(notion: Client, page_id: str) -> bool:
    """True if page has >0 child blocks that aren't a single empty paragraph."""
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

    # Gather subprojects for this project.
    subs = query_all(
        notion, cfg["subprojects_data_source_id"],
        filter={"property": "Parent Project", "relation": {"contains": project_page_id}},
    )
    subproject_ids_by_name = [(title_of(s), s["id"]) for s in subs]
    subproject_ids_by_name.sort(key=lambda t: t[0].lower())
    subproject_ids = [sid for _, sid in subproject_ids_by_name]

    blocks: list[dict] = []
    blocks += build_notes_section()
    blocks += build_subprojects_section(notion, cfg, project_page_id)
    blocks += build_tasks_section(notion, cfg, subproject_ids_by_name)
    blocks += build_recent_snapshots_section(notion, cfg, subproject_ids)
    blocks += build_done_tasks_toggle(notion, cfg, subproject_ids_by_name)

    # Notion caps children per request at 100. Chunk just in case.
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
    for k in ("notion_token", "projects_data_source_id", "subprojects_data_source_id",
              "ptl_data_source_id", "data_source_id", "projects_database_id",
              "subprojects_database_id", "ptl_database_id", "database_id"):
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
