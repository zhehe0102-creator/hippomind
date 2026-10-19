# Release Checklist

## Repository safety

- Verify `.gitignore` excludes ledgers, WAL files, raw exports, tokens, logs, local JSON snapshots and virtual environments.
- Verify `LICENSE` is present and the public repository metadata points to the same license.
- Verify the detailed user guide includes installation, security, backup, troubleshooting and visual architecture.
- Run `hippomind scan-raw` and `hippomind redact-raw` on a development ledger before any demonstration export.
- Inspect `git status --ignored` and never commit files below `HIPPOMIND_HOME`.
- Create portable examples only with `hippomind export`; it omits raw chat, system/tool events, cursors and credentials.

## Verification

- Run `python3 -m unittest discover -s tests`.
- Run `python3 -m py_compile hippomind/*.py` and `node --check integrations/dsh-plugin/lib/index.js`.
- Run the repository CI workflow on a clean checkout before publishing.
- Run `python3 -m hippomind doctor --deep` and `python3 -m hippomind recovery-check` against a disposable ledger.
- Verify one marked request (`记忆：<query>`) returns only evidence-backed context and an unmarked request returns `not_triggered` without starting a daemon.
- Verify `hippomind inject off --agent <agent>` takes effect on the next request.
- Render the PDF guide and inspect its first page, one GUI page and the release checklist page.

## GitHub publication

Initialize a repository only after the checks pass, commit source/docs/screenshots without local data, tag the release, then create the GitHub repository with the owner-selected name and visibility. The final release description should link to `README.md`, `docs/PORTABILITY.md`, `MEMORY_HUB_GUIDE.md`, and the PDF guide.
