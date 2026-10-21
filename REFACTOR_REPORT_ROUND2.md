# HippoMind 重构二次开发报告（第二轮）

> 2026-10-20/21 · Lead + 2 个专项子智能体 · schema v8（无迁移）
> 工作区：`/Volumes/移动硬盘/记忆系统重构`（git 版本化）
> 本轮基线：`REFACTOR_REPORT.md`（首轮重构）+ `docs/BACKLOG-hippomind.md`

## 一、本轮性质

首轮重构（`REFACTOR_REPORT.md`）把 4.48GB / 153 万行的失控账本收敛到 407MB / 13.6 万行，
并交付了 claims/relations/feedback/procedural 的激活路径、架构拆分与 CI 门禁。
本轮**不重做已验证的路线**，而是从首轮报告自己写下的「遗留风险」和「未兑现承诺」出发，
做**实测驱动的二次开发**：先复现、再修、每项都带负向控制（revert 后测试必须变红）。

首轮报告 `五、遗留风险` 中列出的 6 条，本轮处理了 3 条，并新挖出 4 条首轮未记录的缺陷。

## 二、修复清单（全部实测，非推测）

| # | 缺陷 | 实测证据 | 影响 |
|---|---|---|---|
| 1 | **FTS 索引集合有四个互不相同的定义**：写路径只认 `accepted`；`fts_sync_report` 认 `accepted+active`；`health_overview`/`_rebuild_fts` 认「除 raw 且非 active 外全部」；`maintain` 只剪 `lifecycle!='active'` | 线上库 5,571 条索引行中只有 392 条可被召回——**5,179 条（93%）是永久垃圾**；`checkup --repair` 反而把它加回来（实测干净库 repair 后 `stale` 0→1） | 索引膨胀、体检说谎、修复命令加重病情 |
| 2 | **`resolve_claim` 翻案重裁只恢复 `quality_state` 不恢复 `lifecycle`** | 复现：A/B 冲突 → 裁 B 胜（A `superseded`）→ 重裁 A 胜：A 变成 `quality_state='accepted'` 但 `lifecycle='superseded'`；所有召回都要求 `lifecycle='active'`，**胜者永久不可召回**，`search` 返回 `[]` | 首轮宣称的冲突裁决工作流实际不可用 |
| 3 | **归档丢失 `metadata_json`**（首轮报告已列为遗留风险 3） | 实测 purge 归档记录 14 个键，无 `metadata`；带 `archive_rowid`/`agent_chat_path` 的行删掉后**溯源指针永久丢失**（首轮靠 4.27GB 备份兜底，而备份会被轮转） | 归档只剩内容，没有「这行从哪来」 |
| 4 | **`collapse_duplicate_rows` 崩溃窗口会误删幸存行** | 预置一张陈旧的 `_dedupe_groups`（`CREATE TABLE IF NOT EXISTS` 不会重建），下一次调用实测 `duplicate_groups=2, collapsed=4, raw 行剩 0`——本该留 1 行 | 进程被杀后下一次维护**毁数据** |
| 5 | **`fts_sync_report` 遇到重复索引行直接抛异常** | `memory_id` 是 `UNINDEXED`，重复行可表示；实测 `IntegrityError: UNIQUE constraint failed`，把可诊断的索引缺陷变成裸 traceback（`checkup` 只报 `status:error`） | 体检在最需要它的时候崩 |
| 6 | **`redact_raw_evidence` 会把隔离行重新写回索引** | 只判 `lifecycle=='active'`，忽略 `quality_state`；实测脱敏一条 quarantine+active 行后 `stale` 0→1、`drift=True` | 安全脱敏操作**反向制造**索引垃圾 |
| 7 | **`export_sql` 无条件重建 FTS** | `INSERT INTO memory_fts ... SELECT ... FROM memories` 无 WHERE，回放后冷/隔离行也进目标库索引 | 迁移目标库开局即漂移 |
| 8 | **`doctor` 是永远 exit 0 的健康门** | 实测打印 `[FAIL] daemon 不可达` + `NEEDS ATTENTION` 后仍 `exit 0`；`RELEASE.md` 却把它当阻断门用 | 与首轮修的 `stop` 谎报成功同类：**门禁不能失败就不是门禁** |
| 9 | **4 个 CLI 测试会静默连上环境里任意 daemon** | `--port` 缺省时 `_port()` 回落到全局 8765；实测本机有真实 daemon 时这 4 项以 `401 local authorization token required` 失败 | 首轮 `ca1f430` 同类缺陷的残余；测试结果取决于机器状态 |
| 10 | **登录自启项路径漂移 + 无法检测**（首轮遗留风险 5） | `com.hippomind.gui.plist` 的 `PYTHONPATH`/`WorkingDirectory` 指向已不存在的 `pi-desktop/chat-workspace/hippocampus-agent`；实测该 job 连启动都失败 | 首轮只「记录」未检测，无人知晓 |
| 11 | **`.sha256` 备份校验文件未被 gitignore 覆盖** | `git status` 显示 `backups/*.sha256` 未跟踪，而 `RELEASE.md` 明令禁止提交备份目录 | 一次 `git add -A` 就会把账本校验文件提交进仓库 |

## 三、修复内容与验收

### 3.1 FTS 索引不变量统一（缺陷 1/5/6/7）

**单一真相源**，`hippomind/schema.py`：

```python
FTS_INDEXED_PREDICATE = "quality_state='accepted' AND lifecycle='active'"
def fts_indexed_predicate(table: str = "") -> str:  # 可选表前缀
```

6 个写/审点全部改为从它派生，包内**零私有副本**（`test_only_schema_module_owns_the_predicate_literal`
扫描 `hippomind/**/*.py` 机械强制）：

1. `schema._rebuild_fts`（带 schema 年龄守卫：`_migrate_v3` 早于 v4 的 `quality_state` 列，
   此时退化为 `lifecycle='active'`；v3 幸存行全部早于质量维度、v4 默认 `accepted`，两集合重合）
2. `health.fts_sync_report` 的 `expected` 与 `stale`
3. `health.health_overview.fts.expected`
4. `lifecycle.maintain()` 的剪枝
5. `lifecycle.repair_fts`（经 `_rebuild_fts`）
6. `archive._fts_indexed`（脱敏重索引）与 `clients.export_sql`（导出重建）

**额外修正**：`maintain` 现在也折叠重复 `memory_id` 索引行（保留最小 rowid，计入 `fts_pruned`），
因为 `fts_sync_report` 的 `duplicate_rows` 提示指向 `maintain`——**提示不能指向不收敛的命令**。

**线上库实测收敛**（先 `scripts/backup.sh`，sha256 `4b1f6408…` 校验通过）：

| 指标 | 收敛前 | 收敛后 |
|---|---:|---:|
| FTS 行数 | 4,036 | **185** |
| 可召回集合 | 185 | 185 |
| `stale` | 3,851 | **0** |
| `drift` | True | **False** |
| `maintain` 耗时 | — | **0.13s** |

`PRAGMA quick_check = ok`；`search("部署"/"WAL 模式"/"记忆系统")` 均正常返回。

### 3.2 冲突裁决翻案（缺陷 2）

`resolve_claim` 恢复胜者时同时恢复 `lifecycle: superseded → active` 并清空 `superseded_by`；
索引成员判定改用统一谓词（只有 `accepted+active` 才补回 FTS）。败者语义不变
（`superseded` + 移出 FTS）。

边界有意收窄：只从 `superseded` 恢复——`expired` 是时间窗口语义（`valid_until` 已过），
裁决不改窗口，强改会造出「`lifecycle=active` 但窗口已过期」的不一致状态。
`quality_state` 恢复 `accepted` 保持既有契约（裁决本身即一次显式人工确认）。

`tests/test_resolve_recrown.py`（4 项）锁定：完整生命周期（A/B 冲突 → 裁 B → 重裁 A →
A 可召回、`superseded_by` 清空、`stale=0`、`drift=False`）、败者语义不变、
冷置行重裁后重回热索引、以及「裁决提升质量」这一契约本身（防止未来只修 lifecycle
却把胜者留在 quarantine，复现同类「胜者不可召回」缺陷）。

### 3.3 归档可恢复性（缺陷 3/4）

- 两份归档 payload 均新增 `metadata` 键（purge 14→15 键、collapse 9→10 键），**纯增量**，
  旧键全部保留（测试逐个锁定）；解码失败降级为 `{}`，不阻塞保留期收敛。
- `_dedupe_groups` 改为无条件 `DROP TABLE IF EXISTS` 后重建，崩溃后留下的是「没有表」
  （安全态）而不是「看似合理的陈旧快照」。
- fail-closed 顺序（写归档 → chmod/fsync → 回读校验 → 才删除）**未改动**，并新增两条锁定测试
  （fsync 失败时零删除且不留半截归档；归档目录不可用时直接拒绝）。

### 3.4 健康门与登录项（缺陷 8/10）

- `doctor` 有任何 `severity='error'` 时 **exit 1**；warning 保持非致命（既有 5 项测试依赖）。
- 新增 `launchd` 检查：读 plist、stat 其声明的路径，**只读、不创建/不加载/不调用子进程**
  （测试用 `subprocess.run` 断言禁止调用）。同时检查 `com.hippomind.gui.plist`——
  它是手写的，`login-install` 不管它，正是最会永久留着死路径的那个。

### 3.5 测试隔离（缺陷 9）

`tests/test_offline_fallback.py`、`tests/test_export_since.py` 补 `--runtime-dir` + 动态空闲端口，
使「没有 daemon 应答」成为**构造事实**而非机器状态。
负向控制：本机有真实 daemon 占 8765 时，还原端口固定后 3 项立刻红（`401 local authorization token required`），
恢复后全绿——与首轮 `ca1f430` 同一处理方式。

## 四、本轮新挖出的一个真实运维结论（重要）

首轮遗留风险 5 只说了「launchd 指向旧路径，启用前需重装」。本轮实测发现**更根本的限制**：

> **工作区在外置卷时，launchd 后台任务无法读取该卷。**

复现方式（最小探针，与本项目无关）：同一条 `python3 -c "import <模块>"`，
终端里成功；由 launchd 启动则在 `importlib._bootstrap_external._path_importer_cache` 抛
`PermissionError: [Errno 1] Operation not permitted`。内置盘路径的同类探针正常。

因此 `hippomind login-install` 写出的 plist 即使路径全对，开机自启仍会在每次登录失败——
这是 macOS 隐私保护（TCC）对可移动卷的限制，**改 plist 修不好**。
`doctor` 现在把这种情况单独报为 warning 并给出真实补救（用 `hippomind start`，或把工作区移到内置盘），
而不是误报成「路径过期」让运维白折腾。

## 五、验证状态（全部本轮实测）

| 门 | 结果 |
|---|---|
| 全量单测 | **596 全绿**（基线 554 → +42；其中 3 个测试文件为本轮新增） |
| 新增测试 | `test_fts_invariant.py`（16）、`test_archive_provenance.py`（14）、`test_doctor_gate.py`（12） |
| lint 棘轮 | gate **90/90**、legacy **254/254**，exit 0（零新增告警） |
| schema 迁移演练 | **PASS**，21 项检查全过（含「拒绝未来库必须字节不变」） |
| 基准冒烟 | HTTP 并发 / 并发写入 / 检索剖析 **全部 OK** |
| 真实库 | `checkup: ok`、`doctor: READY`（exit 0）、`quick_check: ok`、FTS 185 行零漂移 |
| 负向控制 | 11 项修复各有 revert 后必红的测试（详见各节） |

## 六、遗留与未做（如实）

1. **`identity.py:314` 写路径刻意未改**：新写入行恒为 `lifecycle='active'`，`accepted` 即
   `accepted AND active`，加 lifecycle 条件是死代码；已用「噪声 raw 行落为 quarantine 且不入索引」
   的反例验证两个分支都一致。
2. **`search.py:299` 未改**：它是**召回**过滤（accepted，再按 flag 收窄 active/cold/expired），
   不是索引成员定义，语义正确。
3. **外置卷开机自启**：属平台限制，未尝试绕过（不引入 TCC 授权破解）。若要开机自启，
   需把工作区迁到内置盘；本轮只在 `doctor` 里把它变成**可见**的已知限制。
4. **32,615 条 `tool_output` 投影**仍留在历史账本（`doctor` 的 privacy WARN 所指）。
   这是首轮「未动」决策（raw 冷数据保留审计通道）的延续，需要时跑 `scan-raw`/`redact-raw`。
5. **本轮未触碰**：Context Pack 8 项/6000 token 预算契约、loopback-only daemon 与 token 权限模型、
   `SCHEMA_VERSION`（仍为 8，本轮全部为代码级修复，无迁移）。

## 七、提交与回滚

- 本轮所有改动在 `main` 上逐项提交，每条信息说明「缺陷 → 修复 → 证据」。
- 线上库改动前有校验备份：`backups/hippomind-20261021T045936Z.sqlite`
  （sha256 `4b1f64085f2d0f5a5fdb0d314b69ed9ef95610361de6c14307f729df2cb4eb7f`，426,508,288 B）。
- `backups/` 与 `*.sha256` 已加入 `.gitignore`（此前 `.sha256` 会被误提交）。
- 恢复：`scripts/restore.sh --db <copy> backups/hippomind-20261021T045936Z.sqlite`。
