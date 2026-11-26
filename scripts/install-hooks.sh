#!/usr/bin/env bash
#
# Install OpenKP's opt-in local PHI pre-commit hook.
#
# The hook runs two checks before every commit:
#
#   1. scripts/phi-scan.py --staged over the staged version of every tracked
#      file (the scan the release checklist asks for, run on what is actually
#      about to be committed).
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
# The common dir, not --absolute-git-dir: from a linked worktree the latter
# is .git/worktrees/<name>, where git never looks for hooks.
GIT_DIR="$(cd "$ROOT" && cd "$(git rev-parse --git-common-dir)" && pwd)"
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
git_dir="$(cd "$(git rev-parse --git-common-dir)" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "pre-commit: python3 not found, so the PHI scan couldn't run. Install it,"
    echo "or use --no-verify if you've checked the commit yourself."
    exit 1
fi
# Scan the staged blobs, not the working tree, so what's checked is what's
# committed. phi-scan.py prints the matched values, so keep its output out of
# here. Exit 1 means a finding, anything else means the scan itself failed.
# Branches older than --staged (for example one cut from upstream main)
# get the working-tree scan instead, with a note.
scan_args=(--staged)
if ! command grep -q -- '--staged' "$root/scripts/phi-scan.py" 2>/dev/null; then
    scan_args=()
    echo "pre-commit: this branch's phi-scan.py has no --staged, so the working tree was scanned."
fi
(cd "$root" && python3 -I scripts/phi-scan.py "${scan_args[@]+"${scan_args[@]}"}" >/dev/null 2>&1)
rc=$?
if [[ "$rc" == 1 ]]; then
    echo "pre-commit: phi-scan.py found something in the staged files. Run"
    echo "'python3 scripts/phi-scan.py --staged' in your own terminal to see it,"
    echo "or use --no-verify if it's a false positive."
    exit 1
elif [[ "$rc" != 0 ]]; then
    echo "pre-commit: phi-scan.py failed to run (exit $rc), so nothing was checked."
    echo "Run 'python3 scripts/phi-scan.py --staged' to see why."
    exit 1
fi

deny="$git_dir/info/phi-denylist"
if [[ -f "$deny" ]]; then
    # Drop comments and blank lines. An empty pattern would match everything.
    patterns="$(command grep -vE '^[[:space:]]*(#|$)' "$deny" || true)"
    if [[ -n "$patterns" ]]; then
        # Report file names and counts only, never the matching text. A commit
        # run from an AI coding session would otherwise copy the PHI it just
        # caught into the session transcript.
        found=0
        while IFS= read -r -d '' file; do
            n="$(git diff --cached -U0 --no-color -- "$file" \
                | command grep -E '^\+' | command grep -vE '^\+\+\+ ' \
                | command grep -Fwic -f <(printf '%s\n' "$patterns") || true)"
            if [[ "${n:-0}" -gt 0 ]]; then
                [[ "$found" == 0 ]] && echo "pre-commit: staged lines match your PHI denylist:"
                echo "  $file: $n line(s)"
                found=1
            fi
        done < <(git diff --cached --name-only -z --diff-filter=ACMR)
        if [[ "$found" == 1 ]]; then
            echo "Matched text is not shown. Check those files, or use --no-verify if it's a false positive."
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
