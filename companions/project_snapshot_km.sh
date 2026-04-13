#!/bin/bash
exec "$HOME/.venvs/project-snapshots/bin/python" \
     "$HOME/Dropbox/!Inbox/_PRO/260412_Project-Snapshots/snapshot.py" \
     > /tmp/snapshot.log 2>&1
