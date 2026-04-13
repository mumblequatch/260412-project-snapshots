"""Helpers for Project Task List (PTL) flow.

Query helpers for the PTL database, order-math for Insert, and
archive-check for the post-save "all subprojects Done" modal.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from notion_client import Client


# ── Query helpers ────────────────────────────────────────────────────────────

def query_all(notion: Client, data_source_id: str, **query) -> list[dict]:
    """Query a data source, paging through all results."""
    out: list[dict] = []
    cursor = None
    while True:
        kwargs = dict(query)
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(data_source_id=data_source_id, **kwargs)
        out.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


def title_of(page: dict, prop_name: str = "Name") -> str:
    props = page.get("properties", {})
    for key in (prop_name, "Title", "title"):
        p = props.get(key)
        if p and p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
    # fall back: first title property
    for v in props.values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title", []))
    return ""


def relation_ids(page: dict, prop_name: str) -> list[str]:
    p = page.get("properties", {}).get(prop_name) or {}
    return [r["id"] for r in p.get("relation", [])]


def select_name(page: dict, prop_name: str) -> str:
    p = page.get("properties", {}).get(prop_name) or {}
    sel = p.get("select")
    return sel.get("name", "") if sel else ""


def number_of(page: dict, prop_name: str) -> float | None:
    p = page.get("properties", {}).get(prop_name) or {}
    return p.get("number")


# ── Projects / Subprojects ───────────────────────────────────────────────────

def fetch_active_projects(notion: Client, projects_ds: str) -> list[dict]:
    """Return projects with Status != Archived, sorted by Name asc."""
    return query_all(
        notion, projects_ds,
        filter={"property": "Status", "select": {"does_not_equal": "Archived"}},
        sorts=[{"property": "Name", "direction": "ascending"}],
    )


def fetch_subprojects_for_project(notion: Client, subprojects_ds: str, project_page_id: str) -> list[dict]:
    """Return subprojects under project_page_id with Status in {Active, Paused}."""
    return query_all(
        notion, subprojects_ds,
        filter={
            "and": [
                {"property": "Parent Project", "relation": {"contains": project_page_id}},
                {
                    "or": [
                        {"property": "Status", "select": {"equals": "Active"}},
                        {"property": "Status", "select": {"equals": "Paused"}},
                    ]
                },
            ]
        },
        sorts=[{"property": "Name", "direction": "ascending"}],
    )


def fetch_all_subprojects_for_project(notion: Client, subprojects_ds: str, project_page_id: str) -> list[dict]:
    return query_all(
        notion, subprojects_ds,
        filter={"property": "Parent Project", "relation": {"contains": project_page_id}},
    )


# ── PTL ──────────────────────────────────────────────────────────────────────

def fetch_top_todo_tasks(notion: Client, ptl_ds: str, subproject_page_id: str, limit: int = 5) -> list[dict]:
    rows = query_all(
        notion, ptl_ds,
        filter={
            "and": [
                {"property": "Subproject", "relation": {"contains": subproject_page_id}},
                {"property": "Status", "select": {"equals": "Todo"}},
            ]
        },
        sorts=[{"property": "Order", "direction": "ascending"}],
        page_size=min(limit, 100),
    )
    return rows[:limit]


def fetch_all_todo_tasks(notion: Client, ptl_ds: str, subproject_page_id: str) -> list[dict]:
    return query_all(
        notion, ptl_ds,
        filter={
            "and": [
                {"property": "Subproject", "relation": {"contains": subproject_page_id}},
                {"property": "Status", "select": {"equals": "Todo"}},
            ]
        },
        sorts=[{"property": "Order", "direction": "ascending"}],
    )


def create_ptl_task(notion: Client, ptl_ds: str, *, title: str, subproject_id: str, order: float, notes: str = "") -> dict:
    props = {
        "Title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
        "Subproject": {"relation": [{"id": subproject_id}]},
        "Order": {"number": order},
        "Status": {"select": {"name": "Todo"}},
    }
    if notes:
        props["Notes"] = {"rich_text": [{"type": "text", "text": {"content": notes[:2000]}}]}
    return notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": ptl_ds},
        properties=props,
    )


def mark_ptl_done(notion: Client, page_id: str, snapshot_id: str | None = None) -> None:
    props = {
        "Status": {"select": {"name": "Done"}},
        "Completed At": {"date": {"start": datetime.now().astimezone().isoformat()}},
    }
    if snapshot_id:
        props["Completed In Snapshot"] = {"relation": [{"id": snapshot_id}]}
    notion.pages.update(page_id=page_id, properties=props)


def mark_ptl_skipped(notion: Client, page_id: str) -> None:
    notion.pages.update(page_id=page_id, properties={"Status": {"select": {"name": "Skipped"}}})


def update_ptl_title(notion: Client, page_id: str, new_title: str) -> None:
    notion.pages.update(
        page_id=page_id,
        properties={"Title": {"title": [{"type": "text", "text": {"content": new_title[:2000]}}]}},
    )


# ── Order math ───────────────────────────────────────────────────────────────

def renumber_todo_tasks(notion: Client, ptl_ds: str, subproject_page_id: str) -> list[dict]:
    """Renumber all Todo tasks under a subproject to fresh 10/20/30 spacing.
    Returns the refreshed list (sorted by new Order asc)."""
    rows = fetch_all_todo_tasks(notion, ptl_ds, subproject_page_id)
    for i, row in enumerate(rows, start=1):
        new_order = i * 10.0
        cur = number_of(row, "Order")
        if cur != new_order:
            notion.pages.update(page_id=row["id"], properties={"Order": {"number": new_order}})
            row["properties"]["Order"]["number"] = new_order
    return rows


def compute_insert_order(
    notion: Client,
    ptl_ds: str,
    subproject_page_id: str,
    upcoming: dict | None,
    previous: dict | None,
) -> tuple[float, bool]:
    """Return (order, did_renumber). If gap < 1 between previous and upcoming,
    renumber first then recompute."""
    if upcoming is None:
        # No upcoming — append at end
        all_todos = fetch_all_todo_tasks(notion, ptl_ds, subproject_page_id)
        if not all_todos:
            return 10.0, False
        last = number_of(all_todos[-1], "Order") or 0
        return last + 10.0, False

    up = number_of(upcoming, "Order") or 0
    if previous is None:
        return up - 5.0, False

    prev = number_of(previous, "Order") or 0
    if up - prev >= 1:
        return (up + prev) / 2.0, False

    # Gap exhausted — renumber and recompute.
    rows = renumber_todo_tasks(notion, ptl_ds, subproject_page_id)
    # find upcoming + previous again by id
    upcoming_new = next((r for r in rows if r["id"] == upcoming["id"]), None)
    previous_new = next((r for r in rows if r["id"] == previous["id"]), None)
    if upcoming_new is None:
        return 10.0, True
    up_n = number_of(upcoming_new, "Order") or 0
    if previous_new is None:
        return up_n - 5.0, True
    prev_n = number_of(previous_new, "Order") or 0
    return (up_n + prev_n) / 2.0, True


# ── Archive check ────────────────────────────────────────────────────────────

def subproject_has_open_tasks(notion: Client, ptl_ds: str, subproject_page_id: str) -> bool:
    resp = notion.data_sources.query(
        data_source_id=ptl_ds,
        filter={
            "and": [
                {"property": "Subproject", "relation": {"contains": subproject_page_id}},
                {"property": "Status", "select": {"equals": "Todo"}},
            ]
        },
        page_size=1,
    )
    return bool(resp.get("results"))


def set_subproject_done_if_empty(notion: Client, ptl_ds: str, subproject_page: dict) -> bool:
    """If the subproject has no Todo tasks and Status == Active, flip to Done.
    Returns True if flipped."""
    sid = subproject_page["id"]
    status = select_name(subproject_page, "Status")
    if status != "Active":
        return False
    if subproject_has_open_tasks(notion, ptl_ds, sid):
        return False
    notion.pages.update(page_id=sid, properties={"Status": {"select": {"name": "Done"}}})
    return True


def all_subprojects_done(notion: Client, subprojects_ds: str, project_page_id: str) -> bool:
    subs = fetch_all_subprojects_for_project(notion, subprojects_ds, project_page_id)
    if not subs:
        return False
    return all(select_name(s, "Status") == "Done" for s in subs)


def archive_project(notion: Client, project_page_id: str) -> None:
    notion.pages.update(
        page_id=project_page_id,
        properties={"Status": {"select": {"name": "Archived"}}},
    )
