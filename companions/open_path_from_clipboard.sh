#!/bin/bash
# Read a file path from the clipboard and open it (or reveal in Finder if modifier held).
# Bind to a Keyboard Maestro hotkey. Works with absolute paths or ~-prefixed paths.

# KM macro performs the Copy action natively before invoking this script.
raw=$(pbpaste)
# Trim whitespace and surrounding quotes
path=$(printf '%s' "$raw" | awk '{$1=$1;print}' | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')

# Expand leading ~ to $HOME
case "$path" in
    "~/"*) path="$HOME/${path#\~/}" ;;
    "~")   path="$HOME" ;;
esac

if [ -z "$path" ]; then
    osascript -e 'display notification "Clipboard is empty" with title "Open path"'
    exit 1
fi

echo "[open_path] raw=[$raw]" > /tmp/open_path.log
echo "[open_path] path=[$path]" >> /tmp/open_path.log
if [ ! -e "$path" ]; then
    osascript -e "display notification \"Not found: $path\" with title \"Open path\""
    exit 1
fi

open -R "$path"
