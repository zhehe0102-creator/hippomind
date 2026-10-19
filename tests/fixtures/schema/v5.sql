-- Frozen HippoMind ledger schema v5 (the version the 2026-10 archive shipped).
--
-- WHY THIS FILE IS HAND-WRITTEN AND FROZEN
-- ----------------------------------------
-- The outage this repository is recovering from: the live ledger reached v8
-- while the only published source tree was v5, so opening it raised
-- "账本 schema 比当前 HippoMind 更新，拒绝降级写入" and 1.5M memories had no code
-- that could read them.  A drill that builds its "old database" by calling the
-- *current* migration functions proves nothing: it can never notice that a
-- migration was deleted, reordered, or that a released version can no longer be
-- opened.  So this fixture is a literal snapshot of what v5 wrote, with no code
-- path back to hippomind.schema.  Do not "fix" it to match current source; add
-- a new vN.sql snapshot instead.
--
-- State captured here (what real v5 databases look like):
--   * tables created by v1 plus the v2/v4 additions (claims, relations, trust,
--     credentials, bi-temporal columns, quality_state);
--   * FTS still on the unicode61 tokenizer (v6 switched it to trigram);
--   * NO `pinned` column (added by v7) and NO v8 retrieval indexes, so the
--     upgrade path has real work to do;
--   * rows whose valid_from/asserted_at were already backfilled by v4;
--   * a legacy raw-chat derivative already quarantined by v5;
--   * raw rows in both 'active' and 'cold' lifecycle, to pin the invariant
--     that only raw rows that are NOT active stay out of the FTS index.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    asserted_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    origin_event_id TEXT NOT NULL UNIQUE REFERENCES ledger_events(event_id),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_access_at TEXT NOT NULL DEFAULT '',
    last_weighted_at TEXT NOT NULL,
    valid_until TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    archived_at TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'active',
    raw_chat INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 0.5,
    importance INTEGER NOT NULL DEFAULT 5,
    touch_count INTEGER NOT NULL DEFAULT 0,
    utility REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.5,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL DEFAULT '',
    asserted_at TEXT NOT NULL DEFAULT '',
    agent_trust REAL NOT NULL DEFAULT 0.5,
    quality_score REAL NOT NULL DEFAULT 1.0,
    quality_state TEXT NOT NULL DEFAULT 'accepted'
);

CREATE TABLE IF NOT EXISTS memory_claims (
    claim_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL DEFAULT '',
    asserted_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    source_trust REAL NOT NULL DEFAULT 0.5,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    UNIQUE(memory_id,subject,predicate,slot_key)
);

CREATE TABLE IF NOT EXISTS memory_relations (
    relation_id TEXT PRIMARY KEY,
    parent_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    child_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    relation_type TEXT NOT NULL DEFAULT 'caused_by',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    UNIQUE(parent_memory_id,child_memory_id,relation_type),
    CHECK(parent_memory_id != child_memory_id)
);

CREATE TABLE IF NOT EXISTS memory_feedback (
    feedback_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    signal REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    source_agent TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_id TEXT PRIMARY KEY,
    cursor TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    last_success_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_trust (
    agent_id TEXT PRIMARY KEY,
    baseline REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local-admin'
);

CREATE TABLE IF NOT EXISTS agent_credentials (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    permissions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_source ON ledger_events(source_agent, session_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON ledger_events(task_id, project);
CREATE INDEX IF NOT EXISTS idx_events_time ON ledger_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, lifecycle);
CREATE INDEX IF NOT EXISTS idx_memories_task ON memories(task_id, project, updated_at);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_agent, session_id);
CREATE INDEX IF NOT EXISTS idx_memories_state ON memories(raw_chat, lifecycle, weight);
CREATE INDEX IF NOT EXISTS idx_feedback_memory ON memory_feedback(memory_id, created_at);
CREATE INDEX IF NOT EXISTS idx_claim_slot ON memory_claims(scope,subject,predicate,slot_key,state,valid_from);
CREATE INDEX IF NOT EXISTS idx_claim_memory ON memory_claims(memory_id,state);
CREATE INDEX IF NOT EXISTS idx_relations_parent ON memory_relations(parent_memory_id);
CREATE INDEX IF NOT EXISTS idx_relations_child ON memory_relations(child_memory_id);
CREATE INDEX IF NOT EXISTS idx_agent_credentials_agent ON agent_credentials(agent_id, revoked_at);

-- v5 tokenizer: unicode61.  v6 drops and recreates this table with trigram.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED, content, task_id, project, source_agent, kind,
    tokenize='unicode61'
);

INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','5');

-- Structured rows written before the outage.
INSERT INTO ledger_events
    (event_id,event_type,occurred_at,ingested_at,source_agent,session_id,task_id,
     project,role,scope,evidence_json,content,content_hash,metadata_json,asserted_at)
VALUES
    ('ev-v5-fact','observation','2026-08-01T00:00:00Z','2026-08-01T00:00:01Z','archive',
     'sess-v5','task-v5','proj-v5','user','shared','["archive:v5:1"]',
     '迁移演练记忆：生产库端口是 5432','hash-v5-fact','{}','2026-08-01T00:00:01Z'),
    ('ev-v5-decision','decision','2026-08-01T00:00:02Z','2026-08-01T00:00:03Z','archive',
     'sess-v5','task-v5','proj-v5','assistant','shared','["archive:v5:2"]',
     '迁移演练记忆：决定使用 WAL 模式','hash-v5-decision','{}','2026-08-01T00:00:03Z'),
    ('ev-v5-raw-cold','raw_chat','2026-08-01T00:00:04Z','2026-08-01T00:00:05Z','archive',
     'sess-v5','task-v5','proj-v5','user','agent','["archive:v5:3"]',
     '迁移演练记忆：这条冷 raw 不应进入检索索引','hash-v5-raw-cold','{}','2026-08-01T00:00:05Z'),
    ('ev-v5-raw-active','raw_chat','2026-08-01T00:00:06Z','2026-08-01T00:00:07Z','archive',
     'sess-v5','task-v5','proj-v5','user','agent','["archive:v5:4"]',
     '迁移演练记忆：这条活跃 raw 仍应可检索','hash-v5-raw-active','{}','2026-08-01T00:00:07Z'),
    ('ev-v5-legacy-derived','observation','2026-08-01T00:00:08Z','2026-08-01T00:00:09Z','archive',
     'sess-v5','task-v5','proj-v5','assistant','agent','["archive:v5:5"]',
     '迁移演练记忆：旧 raw 派生的启发式摘要','hash-v5-legacy-derived',
     '{"derived_from_raw_chat":true}','2026-08-01T00:00:09Z');

INSERT INTO memories
    (memory_id,origin_event_id,kind,content,source_agent,session_id,task_id,project,
     role,scope,evidence_json,tags_json,metadata_json,created_at,updated_at,
     last_access_at,last_weighted_at,lifecycle,raw_chat,weight,importance,
     valid_from,asserted_at,quality_state,quality_score)
VALUES
    ('m-v5-fact','ev-v5-fact','fact','迁移演练记忆：生产库端口是 5432','archive',
     'sess-v5','task-v5','proj-v5','user','shared','["archive:v5:1"]','["db"]','{}',
     '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','',
     '2026-08-01T00:00:00Z','active',0,0.5,7,
     '2026-08-01T00:00:00Z','2026-08-01T00:00:01Z','accepted',1.0),
    ('m-v5-decision','ev-v5-decision','decision','迁移演练记忆：决定使用 WAL 模式','archive',
     'sess-v5','task-v5','proj-v5','assistant','shared','["archive:v5:2"]','[]','{}',
     '2026-08-01T00:00:02Z','2026-08-01T00:00:02Z','',
     '2026-08-01T00:00:02Z','active',0,0.6,6,
     '2026-08-01T00:00:02Z','2026-08-01T00:00:03Z','accepted',1.0),
    ('m-v5-raw-cold','ev-v5-raw-cold','raw_chat','迁移演练记忆：这条冷 raw 不应进入检索索引','archive',
     'sess-v5','task-v5','proj-v5','user','agent','["archive:v5:3"]','[]','{}',
     '2026-08-01T00:00:04Z','2026-08-01T00:00:04Z','',
     '2026-08-01T00:00:04Z','cold',1,0.2,3,
     '2026-08-01T00:00:04Z','2026-08-01T00:00:05Z','accepted',1.0),
    ('m-v5-raw-active','ev-v5-raw-active','raw_chat','迁移演练记忆：这条活跃 raw 仍应可检索','archive',
     'sess-v5','task-v5','proj-v5','user','agent','["archive:v5:4"]','[]','{}',
     '2026-08-01T00:00:06Z','2026-08-01T00:00:06Z','',
     '2026-08-01T00:00:06Z','active',1,0.3,4,
     '2026-08-01T00:00:06Z','2026-08-01T00:00:07Z','accepted',1.0),
    ('m-v5-legacy-derived','ev-v5-legacy-derived','semantic','迁移演练记忆：旧 raw 派生的启发式摘要','archive',
     'sess-v5','task-v5','proj-v5','assistant','agent','["archive:v5:5"]','[]',
     '{"derived_from_raw_chat":true}',
     '2026-08-01T00:00:08Z','2026-08-01T00:00:08Z','',
     '2026-08-01T00:00:08Z','active',0,0.4,3,
     '2026-08-01T00:00:08Z','2026-08-01T00:00:09Z','quarantine',0.15);

-- v5 FTS contents: raw+cold excluded, everything else indexed.
INSERT INTO memory_fts(memory_id,content,task_id,project,source_agent,kind) VALUES
    ('m-v5-fact','迁移演练记忆：生产库端口是 5432 db','task-v5','proj-v5','archive','fact'),
    ('m-v5-decision','迁移演练记忆：决定使用 WAL 模式','task-v5','proj-v5','archive','decision'),
    ('m-v5-raw-active','迁移演练记忆：这条活跃 raw 仍应可检索','task-v5','proj-v5','archive','raw_chat'),
    ('m-v5-legacy-derived','迁移演练记忆：旧 raw 派生的启发式摘要','task-v5','proj-v5','archive','semantic');

INSERT INTO memory_claims
    (claim_id,memory_id,scope,subject,predicate,slot_key,value_json,value_hash,
     valid_from,valid_until,asserted_at,confidence,source_trust,state,created_at)
VALUES
    ('claim-v5-port','m-v5-fact','shared','生产数据库','port','db:prod:port','5432',
     'vh-claim-v5-port','2026-08-01T00:00:00Z','','2026-08-01T00:00:01Z',0.8,0.7,'active',
     '2026-08-01T00:00:01Z');

INSERT INTO memory_relations
    (relation_id,parent_memory_id,child_memory_id,relation_type,confidence,created_at)
VALUES
    ('rel-v5-1','m-v5-decision','m-v5-fact','informed_by',0.6,'2026-08-01T00:00:03Z');

INSERT INTO source_cursors
    (source_id,cursor,updated_at,last_success_at,last_error,metadata_json)
VALUES
    ('dsh:sess-v5','offset:42','2026-08-01T00:00:09Z','2026-08-01T00:00:09Z','','{}');

INSERT INTO agent_trust(agent_id,baseline,updated_at,source)
VALUES ('archive',0.6,'2026-08-01T00:00:00Z','local-admin');
