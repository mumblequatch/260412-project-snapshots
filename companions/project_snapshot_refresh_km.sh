#!/bin/bash
exec "$HOME/.venvs/project-snapshots/bin/python" \
     "$HOME/Dropbox/!Inbox/_PRO/260412_Project-Snapshots/snapshot.py" \
     --refresh-overview \
     > /tmp/snapshot_refresh.log 2>&1
