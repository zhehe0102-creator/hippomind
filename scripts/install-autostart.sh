#!/bin/bash
# Install a login-time daemon from an internal-disk runtime copy.
#
# macOS TCC refuses launchd background jobs on removable volumes, so this
# script copies the Python package to ~/Library/Application Support/HippoMind/
# runtime and points the LaunchAgent there.  The ledger already lives on the
# internal disk.  Re-run this script after upgrading the code to refresh the
# runtime copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/HippoMind"
RUNTIME="$SUPPORT/runtime"
PLIST="$HOME/Library/LaunchAgents/com.hippomind.daemon.plist"
PYTHON="/usr/bin/python3"
PORT="8765"

if ! command -v /usr/bin/rsync >/dev/null 2>&1; then
  echo "rsync 不可用，无法安装自启" >&2
  exit 1
fi
if [ ! -d "$ROOT/hippomind" ]; then
  echo "找不到 hippomind 包：$ROOT" >&2
  exit 1
fi

mkdir -p "$SUPPORT" "$RUNTIME" "$(dirname "$PLIST")"
echo "同步运行时代码到 $RUNTIME ..."
/usr/bin/rsync -a --delete   --exclude '.git' --exclude 'backups' --exclude 'output' --exclude 'tmp'   --exclude '__pycache__' --exclude '*.pyc' --exclude '._*'   --exclude '*.sqlite' --exclude '*.sqlite-*'   "$ROOT/hippomind" "$ROOT/integrations" "$ROOT/requirements.txt" "$RUNTIME/"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hippomind.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string><string>hippomind.daemon</string>
    <string>--ledger</string><string>$SUPPORT/hippomind.sqlite</string>
    <string>--runtime-dir</string><string>$SUPPORT</string>
    <string>--port</string><string>$PORT</string>
    <string>--watch-interval</string><string>30</string>
  </array>
  <key>WorkingDirectory</key><string>$RUNTIME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$RUNTIME</string>
    <key>PATH</key><string>/Users/zps/.local/bin:/Users/zps/.local/share/pi-node/node-v22.23.2-darwin-arm64/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$SUPPORT/hippomindd.log</string>
  <key>StandardErrorPath</key><string>$SUPPORT/hippomindd.log</string>
</dict>
</plist>
PLIST_EOF

/usr/bin/plutil -lint "$PLIST" >/dev/null
echo "已写入 $PLIST"
echo "注意：若 8765 端口已有 daemon，请先运行 python3 -m hippomind.cli stop 再登录，或手动："
echo "  launchctl bootout gui/$(id -u) '$PLIST' 2>/dev/null || true"
echo "  launchctl bootstrap gui/$(id -u) '$PLIST'"
