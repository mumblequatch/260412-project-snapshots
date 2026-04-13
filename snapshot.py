#!/usr/bin/env python3
"""Project Snapshots — end-of-session capture form.

Triggered by a Keyboard Maestro hotkey. Shows a floating form, pre-populates
with the latest snapshot for the selected project, writes a new row to Notion
on save, and refreshes the Project Overview page.

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

from notion_client import Client
from notion_client.errors import APIResponseError

CONFIG_PATH = Path.home() / ".config" / "project-snapshots" / "config.json"


# ── Notion helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config not found at {CONFIG_PATH}. Run setup_notion.py.")
    cfg = json.loads(CONFIG_PATH.read_text())
    for key in ("notion_token", "database_id", "data_source_id", "overview_page_id"):
        if not cfg.get(key):
            raise RuntimeError(f"Config missing '{key}'. Run setup_notion.py.")
    return cfg


def rt(text: str) -> list:
    """Build a rich_text payload. Notion caps at 2000 chars per block."""
    if not text:
        return []
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def rt_files(paths: list[str]) -> list:
    """Build rich_text. http(s) URLs are clickable; local paths stay plain (Notion rejects custom schemes)."""
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


def fetch_select_options(notion: Client, data_source_id: str) -> list[str]:
    ds = notion.data_sources.retrieve(data_source_id=data_source_id)
    proj = ds.get("properties", {}).get("Project", {})
    opts = proj.get("select", {}).get("options", [])
    return [o["name"] for o in opts]


def fetch_latest_snapshot(notion: Client, data_source_id: str, project: str) -> dict | None:
    resp = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "Project", "select": {"equals": project}},
        sorts=[{"property": "Timestamp", "direction": "descending"}],
        page_size=1,
    )
    results = resp.get("results", [])
    if not results:
        return None
    page = results[0]
    props = page["properties"]
    ts = (props.get("Timestamp") or {}).get("date") or {}
    return {
        "status": rt_to_plain(props.get("Status")),
        "next_action": rt_to_plain(props.get("Next Action")),
        "open_questions": rt_to_plain(props.get("Open Questions")),
        "latest_files": rt_to_plain(props.get("Latest Files")),
        "timestamp": ts.get("start", ""),
        "page_url": page.get("url", ""),
    }


def create_snapshot(
    notion: Client,
    data_source_id: str,
    *,
    project: str,
    status: str,
    next_action: str,
    open_questions: str,
    latest_files: str,
    session_notes: str,
) -> None:
    now = datetime.now().astimezone()
    now_iso = now.isoformat()
    title = f"{project} · {now.strftime('%Y-%m-%d %H:%M')}"
    file_lines = [l for l in latest_files.splitlines() if l.strip()]
    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id},
        properties={
            "Name": {"title": rt(title)},
            "Project": {"select": {"name": project}},
            "Status": {"rich_text": rt(status)},
            "Next Action": {"rich_text": rt(next_action)},
            "Open Questions": {"rich_text": rt(open_questions)},
            "Latest Files": {"rich_text": rt_files(file_lines)},
            "Session Notes": {"rich_text": rt(session_notes)},
            "Timestamp": {"date": {"start": now_iso}},
        },
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
    ds_id = cfg["data_source_id"]
    overview_id = cfg["overview_page_id"]

    projects = fetch_select_options(notion, ds_id)
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
    for project in projects:
        latest = fetch_latest_snapshot(notion, ds_id, project)
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
                    cell(project, bold=True),
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

    # Append in chunks of 100 (Notion API limit).
    for i in range(0, len(children), 100):
        notion.blocks.children.append(block_id=overview_id, children=children[i:i + 100])


# ── GUI ──────────────────────────────────────────────────────────────────────

class SnapshotApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.notion = Client(auth=cfg["notion_token"])

        try:
            self.projects = fetch_select_options(self.notion, cfg["data_source_id"])
        except Exception as e:
            messagebox.showerror("Notion error", f"Could not fetch projects:\n{e}")
            sys.exit(1)

        if not self.projects:
            messagebox.showerror("Setup error", "No projects found in the Notion database Select.")
            sys.exit(1)

        self.files: list[str] = []

        if USING_CTK:
            ctk.set_appearance_mode("System")
            ctk.set_default_color_theme("blue")
            self.root = ctk.CTk()
        else:
            self.root = ctk.Tk()

        self.root.title("Project Snapshot")
        self.root.attributes("-topmost", True)
        self._center(620, 880)
        self.root.resizable(True, True)
        self.root.minsize(560, 700)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<Command-Return>", lambda _e: self.on_save())

        self._build_ui()
        self.on_project_change("—")

    def _center(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _label(self, parent, text: str):
        if USING_CTK:
            return ctk.CTkLabel(parent, text=text, anchor="w")
        return ctk.Label(parent, text=text, anchor="w")

    def _textbox(self, parent, height: int):
        if USING_CTK:
            tb = ctk.CTkTextbox(parent, height=height * 22, wrap="word")
            inner = tb
        else:
            import tkinter as tk
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

    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 4}

        btn_row = ctk.CTkFrame(self.root) if USING_CTK else ctk.Frame(self.root)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)
        cancel_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            btn_row, text="Cancel", command=self.root.destroy,
        )
        cancel_btn.pack(side="right", padx=(8, 0))
        self.save_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            btn_row, text="Save Snapshot", command=self.on_save,
        )
        self.save_btn.pack(side="right")

        self._label(self.root, "Project").pack(fill="x", **pad)
        menu_values = ["—"] + list(self.projects)
        if USING_CTK:
            self.project_var = ctk.StringVar(value="—")
            self.project_menu = ctk.CTkOptionMenu(
                self.root, values=menu_values, variable=self.project_var,
                command=self.on_project_change,
            )
        else:
            import tkinter as tk
            self.project_var = tk.StringVar(value="—")
            self.project_menu = tk.OptionMenu(
                self.root, self.project_var, *menu_values,
                command=self.on_project_change,
            )
        self.project_menu.pack(fill="x", **pad)

        self._label(self.root, "Status").pack(fill="x", **pad)
        self.status_tb = self._textbox(self.root, 3)
        self.status_tb.pack(fill="x", **pad)

        self._label(self.root, "Next Action").pack(fill="x", **pad)
        self.next_tb = self._textbox(self.root, 3)
        self.next_tb.pack(fill="x", **pad)

        self._label(self.root, "Open Questions").pack(fill="x", **pad)
        self.questions_tb = self._textbox(self.root, 4)
        self.questions_tb.pack(fill="x", **pad)

        self._label(self.root, "Latest Files").pack(fill="x", **pad)
        files_row = ctk.CTkFrame(self.root) if USING_CTK else ctk.Frame(self.root)
        files_row.pack(fill="x", **pad)
        add_btn = (ctk.CTkButton if USING_CTK else ctk.Button)(
            files_row, text="Add Files…", command=self.on_add_files,
        )
        add_btn.pack(side="left")
        self.files_frame = ctk.CTkFrame(self.root) if USING_CTK else ctk.Frame(self.root)
        self.files_frame.pack(fill="x", **pad)

        self._label(self.root, "Session Notes").pack(fill="x", **pad)
        self.notes_tb = self._textbox(self.root, 3)
        self.notes_tb.pack(fill="x", **pad)

    def _set_textbox(self, tb, value: str) -> None:
        tb.delete("1.0", "end")
        if value:
            tb.insert("1.0", value)

    def _get_textbox(self, tb) -> str:
        return tb.get("1.0", "end").strip()

    def _render_files(self) -> None:
        for child in self.files_frame.winfo_children():
            child.destroy()
        for idx, path in enumerate(self.files):
            row = ctk.CTkFrame(self.files_frame) if USING_CTK else ctk.Frame(self.files_frame)
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

    def on_project_change(self, project: str) -> None:
        self._set_textbox(self.status_tb, "")
        self._set_textbox(self.next_tb, "")
        self._set_textbox(self.questions_tb, "")
        self.files = []
        self._render_files()

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

    def on_save(self) -> None:
        import threading
        project = self.project_var.get()
        if not project or project == "—":
            messagebox.showerror("Missing", "Pick a project first.")
            return

        self.save_btn.configure(text="Saving…", state="disabled")
        self.root.withdraw()
        self.root.update()
        payload = dict(
            project=project,
            status=self._get_textbox(self.status_tb),
            next_action=self._get_textbox(self.next_tb),
            open_questions=self._get_textbox(self.questions_tb),
            latest_files="\n".join(self.files),
            session_notes=self._get_textbox(self.notes_tb),
        )

        def worker():
            try:
                create_snapshot(self.notion, self.cfg["data_source_id"], **payload)
            except Exception as e:
                self.root.after(0, lambda err=e: self._on_save_failed(err))
                return
            import subprocess, os
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--refresh-overview"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.root.after(0, self.root.destroy)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_failed(self, err) -> None:
        self.root.deiconify()
        self.save_btn.configure(text="Save Snapshot", state="normal")
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
