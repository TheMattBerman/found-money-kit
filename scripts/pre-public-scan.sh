#!/usr/bin/env bash
#
# pre-public-scan.sh
# -----------------------------------------------------------------------------
# A portable, one-command hygiene gate to run BEFORE making any git repo public.
#
# WHY: Making a repo public exposes its ENTIRE git history, not just the current
# tree. A secret you committed and later deleted is still recoverable from
# history forever. This script scans BOTH the current tracked tree AND the full
# git history so you catch leaks that a plain `ls`/grep of the working tree
# would miss.
#
# USAGE:
#   pre-public-scan.sh [repo-path]      # defaults to current directory
#   pre-public-scan.sh --help
#
# ENV OVERRIDES:
#   EXTRA_TERMS="acme|globex|client-x"  # extra client/PII terms to flag (regex
#                                       # alternation, case-insensitive). These
#                                       # are WARNINGS. Keep real client names
#                                       # OUT of this script — pass them here.
#   SKIP_HISTORY=1                      # scan only the current tree (faster;
#                                       # use for the in-repo "doctor" check).
#
# EXIT CODE:
#   0  = no BLOCKERS (warnings may still be present — clear them before public)
#   1  = at least one BLOCKER found (do NOT make the repo public)
#   2  = usage / environment error (not a git repo, etc.)
#
# OUTPUT CONTRACT:
#   - Every section is labeled.
#   - Secret VALUES are NEVER printed. For real-secret matches we print only
#     `commit:file` (history) or `file:line` (current tree) plus a masked token.
#   - A final verdict summarizes BLOCKERS vs WARNINGS.
#
# Portable bash: works under bash 3.2 (macOS default) and modern bash/Linux.
# No external deps beyond git, grep, and coreutils.
# -----------------------------------------------------------------------------

set -uo pipefail

# ---- args / help ------------------------------------------------------------

print_help() {
  sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
}

REPO_PATH="."
case "${1:-}" in
  -h|--help) print_help; exit 0 ;;
  "") : ;;
  *) REPO_PATH="$1" ;;
esac

# ---- setup ------------------------------------------------------------------

if ! cd "$REPO_PATH" 2>/dev/null; then
  echo "ERROR: cannot cd into '$REPO_PATH'" >&2
  exit 2
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: '$REPO_PATH' is not inside a git repository" >&2
  exit 2
fi

# Move to repo root so all paths are repo-relative and history scans are total.
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT" || { echo "ERROR: cannot cd to repo root" >&2; exit 2; }

BLOCKERS=0
WARNINGS=0
# Accumulated human-readable findings for the final summary.
BLOCKER_LINES=""
WARNING_LINES=""

add_blocker() { BLOCKERS=$((BLOCKERS+1)); BLOCKER_LINES="${BLOCKER_LINES}  - $1"$'\n'; }
add_warning() { WARNINGS=$((WARNINGS+1)); WARNING_LINES="${WARNING_LINES}  - $1"$'\n'; }

section() {
  echo
  echo "============================================================"
  echo "  $1"
  echo "============================================================"
}

# Mask a matched token so we never print a live secret. Keeps a short prefix
# and a short suffix, blanks the middle.
mask() {
  awk '{
    s=$0
    n=length(s)
    if (n <= 10) { print "********" }
    else { print substr(s,1,4) "…" substr(s,n-3,4) " (" n " chars, masked)" }
  }'
}

# All commits (for history scans). Empty if the repo has no commits yet.
ALL_COMMITS="$(git rev-list --all 2>/dev/null || true)"
HAS_HISTORY=1
[ -z "$ALL_COMMITS" ] && HAS_HISTORY=0

echo "pre-public-scan"
echo "repo: $REPO_ROOT"
echo "origin: $(git remote get-url origin 2>/dev/null || echo '(none)')"
echo "commits in history: $(git rev-list --all --count 2>/dev/null || echo 0)"
if [ "${SKIP_HISTORY:-0}" = "1" ]; then
  echo "mode: CURRENT TREE ONLY (SKIP_HISTORY=1)"
else
  echo "mode: current tree + FULL history"
fi

# Helper: grep the current tracked tree (HEAD) for a pattern.
# Prints matching file:line with the matched value masked, returns 0 if hits.
grep_current() {
  local pattern="$1"
  git grep -nIE "$pattern" -- . 2>/dev/null
}

# Helper: list files in FULL history matching a pattern. Prints commit:file.
# We deliberately use -l (file names only) so we never echo the secret line.
grep_history_files() {
  local pattern="$1"
  [ "$HAS_HISTORY" -eq 1 ] || return 0
  # shellcheck disable=SC2086
  git grep -lIE "$pattern" $ALL_COMMITS 2>/dev/null
}

# =============================================================================
# CHECK 1 — Real secret / key patterns (CURRENT TREE + FULL HISTORY)  [BLOCKER]
# =============================================================================
section "1. SECRETS / KEYS  (current tree + full history)  [BLOCKER]"

# Each entry: label::regex
# Patterns target high-signal credential shapes to minimize false positives.
#
# Design notes on false-positive control:
#  - The OpenAI/GitHub/Slack patterns require a NON-word char (or start of line)
#    immediately before the prefix so we don't match the prefix mid-word, e.g.
#    "ask-a-doctor-...glycinate" must NOT match the OpenAI "sk-" pattern.
#  - The generic-secret pattern requires the VALUE to be a QUOTED string literal.
#    Real hard-coded secrets are quoted; code that reads creds from env
#    (e.g. `api_key = discover_api_key()`, `login, password = get_creds()`) is
#    NOT quoted and is correctly ignored.
SECRET_PATTERNS=(
  "OpenAI key::(^|[^A-Za-z0-9])sk-(proj-)?[A-Za-z0-9]{20,}"
  "AWS access key id::(^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}"
  "GitHub token::(^|[^A-Za-z0-9])gh[posru]_[A-Za-z0-9]{20,}"
  "Slack token::(^|[^A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"
  "Bearer token::Bearer [A-Za-z0-9._-]{20,}"
  "Private key block::-----BEGIN [A-Z ]*PRIVATE KEY-----"
  "Generic secret literal::(api[_-]?key|secret|token|password)[\"']?[[:space:]]*[:=][[:space:]]*[\"'][A-Za-z0-9_-]{16,}[\"']"
)

secret_hit=0
for entry in "${SECRET_PATTERNS[@]}"; do
  label="${entry%%::*}"
  pattern="${entry#*::}"

  # --- current tree ---
  cur="$(grep_current "$pattern" || true)"
  if [ -f "$REPO_ROOT/scripts/hygiene_fixtures.py" ] && command -v python3 >/dev/null 2>&1; then
    if filtered_cur="$(printf '%s\n' "$cur" | python3 "$REPO_ROOT/scripts/hygiene_fixtures.py" "$label" current)"; then
      cur="$filtered_cur"
    fi
  fi
  if [ -n "$cur" ]; then
    secret_hit=1
    echo
    echo "[$label] in CURRENT TREE:"
    # Print file:line and a masked token (extract first match from the line).
    while IFS= read -r line; do
      file_line="${line%%:*}:$(echo "$line" | cut -d: -f2)"
      token="$(echo "$line" | grep -oE "$pattern" | head -1)"
      masked="$(printf '%s' "$token" | mask)"
      echo "    $file_line  -> $masked"
    done <<< "$cur"
  fi

  # --- full history ---
  if [ "${SKIP_HISTORY:-0}" != "1" ] && [ "$HAS_HISTORY" -eq 1 ]; then
    if [ -f "$REPO_ROOT/scripts/hygiene_fixtures.py" ] && command -v python3 >/dev/null 2>&1; then
      # Values stay inside the filter; history findings still print only commit:file.
      if ! histfiles="$(git grep -nIE "$pattern" $ALL_COMMITS 2>/dev/null | python3 "$REPO_ROOT/scripts/hygiene_fixtures.py" "$label" history)"; then
        histfiles="$(grep_history_files "$pattern" || true)"
      fi
    else
      histfiles="$(grep_history_files "$pattern" || true)"
    fi
    if [ -n "$histfiles" ]; then
      secret_hit=1
      echo
      echo "[$label] in HISTORY (commit:file — value withheld):"
      echo "$histfiles" | sort -u | head -50 | sed 's/^/    /'
      extra="$(echo "$histfiles" | sort -u | wc -l | tr -d ' ')"
      [ "$extra" -gt 50 ] && echo "    ...(+$((extra-50)) more)"
    fi
  fi
done

if [ "$secret_hit" -eq 1 ]; then
  echo
  echo "  >> Real secret pattern(s) detected. Treat each as a leak."
  add_blocker "Secret/key pattern(s) matched (current tree and/or history). See section 1."
else
  echo "  ok  no secret/key patterns found."
fi

# =============================================================================
# CHECK 2 — Absolute local paths in current tree                    [WARNING]
# =============================================================================
section "2. ABSOLUTE LOCAL PATHS  (current tree)  [WARNING]"

# /Users/<name> (macOS) and /home/<name> (Linux). Leaks username + machine layout.
PATH_PATTERN="(/Users/|/home/)[A-Za-z0-9._-]+"
cur_paths="$(grep_current "$PATH_PATTERN" || true)"
# Also check history so the operator understands why a clean tree may still be dirty.
hist_paths=""
if [ "${SKIP_HISTORY:-0}" != "1" ]; then
  hist_paths="$(grep_history_files "$PATH_PATTERN" || true)"
fi

if [ -n "$cur_paths" ]; then
  echo "  CURRENT TREE matches:"
  echo "$cur_paths" | head -40 | sed 's/^/    /'
  c="$(echo "$cur_paths" | wc -l | tr -d ' ')"
  [ "$c" -gt 40 ] && echo "    ...(+$((c-40)) more)"
  add_warning "Absolute local paths ($PATH_PATTERN) present in CURRENT tree."
else
  echo "  ok  no absolute local paths in current tree."
fi

if [ -n "$hist_paths" ]; then
  echo
  echo "  HISTORY matches (commit:file) — these ship if you make THIS history public:"
  echo "$hist_paths" | sort -u | head -40 | sed 's/^/    /'
  h="$(echo "$hist_paths" | sort -u | wc -l | tr -d ' ')"
  [ "$h" -gt 40 ] && echo "    ...(+$((h-40)) more)"
  add_warning "Absolute local paths present in git HISTORY (current tree may be clean). See the dev/public-split playbook."
fi

# =============================================================================
# CHECK 3 — Account / usage metadata                                [WARNING]
# =============================================================================
section "3. ACCOUNT / USAGE METADATA  (current tree + history)  [WARNING]"

META_PATTERN="credits_remaining|credits_used|remaining_credits|account_balance|usage_credits"
cur_meta="$(grep_current "$META_PATTERN" || true)"
hist_meta=""
if [ "${SKIP_HISTORY:-0}" != "1" ]; then
  hist_meta="$(grep_history_files "$META_PATTERN" || true)"
fi

if [ -n "$cur_meta" ]; then
  echo "  CURRENT TREE matches:"
  echo "$cur_meta" | head -40 | sed 's/^/    /'
  add_warning "Account/usage metadata ($META_PATTERN) present in CURRENT tree."
else
  echo "  ok  no account/usage metadata in current tree."
fi

if [ -n "$hist_meta" ]; then
  echo
  echo "  HISTORY matches (commit:file):"
  echo "$hist_meta" | sort -u | head -40 | sed 's/^/    /'
  add_warning "Account/usage metadata present in git HISTORY. See the dev/public-split playbook."
fi

# =============================================================================
# CHECK 4 — Tracked secret files                                    [BLOCKER]
# =============================================================================
section "4. TRACKED SECRET FILES  (current tree)  [BLOCKER]"

# List all tracked files, filter for dangerous shapes. .env.example is allowed.
tracked="$(git ls-files)"
secret_files="$(printf '%s\n' "$tracked" | grep -E '(^|/)\.env($|\.)|secret|credential|\.pem$|\.key$' | grep -vE '\.env\.example$|\.env\.sample$|\.env\.template$' || true)"

if [ -n "$secret_files" ]; then
  echo "  Tracked files that look like secrets:"
  echo "$secret_files" | sed 's/^/    /'
  add_blocker "Tracked secret-shaped file(s) present: $(echo "$secret_files" | tr '\n' ' ')"
else
  echo "  ok  no tracked .env / *secret* / *credential* / *.pem / *.key files."
fi

# =============================================================================
# CHECK 5 — .gitignore coverage                                     [WARNING]
# =============================================================================
section "5. .GITIGNORE COVERAGE  [WARNING]"

if [ -f .gitignore ]; then
  missing=""
  for needle in ".env" "*.pem" "*.key"; do
    if ! grep -qF "$needle" .gitignore; then
      missing="${missing} ${needle}"
    fi
  done
  if [ -n "$missing" ]; then
    echo "  .gitignore is missing entries for:$missing"
    add_warning ".gitignore does not cover:$missing"
  else
    echo "  ok  .gitignore covers .env / *.pem / *.key."
  fi
else
  echo "  no .gitignore file found."
  add_warning "No .gitignore present (recommended before going public)."
fi

# =============================================================================
# CHECK 6 — Configurable client / PII term scan                     [WARNING]
# =============================================================================
section "6. CLIENT / PII TERM SCAN  (configurable)  [WARNING]"

# Default list is intentionally GENERIC. Do NOT add real client names here —
# pass them at run time via EXTRA_TERMS="acme|globex".
DEFAULT_TERMS="confidential|internal[- ]only|do[- ]not[- ]distribute|client[- ]name|REDACTED|TODO[- ]?REMOVE"
TERMS="$DEFAULT_TERMS"
if [ -n "${EXTRA_TERMS:-}" ]; then
  TERMS="${TERMS}|${EXTRA_TERMS}"
  echo "  (EXTRA_TERMS active)"
fi
echo "  terms: $TERMS"

cur_terms="$(git grep -nIE -i "$TERMS" -- . 2>/dev/null || true)"
if [ -n "$cur_terms" ]; then
  echo "  CURRENT TREE matches:"
  echo "$cur_terms" | head -40 | sed 's/^/    /'
  c="$(echo "$cur_terms" | wc -l | tr -d ' ')"
  [ "$c" -gt 40 ] && echo "    ...(+$((c-40)) more)"
  add_warning "Client/PII terms matched in current tree (review section 6)."
else
  echo "  ok  no configured client/PII terms found in current tree."
fi

# =============================================================================
# CHECK 7 — Email addresses in tracked files                        [WARNING]
# =============================================================================
section "7. EMAIL ADDRESSES  (current tree)  [WARNING]"

EMAIL_PATTERN="[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
# Exclude common noise: example.com, noreply, your-, schema URLs.
cur_emails="$(git grep -nIE "$EMAIL_PATTERN" -- . 2>/dev/null \
  | grep -viE 'example\.(com|org)|noreply|your-?email|user@|@example|@domain' || true)"
if [ -n "$cur_emails" ]; then
  echo "  CURRENT TREE matches (review — some may be intentional, e.g. LICENSE):"
  echo "$cur_emails" | head -40 | sed 's/^/    /'
  c="$(echo "$cur_emails" | wc -l | tr -d ' ')"
  [ "$c" -gt 40 ] && echo "    ...(+$((c-40)) more)"
  add_warning "Email address(es) present in current tree (review section 7)."
else
  echo "  ok  no notable email addresses in current tree."
fi

# =============================================================================
# FINAL VERDICT
# =============================================================================
section "VERDICT"

echo "BLOCKERS: $BLOCKERS"
if [ "$BLOCKERS" -gt 0 ]; then
  printf '%s' "$BLOCKER_LINES"
fi
echo
echo "WARNINGS: $WARNINGS  (clear these before going public)"
if [ "$WARNINGS" -gt 0 ]; then
  printf '%s' "$WARNING_LINES"
fi
echo
if [ "$BLOCKERS" -gt 0 ]; then
  echo ">> DO NOT make this repo public. Resolve BLOCKERS first."
  echo ">> If blockers/warnings are in HISTORY, use the dev/public-split playbook"
  echo "   (rename dirty repo to <name>-dev + private, publish a fresh single-commit"
  echo "   snapshot at the same URL). See the pre-public-scan SKILL.md."
  exit 1
else
  if [ "$WARNINGS" -gt 0 ]; then
    echo ">> No blockers. Warnings above should be cleared before going public."
    echo ">> If warnings are only in HISTORY, the dev/public-split playbook is the"
    echo "   correct remedy — you cannot make that history public cleanly."
  else
    echo ">> Clean. Safe to proceed (re-verify origin + visibility before flipping public)."
  fi
  exit 0
fi
