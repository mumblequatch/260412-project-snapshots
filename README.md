# Project Snapshots

End-of-session project status capture → Notion database, via a floating form triggered by a Keyboard Maestro hotkey.

## One-time setup

### 1. Create the Notion integration

1. Go to https://www.notion.so/my-integrations → **New integration**
2. Name: "Project Snapshots". Capabilities: **Read content**, **Update content**, **Insert content**.
3. Copy the internal integration token (starts with `secret_` or `ntn_`).

### 2. Share a parent page with the integration

Pick (or create) a Notion page where the snapshots DB and overview page should live. On that page: **... menu → Connections → add "Project Snapshots"**. Without this, the API can't see the page.

Copy the page's URL — you'll paste it into the setup script.

### 3. Install the app

```bash
cd ~/Dropbox/\!Inbox/_PRO/260412_Project-Snapshots
python3 -m venv ~/.venvs/project-snapshots
~/.venvs/project-snapshots/bin/pip install -r requirements.txt
```

### 4. Configure

```bash
mkdir -p ~/.config/project-snapshots
cp config_example.json ~/.config/project-snapshots/config.json
# edit ~/.config/project-snapshots/config.json and paste the notion_token
```

### 5. Run the setup script

```bash
~/.venvs/project-snapshots/bin/python setup_notion.py
# paste the parent page URL when prompted
```

This creates the **Project Snapshots** database and the **Project Overview** page, and writes both IDs back to the config file.

### 6. Keyboard Maestro macro

- **Trigger:** Ctrl+Opt+S (or your choice)
- **Action:** Execute Shell Script — **with "Ignore Results" set**
- **Script:**
  ```
  ~/.venvs/project-snapshots/bin/python ~/Dropbox/\!Inbox/_PRO/260412_Project-Snapshots/snapshot.py
  ```

## Daily use

Hit the hotkey → pick project → fields pre-populate from the last snapshot → edit → Cmd+Enter.

## CLI

```bash
~/.venvs/project-snapshots/bin/python snapshot.py                      # launch the form
~/.venvs/project-snapshots/bin/python snapshot.py --refresh-overview   # regenerate the overview page
```

## Adding / renaming projects

Edit the **Project** Select options directly in the Notion database (click the column header). The app reads options from Notion at launch, so no code changes needed.

## Files

- `setup_notion.py` — one-time DB + overview page creation
- `snapshot.py` — main app; KM triggers this
- `config_example.json` — template for `~/.config/project-snapshots/config.json`
- `requirements.txt` — `notion-client`, `customtkinter`

## Notes

- Files added via "Add Files…" are stored as local absolute paths, one per line, in the Latest Files column. Notion renders them as plain text; drag into Notion manually if you want a preview.
- Session Notes is always blank on open (per-session).
- Overview refresh failures are non-fatal — the snapshot save still counts. If the overview looks stale, run `snapshot.py --refresh-overview`.
