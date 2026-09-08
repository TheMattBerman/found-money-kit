#!/usr/bin/env bash
# doctor.sh - structure health-check for Found Money. No network. No send.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pass=0
fail=0
warn=0

ok()   { printf "  ok   %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  FAIL %s\n" "$1"; fail=$((fail+1)); }
note() { printf "  warn %s\n" "$1"; warn=$((warn+1)); }

have_file() { [ -f "$ROOT/$1" ] && ok "$1" || bad "missing file: $1"; }
have_dir()  { [ -d "$ROOT/$1" ] && ok "$1/" || bad "missing dir: $1/"; }

echo "Found Money doctor"
echo

echo "floor:"
for f in README.md AGENTS.md LICENSE VERSION CHANGELOG.md install.sh doctor.sh .gitignore .env.example; do
  have_file "$f"
done
have_file "skills/found-money/SKILL.md"
have_dir "assets"
have_file "assets/demo.gif"

echo "package:"
have_dir "found_money"
have_file "scripts/doctor.py"
have_file "scripts/public_safety.py"
have_file "output/sample-public.json"

echo "python:"
# Diagnostics use an already-installed interpreter. Environment preparation belongs
# to explicit setup; even `uv run` can sync dependencies in a fresh checkout.
if [ -n "${FOUNDMONEY_DOCTOR_PYTHON:-}" ]; then
  PY=("$FOUNDMONEY_DOCTOR_PYTHON")
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY=("$ROOT/.venv/bin/python")
else
  PY=(python3)
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
if "${PY[@]}" -c "import sys; assert sys.version_info >= (3, 11)" 2>/dev/null; then
  ok "existing Python 3.11+ interpreter selected"
else
  bad "existing Python 3.11+ required; prepare dependencies separately or set FOUNDMONEY_DOCTOR_PYTHON to an installed interpreter"
  exit 1
fi

echo "bootstrap doctor:"
if "${PY[@]}" "$ROOT/scripts/doctor.py"; then
  ok "scripts/doctor.py"
else
  bad "scripts/doctor.py failed"
fi

echo "public-safety:"
if "${PY[@]}" "$ROOT/scripts/public_safety.py"; then
  ok "scripts/public_safety.py"
else
  bad "scripts/public_safety.py failed"
fi

echo "hygiene (current tree):"
if [ -f "$ROOT/scripts/pre-public-scan.sh" ]; then
  if SKIP_HISTORY=1 bash "$ROOT/scripts/pre-public-scan.sh" "$ROOT" >/dev/null 2>&1; then
    ok "no current-tree blockers from pre-public-scan"
  else
    note "pre-public-scan current-tree reported a finding (run: SKIP_HISTORY=1 bash scripts/pre-public-scan.sh)"
  fi
else
  note "scripts/pre-public-scan.sh missing"
fi

echo
echo "pass=$pass fail=$fail warn=$warn"
[ "$fail" -eq 0 ] && { echo "healthy."; exit 0; } || { echo "needs attention."; exit 1; }
