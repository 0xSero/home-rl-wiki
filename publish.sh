#!/bin/sh
# Push local wiki edits to the public copy on GitHub Pages.
set -e
cd "$(dirname "$0")"
git add -A content/pages seed.wiki
git commit -m "${1:-Wiki edits}" || { echo "Nothing to publish."; exit 0; }
git pull --rebase --quiet
git push --quiet
echo "Pushed. The public site updates in about a minute."
