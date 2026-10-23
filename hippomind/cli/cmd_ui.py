"""hippomind ui — open the HippoMind Atlas console in the browser.

The console is served by hippomindd at /ui.  The page itself is static and
carries no credential, so the CLI mints a single-use, 120-second bootstrap
nonce from the running daemon and hands the browser a URL like

    http://127.0.0.1:8765/ui#/pulse?boot=<nonce>

The page exchanges the nonce (via the fragment, never a query string the
server logs) for the local admin token and then rewrites the fragment away.
Nothing about the token is printed or written to shell history.
"""

from __future__ import annotations

import sys
import webbrowser


def cmd_ui(args) -> int:
    # Imported lazily: the CLI package re-exports this command at the bottom of
    # __init__, so a module-level import of its helpers would be circular.
    from . import _daemon_reachable, _paths, _port, _request

    paths = _paths(args)
    port = _port(args, paths)
    base = "http://127.0.0.1:%d" % port
    if not _daemon_reachable(paths, port):
        print("hippomindd 未在 %s 运行。先运行: hippomind start" % base,
              file=sys.stderr)
        return 1

    minted: dict = {}
    try:
        response = _request(paths, port, "POST", "/api/v1/ui-token", {})
        if isinstance(response, dict):
            minted = response
    except Exception as exc:  # LedgerError on HTTP errors; keep a usable fallback
        print("无法签发一次性 UI 令牌（%s）。" % exc, file=sys.stderr)
        print("将打开控制台；首次使用时请在页面里粘贴本机令牌。", file=sys.stderr)

    path = str(minted.get("url") or "/ui/#/pulse")
    target = base + path if path.startswith("/") else path
    tokenized = bool(minted.get("nonce"))
    if tokenized:
        print("HippoMind Atlas · %s" % base)
        print("一次性登录链接已生成（%s 秒内有效，仅可使用一次）。"
              % minted.get("ttl_seconds", 120))
    else:
        print("HippoMind Atlas · %s" % target)
        print("令牌文件（0600）：%s" % paths.token_path)
        print("在页面里粘贴该文件的内容即可连接；也可以用 hippomind ui 重新签发一次性链接。")
    if getattr(args, "no_open", False):
        print(target)
        return 0
    try:
        opened = webbrowser.open(target)
    except Exception:
        opened = False
    if not opened:
        print("未能自动打开浏览器，请手动访问：%s" % target)
    return 0
