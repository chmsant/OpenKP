#!/usr/bin/env bash
#
# Install OpenKP's opt-in local PHI pre-commit hook.
#
# The hook runs two checks before every commit:
#
#   1. scripts/phi-scan.py over all tracked files (the same scan the release
#      checklist asks for, just run every time instead of once).
#   2. Staged added lines against your personal denylist at
#      .git/info/phi-denylist. Put names, MRNs, member IDs, or anything else
#      specific to you and your family there, one per line. Matching is
#      whole-word, fixed-string, case-insensitive. Lines starting with # are
#      comments. The file lives inside .git/, so it is never committed.
#
# It also adds *.har to .git/info/exclude so a raw capture can't be staged
# from anywhere in the tree.
#
# This is opt-in and local on purpose. phi-scan.py stays out of CI (see its
# docstring). Bypass a false positive with `git commit --no-verify`.
#
# Usage:
#   bash scripts/install-hooks.sh           # install (refuses to clobber a foreign hook)
#   bash scripts/install-hooks.sh --force   # replace an existing non-OpenKP hook

set -euo pipefail

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
GIT_DIR="$(git -C "$ROOT" rev-parse --absolute-git-dir)"
HOOK="$GIT_DIR/hooks/pre-commit"
DENYLIST="$GIT_DIR/info/phi-denylist"
EXCLUDE="$GIT_DIR/info/exclude"
MARKER="# openkp-phi-hook"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

mkdir -p "$GIT_DIR/hooks" "$GIT_DIR/info"

if [[ -e "$HOOK" ]] && ! grep -q "$MARKER" "$HOOK" && [[ "$FORCE" != "1" ]]; then
    echo "ERROR: $HOOK already exists and wasn't installed by this script." >&2
    echo "Re-run with --force to replace it, or merge the checks by hand." >&2
    exit 1
fi

cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
# openkp-phi-hook (installed by scripts/install-hooks.sh, safe to re-install)
set -u
root="$(git rev-parse --show-toplevel)"
git_dir="$(git rev-parse --absolute-git-dir)"

if ! scan_out="$(python3 -I "$root/scripts/phi-scan.py" 2>&1)"; then
    printf '%s\n' "$scan_out"
    echo "pre-commit: phi-scan.py found something. Fix it, or use --no-verify if it's a false positive."
    exit 1
fi

deny="$git_dir/info/phi-denylist"
if [[ -f "$deny" ]]; then
    patterns="$(grep -vE '^[[:space:]]*(#|$)' "$deny" || true)"
    if [[ -n "$patterns" ]]; then
        hits="$(git diff --cached -U0 --no-color \
            | grep -E '^\+' | grep -vE '^\+\+\+ ' \
            | grep -Fwi -f <(printf '%s\n' "$patterns") || true)"
        if [[ -n "$hits" ]]; then
            echo "pre-commit: staged lines match your PHI denylist ($deny):"
            printf '%s\n' "$hits" | cut -c1-160
            echo "Scrub them, or use --no-verify if it's a false positive."
            exit 1
        fi
    fi
fi
exit 0
HOOK_EOF
chmod +x "$HOOK"
echo "Installed $HOOK"

if [[ ! -f "$DENYLIST" ]]; then
    cat > "$DENYLIST" <<'DENY_EOF'
# Personal PHI denylist for the OpenKP pre-commit hook. Never committed.
# One entry per line: names, MRNs, member IDs, provider names, street names.
# Whole-word, case-insensitive, fixed-string match against staged added lines.
DENY_EOF
    echo "Created $DENYLIST (empty, add your own entries)"
else
    echo "Kept existing $DENYLIST"
fi

if ! grep -qxF '*.har' "$EXCLUDE" 2>/dev/null; then
    printf '\n# OpenKP: never commit raw captures from anywhere in the tree\n*.har\n' >> "$EXCLUDE"
    echo "Added *.har to $EXCLUDE"
fi
