#!/usr/bin/env bash
set -uo pipefail
# Sofascore daily pass: freeze finished seasons, re-queue every open season's schedule,
# newest results page, standings and rounds, then drain the queue (unchanged pages are 304s).
# Writes only the sofascore_* tables. Mac mini only; the CLI holds a lock, so runs never overlap.
case "$(hostname)" in
    *[Mm]ac-mini*) ;;
    *) echo "run_sofascore_daily.sh runs on the Mac mini only ($(hostname))"; exit 0 ;;
esac
cd /Users/nicolaitanghoj/dev/sofascore
LOGFILE="logs/sofascore_daily.log"
mkdir -p "$(dirname "$LOGFILE")"
sleep $((RANDOM % 300))
set -a
[ -f .default.env ] && source .default.env
[ -f .development.env ] && source .development.env
set +a
echo -e "\n===== $(date '+%Y-%m-%d %H:%M:%S') — Sofascore daily =====" >> "$LOGFILE"
STATUS=0
/opt/homebrew/bin/poetry run python -m sofascore.run daily >> "$LOGFILE" 2>&1 || STATUS=$?
if [ $STATUS -ne 0 ]; then
    echo "===== Sofascore daily FAILED (exit $STATUS) =====" >> "$LOGFILE"
fi
exit $STATUS
