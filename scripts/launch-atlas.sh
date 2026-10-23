#!/bin/bash
# One-click launcher for HippoMind Atlas.
#
# Double-clicking the .app bundle or the .command file runs this script:
#   1. ensure hippomindd is running (start it if needed),
#   2. mint a one-time UI token and open the console in the default browser,
#   3. show a native dialog with the log path if anything fails.
#
# It never edits the ledger.  All output goes to ~/Library/Logs/HippoMind-Atlas.log
# unless HIPPOMIND_LAUNCH_DRY_RUN=1 (then it prints the URL and opens nothing).
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/HippoMind-Atlas.log"
DRY_RUN=0
if [ -n "$(printenv HIPPOMIND_LAUNCH_DRY_RUN || true)" ]; then
  DRY_RUN="$(printenv HIPPOMIND_LAUNCH_DRY_RUN)"
fi
if [ "$DRY_RUN" != "1" ]; then
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  exec >>"$LOG" 2>&1
fi
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') HippoMind Atlas launch ==="
echo "root=$ROOT"

dialog() {
  /usr/bin/osascript -e "display dialog \"$1\" buttons {\"好\"} default button 1 with title \"HippoMind Atlas\" with icon caution" >/dev/null 2>&1 || true
}

fail() {
  echo "FAIL: $1"
  if [ "$DRY_RUN" != "1" ]; then dialog "$1"; fi
  exit 1
}

cd "$ROOT" || fail "无法进入工作区目录：$ROOT"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then PY="/usr/bin/python3"; fi
if [ ! -x "$PY" ]; then fail "找不到 Python 3。请安装 Python 3.9+，或运行 scripts/bootstrap-env.sh 检查环境。"; fi

# 1) daemon
if ! /usr/bin/curl -fsS -m 2 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  echo "daemon 未运行，正在启动…"
  "$PY" -m hippomind.cli start >/dev/null 2>&1 || true
  i=0
  while [ "$i" -lt 40 ]; do
    if /usr/bin/curl -fsS -m 1 http://127.0.0.1:8765/health >/dev/null 2>&1; then break; fi
    i=$((i + 1))
    /bin/sleep 0.25
  done
fi
if ! /usr/bin/curl -fsS -m 2 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  fail "本地记忆服务没有启动成功。请查看日志：$LOG 或运行：python3 -m hippomind.cli start"
fi

# 2) one-time token + URL (do not put the token in a URL or shell history)
URL="$("$PY" -m hippomind.cli ui --no-open 2>>"$LOG" | /usr/bin/tail -n 1)"
case "$URL" in
  http*) ;;
  *)
    echo "一次性登录 URL 生成失败，回退到静态 /ui/（页面内可粘贴令牌）。"
    URL="http://127.0.0.1:8765/ui/"
    ;;
esac
echo "url=$URL"

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN: 不打开浏览器。"
  exit 0
fi

# 3) browser
/usr/bin/open "$URL" || fail "无法打开浏览器。请手动访问：http://127.0.0.1:8765/ui/"
/usr/bin/osascript -e 'display notification "HippoMind Atlas 已打开" with title "HippoMind Atlas"' >/dev/null 2>&1 || true
exit 0
