#!/usr/bin/env python3
"""One-time migration: PS flat Project Select → Projects + Subprojects + relations.

Runs in phases, each phase is idempotent on its own:

  1. Ensure Project Snapshots DB has new props: Subproject, Task Complete?,
     Task Worked On, Next Task, Next Task Edit Action.
  2. For each existing Project Select option, create (if missing) a Projects row
     and a default Subproject row (same name).
  3. For each existing PS row, if its Subproject relation is empty, set it to
     the default Subproject matching its current Project Select value.
  4. [GATED — NOT RUN BY DEFAULT] Drop the old Project Select property.

The destructive step 4 is only run if you pass --drop-old-project. The user
must sign off on this. Re-run the script without the flag as many times as
you want; it will not duplicate rows.
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
)

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def fetch_ps_project_options(notion: Client, ps_ds: str) -> list[str]:
    ds = notion.data_sources.retrieve(data_source_id=ps_ds)
    proj = ds.get("properties", {}).get("Project", {})
    if proj.get("type") != "select":
        return []
    return [o["name"] for o in proj.get("select", {}).get("options", [])]


def ensure_ps_properties(notion: Client, ps_ds: str, subprojects_ds: str, ptl_ds: str) -> None:
    ds = notion.data_sources.retrieve(data_source_id=ps_ds)
    existing = ds.get("properties", {})
    to_add: dict = {}
    if "Subproject" not in existing:
        to_add["Subproject"] = {
            "relation": {
                "data_source_id": subprojects_ds,
                "type": "dual_property",
                "dual_property": {},
            }
        }
    if "Task Complete?" not in existing:
        to_add["Task Complete?"] = {"checkbox": {}}
    if "Task Worked On" not in existing:
        to_add["Task Worked On"] = {
            "relation": {
                "data_source_id": ptl_ds,
                "type": "dual_property",
                "dual_property": {},
            }
        }
    if "Next Task" not in existing:
        to_add["Next Task"] = {
            "relation": {
                "data_source_id": ptl_ds,
                "type": "dual_property",
                "dual_property": {},
            }
        }
    if "Next Task Edit Action" not in existing:
        to_add["Next Task Edit Action"] = {
            "select": {
                "options": [
                    {"name": "None"},
                    {"name": "Replace"},
                    {"name": "Insert"},
                    {"name": "Skip"},
                ]
            }
        }
    if to_add:
        print(f"Adding PS properties: {list(to_add)}")
        notion.data_sources.update(data_source_id=ps_ds, properties=to_add)
    else:
        print("PS properties already present; nothing to add.")


def fetch_existing_projects(notion: Client, projects_ds: str) -> dict[str, dict]:
    rows = query_all(notion, projects_ds)
    return {title_of(r): r for r in rows if title_of(r)}


def fetch_existing_subprojects(notion: Client, subprojects_ds: str) -> dict[tuple[str, str], dict]:
    """Keyed by (parent_project_page_id, subproject_name)."""
    rows = query_all(notion, subprojects_ds)
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        name = title_of(r)
        parents = relation_ids(r, "Parent Project")
        for pid in parents:
            out[(pid, name)] = r
    return out


def ensure_project_and_default_subproject(
    notion: Client, projects_ds: str, subprojects_ds: str,
    name: str,
    projects_cache: dict[str, dict],
    subs_cache: dict[tuple[str, str], dict],
) -> tuple[dict, dict]:
    proj = projects_cache.get(name)
    if proj is None:
        print(f"  Creating Project: {name}")
        proj = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": projects_ds},
            properties={
                "Name": {"title": [{"type": "text", "text": {"content": name}}]},
                "Status": {"select": {"name": "Active"}},
            },
        )
        projects_cache[name] = proj
    else:
        print(f"  Project exists: {name}")

    sub_key = (proj["id"], name)
    sub = subs_cache.get(sub_key)
    if sub is None:
        print(f"    Creating default Subproject: {name}")
        sub = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": subprojects_ds},
            properties={
                "Name": {"title": [{"type": "text", "text": {"content": name}}]},
                "Parent Project": {"relation": [{"id": proj["id"]}]},
                "Status": {"select": {"name": "Active"}},
            },
        )
        subs_cache[sub_key] = sub
    else:
        print(f"    Default Subproject exists: {name}")
    return proj, sub


def backfill_ps_subproject_relations(
    notion: Client,
    ps_ds: str,
    proj_to_default_sub: dict[str, dict],
) -> None:
    ps_rows = query_all(notion, ps_ds)
    print(f"Checking {len(ps_rows)} PS rows for missing Subproject relation…")
    updated = 0
    skipped = 0
    for row in ps_rows:
        # Skip if Subproject relation already set.
        if relation_ids(row, "Subproject"):
            skipped += 1
            continue
        proj_name = select_name(row, "Project")
        if not proj_name:
            continue
        default_sub = proj_to_default_sub.get(proj_name)
        if not default_sub:
            print(f"  WARN: no default subproject for project '{proj_name}' — skipping row {row['id']}")
            continue
        notion.pages.update(
            page_id=row["id"],
            properties={"Subproject": {"relation": [{"id": default_sub["id"]}]}},
        )
        updated += 1
    print(f"Backfill complete. Updated: {updated}, already-set: {skipped}.")


def drop_old_project_property(notion: Client, ps_ds: str) -> None:
    print("DESTRUCTIVE: removing old 'Project' Select property from PS DB…")
    notion.data_sources.update(
        data_source_id=ps_ds,
        properties={"Project": None},
    )
    print("Dropped.")


def main() -> None:
    drop_old = "--drop-old-project" in sys.argv

    cfg = load_config()
    for k in (
        "notion_token", "data_source_id",
        "projects_data_source_id", "subprojects_data_source_id", "ptl_data_source_id",
    ):
        if not cfg.get(k):
            print(f"ERROR: config missing '{k}'. Run setup_notion.py first.")
            sys.exit(1)

    notion = Client(auth=cfg["notion_token"])
    ps_ds = cfg["data_source_id"]
    projects_ds = cfg["projects_data_source_id"]
    subprojects_ds = cfg["subprojects_data_source_id"]
    ptl_ds = cfg["ptl_data_source_id"]

    # Step 1: ensure new PS properties exist.
    ensure_ps_properties(notion, ps_ds, subprojects_ds, ptl_ds)

    # Step 2: collect existing Project Select options; create Projects + default Subprojects.
    project_names = fetch_ps_project_options(notion, ps_ds)
    if not project_names:
        print("No Project Select options found — PS may have already been migrated.")
        # Still build proj_to_default_sub from existing Subprojects so backfill is a no-op.
    else:
        print(f"Found {len(project_names)} existing Project Select options: {project_names}")

    projects_cache = fetch_existing_projects(notion, projects_ds)
    subs_cache = fetch_existing_subprojects(notion, subprojects_ds)

    proj_to_default_sub: dict[str, dict] = {}
    for name in project_names:
        proj, sub = ensure_project_and_default_subproject(
            notion, projects_ds, subprojects_ds,
            name, projects_cache, subs_cache,
        )
        proj_to_default_sub[name] = sub

    # Even if project_names was empty (post-drop re-run), rebuild from caches so re-runs behave:
    if not proj_to_default_sub:
        for (pid, sname), sub in subs_cache.items():
            # Pick first subproject with name == project name as the default.
            proj = next((p for p in projects_cache.values() if p["id"] == pid), None)
            if proj and title_of(proj) == sname:
                proj_to_default_sub[sname] = sub

    # Step 3: backfill existing PS rows' Subproject relation.
    if project_names:
        backfill_ps_subproject_relations(notion, ps_ds, proj_to_default_sub)
    else:
        print("Skipping PS row backfill (no Project Select options to key on).")

    # Step 4 (gated).
    if drop_old:
        # Only meaningful if Project still exists.
        if project_names:
            drop_old_project_property(notion, ps_ds)
        else:
            print("Old 'Project' Select property not present; nothing to drop.")
    else:
        print()
        print("Step 4 (destructive) not run. The old 'Project' Select property is still on the PS DB.")
        print("To drop it (after you verify Projects/Subprojects look right), re-run:")
        print("  python migrate_to_ptl.py --drop-old-project")


if __name__ == "__main__":
    main()
