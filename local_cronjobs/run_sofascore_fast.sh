#!/usr/bin/env bash
set -uo pipefail
# Sofascore fast run: queue games that started 100 min to 48 h ago and are not
# finished in sofascore_events, fetch each once with its stats and lineups, then the re-checks.
# Writes only the sofascore_* tables. Mac mini only; the CLI holds a lock, so runs never overlap.
case "$(hostname)" in
    *[Mm]ac-mini*) ;;
    *) echo "run_sofascore_fast.sh runs on the Mac mini only ($(hostname))"; exit 0 ;;
esac
cd /Users/nicolaitanghoj/dev/sofascore
LOGFILE="logs/sofascore_fast.log"
mkdir -p "$(dirname "$LOGFILE")"
sleep $((RANDOM % 60))
set -a
[ -f .default.env ] && source .default.env
[ -f .development.env ] && source .development.env
set +a
echo -e "\n===== $(date '+%Y-%m-%d %H:%M:%S') — Sofascore fast =====" >> "$LOGFILE"
STATUS=0
/opt/homebrew/bin/poetry run python -m sofascore.run fast >> "$LOGFILE" 2>&1 || STATUS=$?
if [ $STATUS -ne 0 ]; then
    echo "===== Sofascore fast FAILED (exit $STATUS) =====" >> "$LOGFILE"
fi
exit $STATUS
