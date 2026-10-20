#!/usr/bin/env bash
# GrainRate end-to-end demo: non-interactive, uses a throwaway data directory,
# exits 0 when the whole walkthrough ran. Safe to re-run - it never touches
# your real ~/.grainrate data.
set -uo pipefail
cd "$(dirname "$0")"

if command -v grainrate >/dev/null 2>&1; then
  GR=(grainrate)
else
  GR=(python3 grainrate.py)
fi

DEMO_HOME="$(mktemp -d)"
export GRAINRATE_HOME="$DEMO_HOME"
export NO_COLOR=1
trap 'rm -rf "$DEMO_HOME"' EXIT

day() {
  python3 -c 'import datetime, sys; print((datetime.date.today() + datetime.timedelta(days=int(sys.argv[1]))).isoformat())' "$1"
}

quote_cmd() {
  local rendered=""
  local argument
  for argument in "$@"; do
    case "$argument" in
      *" "*) rendered="$rendered \"$argument\"" ;;
      *) rendered="$rendered $argument" ;;
    esac
  done
  printf '%s' "${rendered# }"
}

run() {
  printf '$ grainrate %s\n' "$(quote_cmd "$@")"
  "${GR[@]}" "$@" 2>&1
  printf '(exit %d)\n\n' "$?"
}

echo "GrainRate demo - data directory: $GRAINRATE_HOME"
echo "================================================================"
echo

run --version
run bin list
run bin add --name "North 1" --grain corn --moisture 14.5
run bin add --name "South 2" --grain soybean --moisture 14.2
run bin add --name "West 3" --grain wheat --moisture 13.0
run bin add --name "East 4" --grain canola --moisture 10.4
echo '$ grainrate bin add --name "West 3" --grain rye --moisture 12.0'
"${GR[@]}" bin add --name "West 3" --grain rye --moisture 12.0 2>&1
printf '(exit %d)  <- duplicate name / unknown grain / out-of-range moisture are refused\n\n' "$?"
run bin list
run report
run temp log --bin "North 1" --date "$(day -2)" --temp 18.5
run temp log --bin "North 1" --date "$(day -1)" --temp 19.2
run temp log --bin "North 1" --date "$(day 0)" --temp 20.4
run temp log --bin "South 2" --date "$(day -1)" --temp 16.0
run temp history --bin "North 1"
echo '$ printf "silo-2026\nsilo-2026\n" | grainrate auth set-passphrase'
printf 'silo-2026\nsilo-2026\n' | "${GR[@]}" auth set-passphrase 2>&1
printf '(exit %d)\n\n' "$?"
run bin list
run auth status
echo '$ printf "silo-2026\n" | grainrate auth login'
printf 'silo-2026\n' | "${GR[@]}" auth login 2>&1
printf '(exit %d)\n\n' "$?"
run bin list
run --json report
run auth logout
run report
echo "================================================================"
echo "Demo complete. Your real data lives in ~/.grainrate and was untouched."
