#!/usr/bin/env bash
# Refresh the vendored skills from upstream, then review `git diff share/skills` before committing.
# Run from anywhere, as any user (no sudo). Our own skills (council) are never touched.
# After committing, the owner re-runs install.sh to publish the new catalog.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# repo | license file to keep (as LICENSE-<name>) | skills to take
SOURCES=(
  "t3dotgg/tstack|LICENSE|babysit-pr file-pr full-send full-stop leaving-pr-comments modernize sol-loop takeover"
  "anthropics/skills|skills/skill-creator/LICENSE.txt|skill-creator webapp-testing frontend-design mcp-builder"
)

: > "$tmp/UPSTREAM"
for src in "${SOURCES[@]}"; do
  IFS='|' read -r repo lic skills <<<"$src"
  name=${repo#*/}; [[ $name == skills ]] && name=${repo%%/*}
  git clone -q --depth 1 "https://github.com/$repo" "$tmp/$name"
  sha=$(git -C "$tmp/$name" rev-parse --short HEAD)
  for s in $skills; do
    [[ -f $tmp/$name/skills/$s/SKILL.md ]] || { echo "upstream $repo no longer has $s; keeping ours" >&2; continue; }
    rm -rf "${here:?}/$s"
    cp -R "$tmp/$name/skills/$s" "$here/$s"
    rm -rf "$here/$s/.git"
  done
  cp "$tmp/$name/$lic" "$here/LICENSE-$name"
  echo "$repo $sha $(git -C "$tmp/$name" log -1 --format=%cs) $skills" >> "$tmp/UPSTREAM"
done
cp "$tmp/UPSTREAM" "$here/UPSTREAM"
echo "synced:"; cat "$here/UPSTREAM"
echo "now review: git -C '$here' diff --stat -- . ; git -C '$here' status --short -- ."
