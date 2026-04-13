#!/usr/bin/env python3
"""Project Snapshots — end-of-session capture form (with Project Task List).

Triggered by a Keyboard Maestro hotkey. Shows a floating form, lets the user
pick Project → Subproject, confirms whether the task they went into the session
working on got done, auto-fills the next task from the PTL (with option to
replace / insert / skip), writes a new PS row + PTL updates, then fires a
detached Overview refresh.

CLI:
    python snapshot.py                      # launch the GUI
    python snapshot.py --refresh-overview   # regenerate the overview page only
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
    USING_CTK = True
except ImportError:
    import tkinter as ctk  # type: ignore
    USING_CTK = False

import tkinter as tk

from notion_client import Client
from notion_client.errors import APIResponseError

from notion_client_helpers import (
    fetch_active_projects,
    fetch_subprojects_for_project,
    fetch_top_todo_tasks,
    fetch_all_todo_tasks,
    create_ptl_task,
    mark_ptl_done,
    mark_ptl_skipped,
    update_ptl_title,
    compute_insert_order,
    set_subproject_done_if_empty,
    all_subprojects_done,
    archive_project,
    title_of,
    relation_ids,
    select_name,
    number_of,
    query_all,
)

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"


# ── Config ───────────────────────────────────────────────────────────────────

REQUIRED_KEYS = (
    "notion_token", "database_id", "data_source_id", "overview_page_id",
    "projects_database_id", "projects_data_source_id",
    "subprojects_database_id", "subprojects_data_source_id",
    "ptl_database_id", "ptl_data_source_id",
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config not found at {CONFIG_PATH}. Run setup_notion.py.")
    cfg = json.loads(CONFIG_PATH.read_text())
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Config missing keys: {missing}. Run setup_notion.py then migrate_to_ptl.py.")
    return cfg


# ── Rich-text helpers ────────────────────────────────────────────────────────

def rt(text: str) -> list:
    if not text:
        return []
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def rt_files(paths: list[str]) -> list:
    out = []
    for i, p in enumerate(paths):
        p = p.strip()
        if not p:
            continue
        if i > 0:
            out.append({"type": "text", "text": {"content": "\n"}})
        if p.startswith(("http://", "https://")):
            out.append({"type": "text", "text": {"content": p, "link": {"url": p}}})
        else:
            out.append({"type": "text", "text": {"content": p}})
    return out


def rt_to_plain(prop: dict) -> str:
    parts = prop.get("rich_text", []) if prop else []
    return "".join(p.get("plain_text", "") for p in parts)


# ── PS queries ───────────────────────────────────────────────────────────────

def fetch_latest_snapshot_for_subproject(notion: Client, ps_ds: str, subproject_id: str) -> dict | None:
    resp = notion.data_sources.query(
        data_source_id=ps_ds,
        filter={"property": "Subproject", "relation": {"contains": subproject_id}},
        sorts=[{"property": "Timestamp", "direction": "descending"}],
        page_size=1,
    )
    results = resp.get("results", [])
    if not results:
        return None
    page = results[0]
    props = page["properties"]
    ts = (props.get("Timestamp") or {}).get("date") or {}
    next_task_rel = relation_ids(page, "Next Task")
    return {
        "status": rt_to_plain(props.get("Status")),
        "next_action": rt_to_plain(props.get("Next Action")),
        "open_questions": rt_to_plain(props.get("Open Questions")),
        "latest_files": rt_to_plain(props.get("Latest Files")),
        "timestamp": ts.get("start", ""),
        "page_url": page.get("url", ""),
        "next_task_ids": next_task_rel,
    }


# ── PTL / snapshot save ──────────────────────────────────────────────────────

def create_snapshot_row(
    notion: Client,
    ps_ds: str,
    *,
    project_name: str,
    subproject_id: str,
    status: str,
    next_action_title: str,
    open_questions: str,
    latest_files: str,
    session_notes: str,
    task_complete: bool,
    task_worked_on_id: str | None,
    next_task_id: str | None,
    edit_action: str,  # "None" | "Replace" | "Insert" | "Skip"
) -> dict:
    now = datetime.now().astimezone()
    now_iso = now.isoformat()
    title = f"{project_name} · {now.strftime('%Y-%m-%d %H:%M')}"
    file_lines = [l for l in latest_files.splitlines() if l.strip()]
    props: dict = {
        "Name": {"title": rt(title)},
        "Subproject": {"relation": [{"id": subproject_id}]},
        "Status": {"rich_text": rt(status)},
        "Next Action": {"rich_text": rt(next_action_title)},
        "Open Questions": {"rich_text": rt(open_questions)},
        "Latest Files": {"rich_text": rt_files(file_lines)},
        "Session Notes": {"rich_text": rt(session_notes)},
        "Timestamp": {"date": {"start": now_iso}},
        "Task Complete?": {"checkbox": bool(task_complete)},
        "Next Task Edit Action": {"select": {"name": edit_action}},
    }
    if task_worked_on_id:
        props["Task Worked On"] = {"relation": [{"id": task_worked_on_id}]}
    if next_task_id:
        props["Next Task"] = {"relation": [{"id": next_task_id}]}
    return notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": ps_ds},
        properties=props,
    )


# ── Overview page ────────────────────────────────────────────────────────────

def _fmt_updated(iso_ts: str) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts).astimezone()
    except ValueError:
        return iso_ts
    return dt.strftime("%b %-d, %-I:%M %p")


def refresh_overview(notion: Client, cfg: dict) -> None:
    """Rebuild the Overview table. Iterates Subprojects (Status != Done/Shelved)
    and pulls their latest PS row."""
    ps_ds = cfg["data_source_id"]
    subprojects_ds = cfg["subprojects_data_source_id"]
    overview_id = cfg["overview_page_id"]

    # Gather subprojects (Active + Paused).
    subs = query_all(
        notion, subprojects_ds,
        filter={
            "or": [
                {"property": "Status", "select": {"equals": "Active"}},
                {"property": "Status", "select": {"equals": "Paused"}},
            ]
        },
        sorts=[{"property": "Name", "direction": "ascending"}],
    )

    now_str = datetime.now().strftime("%B %-d, %Y, %-I:%M %p")

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"Last updated: {now_str}"},
                        "annotations": {"italic": True},
                    }
                ]
            },
        }
    ]

    def cell(text: str, *, bold: bool = False, url: str = "") -> list:
        if not text:
            return []
        text_obj = {"content": text[:2000]}
        if url:
            text_obj["link"] = {"url": url}
        rt_obj = {"type": "text", "text": text_obj}
        if bold:
            rt_obj["annotations"] = {"bold": True}
        return [rt_obj]

    header_row = {
        "type": "table_row",
        "table_row": {
            "cells": [
                cell("PROJECT", bold=True),
                cell("STATUS", bold=True),
                cell("NEXT ACTION", bold=True),
                cell("LATEST FILE", bold=True),
                cell("LAST UPDATED", bold=True),
            ]
        },
    }
    table_rows = [header_row]
    for sub in subs:
        sub_name = title_of(sub)
        latest = fetch_latest_snapshot_for_subproject(notion, ps_ds, sub["id"])
        if latest is None:
            status = "No snapshots yet."
            next_action = "—"
            first_file = ""
            updated = "—"
            page_url = ""
        else:
            status = latest["status"] or "—"
            next_action = latest["next_action"] or "—"
            files = [f for f in latest["latest_files"].splitlines() if f.strip()]
            first_file = files[0] if files else "—"
            updated = _fmt_updated(latest["timestamp"])
            page_url = latest["page_url"]
        table_rows.append({
            "type": "table_row",
            "table_row": {
                "cells": [
                    cell(sub_name, bold=True),
                    cell(status),
                    cell(next_action),
                    cell(first_file),
                    cell(updated, url=page_url),
                ]
            },
        })

    children.append({
        "object": "block",
        "type": "table",
        "table": {
            "table_width": 5,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows,
        },
    })

    existing = notion.blocks.children.list(block_id=overview_id).get("results", [])
    for block in existing:
        try:
            notion.blocks.delete(block_id=block["id"])
        except APIResponseError:
            pass

    for i in range(0, len(children), 100):
        notion.blocks.children.append(block_id=overview_id, children=children[i:i + 100])


# ── GUI ──────────────────────────────────────────────────────────────────────

EDIT_ACTIONS = [
    ("Replace upcoming task with this edit", "Replace"),
    ("Insert this as a new task before the upcoming one", "Insert"),
    ("Skip the upcoming task; use this instead", "Skip"),
]


class SnapshotApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.notion = Client(auth=cfg["notion_token"])

        try:
            self.projects = fetch_active_projects(self.notion, cfg["projects_data_source_id"])
        except Exception as e:
            messagebox.showerror("Notion error", f"Could not fetch projects:\n{e}")
            sys.exit(1)

        if not self.projects:
            messagebox.showerror("Setup error", "No active projects found. Run migrate_to_ptl.py?")
            sys.exit(1)

        self.project_name_to_page = {title_of(p): p for p in self.projects if title_of(p)}

        self.subprojects: list[dict] = []  # current project's subs
        self.subproject_name_to_page: dict[str, dict] = {}

        # PTL state
        self.top_todo: list[dict] = []            # top 5 Todo PTL rows for current subproject
        self.last_next_task_id: str | None = None  # previous PS row's Next Task id (if any)
        self.last_next_task_title: str = ""
        self.auto_next_task: dict | None = None    # PTL row the form will treat as "upcoming"
        self.auto_next_title_original: str = ""    # what we auto-filled into the entry

        self.files: list[str] = []

        if USING_CTK:
            ctk.set_appearance_mode("System")
            ctk.set_default_color_theme("blue")
            self.root = ctk.CTk()
        else:
            self.root = ctk.Tk()

        self.root.title("Project Snapshot")
        self.root.attributes("-topmost", True)
        self._center(660, 980)
        self.root.resizable(True, True)
        self.root.minsize(600, 780)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<Command-Return>", lambda _e: self.on_save())

        self._build_ui()
        # initial population
        first_proj = sorted(self.project_name_to_page.keys())[0]
        self.project_var.set(first_proj)
        self.on_project_change(first_proj)

    # ── layout helpers ─────────────────────────────────────────────────────
    def _center(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = max(20, (sh - h) // 4)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _label(self, parent, text: str):
        if USING_CTK:
            return ctk.CTkLabel(parent, text=text, anchor="w")
        return ctk.Label(parent, text=text, anchor="w")

    def _frame(self, parent):
        return ctk.CTkFrame(parent) if USING_CTK else ctk.Frame(parent)

    def _textbox(self, parent, height: int):
        if USING_CTK:
            tb = ctk.CTkTextbox(parent, height=height * 22, wrap="word")
            inner = tb
        else:
            tb = tk.Text(parent, height=height, wrap="word")
            inner = tb
        def _focus_next(ev):
            ev.widget.tk_focusNext().focus()
            return "break"
        def _focus_prev(ev):
            ev.widget.tk_focusPrev().focus()
            return "break"
        inner.bind("<Tab>", _focus_next)
        inner.bind("<Shift-Tab>", _focus_prev)
        return tb

    def _entry(self, parent):
        if USING_CTK:
            return ctk.CTkEntry(parent)
        return tk.Entry(parent)

    def _option_menu(self, parent, var, values, command=None):
        if USING_CTK:
            return ctk.CTkOptionMenu(parent, values=values or ["—"], variable=var, command=command)
        return tk.OptionMenu(parent, var, *(values or ["—"]), command=command) if command \
            else tk.OptionMenu(parent, var, *(values or ["—"]))

    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 4}

        # Save/Cancel bar
        btn_row = self._frame(self.root)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)
        cancel_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            btn_row, text="Cancel", command=self.root.destroy,
        )
        cancel_btn.pack(side="right", padx=(8, 0))
        self.save_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            btn_row, text="Save Snapshot", command=self.on_save,
        )
        self.save_btn.pack(side="right")

        # Project
        self._label(self.root, "Project").pack(fill="x", **pad)
        proj_names = sorted(self.project_name_to_page.keys())
        self.project_var = (ctk.StringVar if USING_CTK else tk.StringVar)(value=proj_names[0])
        self.project_menu = self._option_menu(
            self.root, self.project_var, proj_names, command=self.on_project_change
        )
        self.project_menu.pack(fill="x", **pad)

        # Subproject
        self._label(self.root, "Subproject").pack(fill="x", **pad)
        self.subproject_var = (ctk.StringVar if USING_CTK else tk.StringVar)(value="—")
        self.subproject_menu = self._option_menu(
            self.root, self.subproject_var, ["—"], command=self.on_subproject_change
        )
        self.subproject_menu.pack(fill="x", **pad)

        # Last Next Task read-only
        self.last_next_label = self._label(self.root, "Last Next Task: —")
        self.last_next_label.pack(fill="x", **pad)

        # Task Complete?
        tc_row = self._frame(self.root)
        tc_row.pack(fill="x", **pad)
        self.task_complete_var = (ctk.BooleanVar if USING_CTK else tk.BooleanVar)(value=True)
        if USING_CTK:
            self.task_complete_cb = ctk.CTkCheckBox(
                tc_row, text="Task Complete? (the task I went into this session on)",
                variable=self.task_complete_var, command=self._on_task_complete_toggle,
            )
        else:
            self.task_complete_cb = tk.Checkbutton(
                tc_row, text="Task Complete? (the task I went into this session on)",
                variable=self.task_complete_var, command=self._on_task_complete_toggle,
            )
        self.task_complete_cb.pack(side="left")

        # Next Task entry + "...or pick another" dropdown
        self._label(self.root, "Next Task").pack(fill="x", **pad)
        nt_row = self._frame(self.root)
        nt_row.pack(fill="x", **pad)
        self.next_task_var = (ctk.StringVar if USING_CTK else tk.StringVar)(value="")
        self.next_task_entry = self._entry(nt_row)
        if USING_CTK:
            self.next_task_entry.configure(textvariable=self.next_task_var)
        else:
            self.next_task_entry.config(textvariable=self.next_task_var)
        self.next_task_entry.pack(side="left", fill="x", expand=True)
        self.next_task_var.trace_add("write", lambda *_: self._on_next_task_text_changed())

        self.picker_var = (ctk.StringVar if USING_CTK else tk.StringVar)(value="…or pick another")
        self.picker_menu = self._option_menu(
            nt_row, self.picker_var, ["…or pick another"], command=self._on_picker_select
        )
        self.picker_menu.pack(side="left", padx=(8, 0))

        # Edit action radio (initially hidden; shown when text differs from auto)
        self.edit_radio_frame = self._frame(self.root)
        self.edit_radio_frame.pack(fill="x", **pad)
        self.edit_action_var = (ctk.StringVar if USING_CTK else tk.StringVar)(value="Insert")
        self.edit_radio_buttons = []
        for label, value in EDIT_ACTIONS:
            if USING_CTK:
                rb = ctk.CTkRadioButton(
                    self.edit_radio_frame, text=label, variable=self.edit_action_var, value=value,
                )
            else:
                rb = tk.Radiobutton(
                    self.edit_radio_frame, text=label, variable=self.edit_action_var, value=value, anchor="w",
                )
            rb.pack(anchor="w")
            self.edit_radio_buttons.append(rb)
        self._set_edit_radio_visible(False)

        # Status
        self._label(self.root, "Status").pack(fill="x", **pad)
        self.status_tb = self._textbox(self.root, 3)
        self.status_tb.pack(fill="x", **pad)

        # Open Questions
        self._label(self.root, "Open Questions").pack(fill="x", **pad)
        self.questions_tb = self._textbox(self.root, 3)
        self.questions_tb.pack(fill="x", **pad)

        # Latest Files
        self._label(self.root, "Latest Files").pack(fill="x", **pad)
        files_row = self._frame(self.root)
        files_row.pack(fill="x", **pad)
        add_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            files_row, text="Add Files…", command=self.on_add_files,
        )
        add_btn.pack(side="left")
        self.files_frame = self._frame(self.root)
        self.files_frame.pack(fill="x", **pad)

        # Session Notes
        self._label(self.root, "Session Notes").pack(fill="x", **pad)
        self.notes_tb = self._textbox(self.root, 3)
        self.notes_tb.pack(fill="x", **pad)

    # ── widget helpers ─────────────────────────────────────────────────────
    def _set_textbox(self, tb, value: str) -> None:
        tb.delete("1.0", "end")
        if value:
            tb.insert("1.0", value)

    def _get_textbox(self, tb) -> str:
        return tb.get("1.0", "end").strip()

    def _set_option_menu_values(self, menu, var, values: list[str], default: str) -> None:
        if not values:
            values = ["—"]
        if USING_CTK:
            menu.configure(values=values)
        else:
            m = menu["menu"]
            m.delete(0, "end")
            for v in values:
                m.add_command(label=v, command=lambda val=v: var.set(val))
        var.set(default if default in values else values[0])

    def _set_edit_radio_visible(self, visible: bool) -> None:
        if visible:
            self.edit_radio_frame.pack(fill="x", padx=16, pady=4)
        else:
            self.edit_radio_frame.pack_forget()

    def _render_files(self) -> None:
        for child in self.files_frame.winfo_children():
            child.destroy()
        for idx, path in enumerate(self.files):
            row = self._frame(self.files_frame)
            row.pack(fill="x", pady=1)
            lbl = (ctk.CTkLabel if USING_CTK else ctk.Label)(
                row, text=path, anchor="w",
            )
            lbl.pack(side="left", fill="x", expand=True)
            x_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
                row, text="x", width=28,
                command=lambda i=idx: self._remove_file(i),
            )
            x_btn.pack(side="right")

    def _remove_file(self, idx: int) -> None:
        if 0 <= idx < len(self.files):
            self.files.pop(idx)
            self._render_files()

    # ── event handlers ─────────────────────────────────────────────────────
    def on_project_change(self, project_name: str) -> None:
        proj = self.project_name_to_page.get(project_name)
        if not proj:
            return
        try:
            self.subprojects = fetch_subprojects_for_project(
                self.notion, self.cfg["subprojects_data_source_id"], proj["id"],
            )
        except Exception as e:
            messagebox.showerror("Notion error", f"Could not fetch subprojects:\n{e}")
            self.subprojects = []
        self.subproject_name_to_page = {title_of(s): s for s in self.subprojects if title_of(s)}
        names = sorted(self.subproject_name_to_page.keys())
        default = names[0] if names else "—"
        self._set_option_menu_values(self.subproject_menu, self.subproject_var, names, default)
        if names:
            self.on_subproject_change(default)
        else:
            self._reset_subproject_ui()

    def _reset_subproject_ui(self) -> None:
        self.top_todo = []
        self.last_next_task_id = None
        self.last_next_task_title = ""
        self.auto_next_task = None
        self.auto_next_title_original = ""
        self.last_next_label.configure(text="Last Next Task: —")
        self.next_task_var.set("")
        self._set_option_menu_values(self.picker_menu, self.picker_var, ["…or pick another"], "…or pick another")
        self._set_textbox(self.status_tb, "")
        self._set_textbox(self.questions_tb, "")
        self.files = []
        self._render_files()
        self._set_edit_radio_visible(False)

    def on_subproject_change(self, subproject_name: str) -> None:
        sub = self.subproject_name_to_page.get(subproject_name)
        if not sub:
            self._reset_subproject_ui()
            return
        sid = sub["id"]

        # Last PS row for this subproject
        try:
            latest = fetch_latest_snapshot_for_subproject(
                self.notion, self.cfg["data_source_id"], sid,
            )
        except Exception as e:
            latest = None
            print(f"WARN: could not fetch latest PS: {e}", file=sys.stderr)

        self.last_next_task_id = None
        self.last_next_task_title = ""
        if latest and latest.get("next_task_ids"):
            self.last_next_task_id = latest["next_task_ids"][0]
            try:
                page = self.notion.pages.retrieve(page_id=self.last_next_task_id)
                self.last_next_task_title = title_of(page, "Title")
            except Exception:
                self.last_next_task_title = ""

        # Top 5 todo tasks
        try:
            self.top_todo = fetch_top_todo_tasks(
                self.notion, self.cfg["ptl_data_source_id"], sid, limit=5,
            )
        except Exception as e:
            self.top_todo = []
            print(f"WARN: could not fetch PTL: {e}", file=sys.stderr)

        # Populate "pick another" with tasks 2–5
        picker_values = ["…or pick another"]
        for t in self.top_todo[1:]:
            picker_values.append(title_of(t, "Title") or "(untitled)")
        self._set_option_menu_values(self.picker_menu, self.picker_var, picker_values, "…or pick another")

        self._refresh_last_next_label()
        self._apply_auto_next_task()

        # Pre-fill status/questions/files from last snapshot.
        if latest:
            self._set_textbox(self.status_tb, latest["status"])
            self._set_textbox(self.questions_tb, latest["open_questions"])
            self.files = [l for l in latest["latest_files"].splitlines() if l.strip()]
        else:
            self._set_textbox(self.status_tb, "")
            self._set_textbox(self.questions_tb, "")
            self.files = []
        self._render_files()
        self._set_textbox(self.notes_tb, "")

    def _refresh_last_next_label(self) -> None:
        if self.last_next_task_title:
            self.last_next_label.configure(text=f"Last Next Task: {self.last_next_task_title}")
        else:
            self.last_next_label.configure(text="Last Next Task: —")

    def _apply_auto_next_task(self) -> None:
        """Set auto next-task based on Task Complete? checkbox and PTL."""
        complete = bool(self.task_complete_var.get())
        if complete:
            self.auto_next_task = self.top_todo[0] if self.top_todo else None
            auto_title = title_of(self.auto_next_task, "Title") if self.auto_next_task else ""
        else:
            # Stay on the same task
            if self.last_next_task_id:
                # Find it in top_todo if present, else construct a stub
                match = next((t for t in self.top_todo if t["id"] == self.last_next_task_id), None)
                self.auto_next_task = match
                auto_title = self.last_next_task_title
            else:
                self.auto_next_task = self.top_todo[0] if self.top_todo else None
                auto_title = title_of(self.auto_next_task, "Title") if self.auto_next_task else ""

        self.auto_next_title_original = auto_title
        # Set the entry without triggering the edit-detect radio
        self.next_task_var.set(auto_title)
        self._set_edit_radio_visible(False)

    def _on_task_complete_toggle(self) -> None:
        self._apply_auto_next_task()

    def _on_picker_select(self, choice: str) -> None:
        if choice == "…or pick another":
            return
        # Find the PTL row with this title (from tasks 2..5).
        for t in self.top_todo[1:]:
            if title_of(t, "Title") == choice:
                self.auto_next_task = t
                self.auto_next_title_original = choice
                self.next_task_var.set(choice)
                self._set_edit_radio_visible(False)
                break

    def _on_next_task_text_changed(self) -> None:
        current = self.next_task_var.get()
        if current.strip() and current != self.auto_next_title_original:
            self._set_edit_radio_visible(True)
        else:
            self._set_edit_radio_visible(False)

    def on_add_files(self) -> None:
        self.root.attributes("-topmost", False)
        try:
            paths = filedialog.askopenfilenames(title="Add files", parent=self.root)
        finally:
            self.root.attributes("-topmost", True)
            self.root.lift()
        for p in paths:
            if p and p not in self.files:
                self.files.append(p)
        self._render_files()

    # ── save ───────────────────────────────────────────────────────────────
    def on_save(self) -> None:
        import threading
        project_name = self.project_var.get()
        subproject_name = self.subproject_var.get()
        if not project_name or project_name == "—":
            messagebox.showerror("Missing", "Pick a project first.")
            return
        if not subproject_name or subproject_name == "—":
            messagebox.showerror("Missing", "Pick a subproject first.")
            return

        sub = self.subproject_name_to_page[subproject_name]
        proj = self.project_name_to_page[project_name]

        task_complete = bool(self.task_complete_var.get())
        next_task_text = self.next_task_var.get().strip()
        edited = next_task_text and (next_task_text != self.auto_next_title_original)
        edit_action = self.edit_action_var.get() if edited else "None"

        self.save_btn.configure(text="Saving…", state="disabled")
        self.root.withdraw()
        self.root.update()

        payload = dict(
            project_page=proj,
            subproject_page=sub,
            task_complete=task_complete,
            next_task_text=next_task_text,
            edit_action=edit_action,
            status=self._get_textbox(self.status_tb),
            open_questions=self._get_textbox(self.questions_tb),
            latest_files="\n".join(self.files),
            session_notes=self._get_textbox(self.notes_tb),
        )

        def worker():
            try:
                archive_candidate = self._do_save(payload)
            except Exception as e:
                err_str = traceback.format_exc()
                self.root.after(0, lambda err=e, tb=err_str: self._on_save_failed(err, tb))
                return
            # Fire-and-forget overview refresh.
            import subprocess, os
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--refresh-overview"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if archive_candidate is not None:
                self.root.after(0, lambda p=archive_candidate: self._show_archive_modal(p))
            else:
                self.root.after(0, self.root.destroy)

        threading.Thread(target=worker, daemon=True).start()

    def _do_save(self, payload: dict) -> dict | None:
        """Does all Notion writes. Returns the Project page dict if the user
        should be prompted to archive it, else None."""
        notion = self.notion
        cfg = self.cfg
        ps_ds = cfg["data_source_id"]
        ptl_ds = cfg["ptl_data_source_id"]
        subprojects_ds = cfg["subprojects_data_source_id"]

        proj = payload["project_page"]
        sub = payload["subproject_page"]
        task_complete = payload["task_complete"]
        edit_action = payload["edit_action"]
        next_task_text = payload["next_task_text"]

        upcoming = self.auto_next_task  # PTL row auto-filled (or picker-selected)
        upcoming_id = upcoming["id"] if upcoming else None

        # Task Worked On: the task the user went into the session on — this is
        # the Last Next Task from the previous PS row (if any).
        task_worked_on_id = self.last_next_task_id

        # Step A: compute next_task_id based on edit_action.
        next_task_id: str | None = None

        if edit_action == "None":
            next_task_id = upcoming_id

        elif edit_action == "Replace":
            if upcoming_id:
                update_ptl_title(notion, upcoming_id, next_task_text)
                next_task_id = upcoming_id
            else:
                # No upcoming to replace — create new at end.
                order, _ = compute_insert_order(notion, ptl_ds, sub["id"], None, None)
                new = create_ptl_task(notion, ptl_ds, title=next_task_text, subproject_id=sub["id"], order=order)
                next_task_id = new["id"]

        elif edit_action in ("Insert", "Skip"):
            # Find previous (the one before upcoming in top_todo), if any.
            previous = None
            if upcoming is not None and self.top_todo:
                idx = next((i for i, t in enumerate(self.top_todo) if t["id"] == upcoming["id"]), None)
                if idx is not None and idx > 0:
                    previous = self.top_todo[idx - 1]
            order, _ = compute_insert_order(notion, ptl_ds, sub["id"], upcoming, previous)
            new = create_ptl_task(notion, ptl_ds, title=next_task_text, subproject_id=sub["id"], order=order)
            next_task_id = new["id"]
            if edit_action == "Skip" and upcoming_id:
                mark_ptl_skipped(notion, upcoming_id)

        # Step B: create PS row.
        ps_row = create_snapshot_row(
            notion, ps_ds,
            project_name=title_of(proj),
            subproject_id=sub["id"],
            status=payload["status"],
            next_action_title=next_task_text or (title_of(upcoming, "Title") if upcoming else ""),
            open_questions=payload["open_questions"],
            latest_files=payload["latest_files"],
            session_notes=payload["session_notes"],
            task_complete=task_complete,
            task_worked_on_id=task_worked_on_id,
            next_task_id=next_task_id,
            edit_action=edit_action,
        )

        # Step C: if task_complete and there was a task being worked on, mark it Done.
        flipped_subproject_done = False
        if task_complete and task_worked_on_id:
            try:
                mark_ptl_done(notion, task_worked_on_id, snapshot_id=ps_row["id"])
            except Exception as e:
                print(f"WARN: could not mark PTL Done: {e}", file=sys.stderr)
            # Check if this subproject now has no Todo tasks → flip to Done.
            try:
                flipped_subproject_done = set_subproject_done_if_empty(notion, ptl_ds, sub)
            except Exception as e:
                print(f"WARN: subproject-done check failed: {e}", file=sys.stderr)

        # Step D: archive prompt check — only on the transition.
        if flipped_subproject_done:
            try:
                parent_ids = relation_ids(sub, "Parent Project")
                if parent_ids:
                    project_page_id = parent_ids[0]
                    if all_subprojects_done(notion, subprojects_ds, project_page_id):
                        proj_status = select_name(proj, "Status")
                        if proj_status != "Archived":
                            return proj
            except Exception as e:
                print(f"WARN: archive check failed: {e}", file=sys.stderr)

        return None

    def _show_archive_modal(self, project_page: dict) -> None:
        name = title_of(project_page)
        # Show the window again for the modal, then destroy on close.
        self.root.deiconify()
        self.root.lift()
        result = messagebox.askyesno(
            "Archive project?",
            f"All subprojects of “{name}” are Done.\n\nArchive it now?",
            parent=self.root,
        )
        if result:
            try:
                archive_project(self.notion, project_page["id"])
            except Exception as e:
                messagebox.showerror("Archive failed", str(e), parent=self.root)
        self.root.destroy()

    def _on_save_failed(self, err, tb: str = "") -> None:
        self.root.deiconify()
        self.save_btn.configure(text="Save Snapshot", state="normal")
        print(tb, file=sys.stderr)
        messagebox.showerror("Save failed", f"Could not write to Notion:\n{err}")

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    if "--refresh-overview" in sys.argv:
        cfg = load_config()
        notion = Client(auth=cfg["notion_token"])
        refresh_overview(notion, cfg)
        print("Overview refreshed.")
        return

    try:
        cfg = load_config()
    except Exception as e:
        try:
            messagebox.showerror("Config error", str(e))
        except Exception:
            pass
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    app = SnapshotApp(cfg)
    app.run()


if __name__ == "__main__":
    main()
