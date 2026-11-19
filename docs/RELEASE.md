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

## Schema compatibility gate (blocking)

These checks are mandatory for every release. The 2026-08 outage happened
because a live ledger reached schema **v8** while the only published source
tree was **v5**; the source refused to open the database, and 1.5M memories had
no code that could read them. Nothing caught it, because every test built
databases with the code under test — a database created by the code under test
can never be too new for it.

- [ ] **Source `SCHEMA_VERSION` >= the live ledger schema.** Compare the two
      numbers directly before tagging:
      - source: `python3 -c "from hippomind.schema import SCHEMA_VERSION; print(SCHEMA_VERSION)"`
      - live ledger: `sqlite3 "$HOME/Library/Application Support/HippoMind/hippomind.sqlite" "SELECT value FROM schema_meta WHERE key='schema_version'"`
      A source version **lower** than the live ledger is a hard stop: that
      release cannot read the data it is meant to serve. Ship the newer source,
      or do not ship.
- [ ] **`python3 -m hippomind version` passes** and reports the expected
      `schema_version`. The command always emits JSON, so it is scriptable
      today: `python3 -m hippomind version | python3 -c "import json,sys; print(json.load(sys.stdin)['schema_version'])"`.
      `python3 -m hippomind version --json` is supported (the flag is an
      explicit machine-readable declaration; output is JSON either way), and
      `tests/test_schema_contract.py` pins both the plain and `--json` forms.
      Feed it to release automation directly:
      `python3 -m hippomind version --json | python3 -c "import json,sys; print(json.load(sys.stdin)['schema_version'])"`.
- [ ] **`hippomind checkup` passes** (`"checkup": "ok"`, exit 0) against a
      disposable ledger, and against a copy of the live ledger when one exists.
- [ ] **Run the schema-migration drill before tagging:**
      `python3 tests/drill_schema_migration.py`
      It must print `PASS` and exit 0. On a machine with a real ledger, also
      rehearse the upgrade on a copy: `python3 tests/drill_schema_migration.py --live`
      (the drill only ever reads the source ledger; it never migrates it in place).
- [ ] **Add a frozen fixture for the new schema.** If `SCHEMA_VERSION` moved,
      add `tests/fixtures/schema/v<N>.sql` capturing what the *old* release
      wrote, so the next release has a real historical database to migrate. A
      fixture newer than the source is a drill failure by design.
- [ ] **Back up before upgrading a real installation:**
      `scripts/backup.sh` (consistent SQLite snapshot + `.sha256`), and verify
      it with `scripts/restore.sh --db <copy> <snapshot>` on a throwaway path.
- [ ] **Run the backup/restore drill (synthetic, no real data):**
      `bash scripts/backup_restore_drill.sh --json` exits 0.
      It creates a disposable ledger, snapshots it with `scripts/backup.sh`,
      mutates the live copy, restores, runs `hippomind checkup`, and
      verifies a tampered snapshot is refused without touching the live ledger.
      `scripts/release_check.sh` runs it as step 9 and CI's
      release-gate job runs it too.

## Static analysis gate (blocking)

- [ ] `bash scripts/lint.sh` exits 0. The gate rule set must stay clean; the
      legacy baseline in `scripts/lint-baseline.txt` may only shrink. If a new
      finding is genuinely intentional, run `scripts/lint.sh --update-baseline`
      and explain the new entries in the commit message.
