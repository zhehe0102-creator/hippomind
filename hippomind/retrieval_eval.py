"""Dependency-free hooks for optional vector retrieval and ACT-R evaluation.

The daemon does not load a model from this module.  A benchmark or an optional
worker can provide an ``EmbeddingProvider`` and compare its ranking against the
default FTS path without changing the privacy or latency contract.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Protocol, Sequence, Tuple
from datetime import datetime


class EmbeddingProvider(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one local vector per input text; never receives raw chat by default."""


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], *, k: int = 60) -> List[Tuple[str, float]]:
    """Fuse lexical and optional semantic rankings without a model dependency."""
    k = max(1, min(1_000, int(k)))
    scores: Dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for index, memory_id in enumerate(ranking):
            value = str(memory_id or "")
            if not value or value in seen:
                continue
            seen.add(value)
            scores[value] = scores.get(value, 0.0) + 1.0 / (k + index + 1)
    return sorted(scores.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)


@dataclass(frozen=True)
class ACTRActivation:
    """Feature-flagged ACT-R-style activation for offline replay only."""

    decay: float = 0.5
    utility_weight: float = 1.0

    def score(self, access_times: Iterable[float], *, now: float | None = None,
              utility: float = 0.0) -> float:
        current = float(time.time() if now is None else now)
        decay = max(0.05, min(1.5, float(self.decay)))
        total = 0.0
        for accessed in access_times:
            age = max(1.0, current - float(accessed))
            total += age ** (-decay)
        if total <= 0.0:
            return float(self.utility_weight) * float(utility)
        value = math.log(total) + float(self.utility_weight) * float(utility)
        return value if math.isfinite(value) else 0.0

    def rank(self, records: Sequence[Mapping[str, object]], *, now: float | None = None) -> List[Tuple[str, float]]:
        ranked = []
        for record in records:
            raw_times = record.get("access_times", ())
            times = raw_times if isinstance(raw_times, (list, tuple)) else ()
            ranked.append((str(record.get("memory_id", "")), self.score(
                [float(value) for value in times], now=now,
                utility=float(record.get("utility", 0.0) or 0.0))))
        return sorted(ranked, key=lambda pair: (pair[1], pair[0]), reverse=True)


# ---------------------------------------------------------------------------
# 中文记忆 QA 评测套件（事实召回 / 知识更新 / 时间推理 / 弃权）
# ---------------------------------------------------------------------------

def _fact(content: str, marker: str, **extra) -> dict:
    record = {
        "kind": "fact", "scope": "shared", "source_agent": "eval",
        "content": content, "evidence": [f"eval:{marker}"],
    }
    record.update(extra)
    return record


FACT_CASES = [
    {"query": "生产环境的数据库端口", "expect": ["5432"],
     "setup": [_fact("生产环境 PostgreSQL 数据库端口是 5432，部署在 db-01 主机", "f1")]},
    {"query": "Redis 缓存部署在哪台主机", "expect": ["cache-02"],
     "setup": [_fact("Redis 缓存部署在 cache-02 主机，分配内存 32G", "f2")]},
    {"query": "流水线构建超时时间", "expect": ["30"],
     "setup": [_fact("部署流水线使用 GitHub Actions，构建超时 30 分钟", "f3")]},
    {"query": "API 网关限流阈值多少", "expect": ["600"],
     "setup": [_fact("API 网关限流阈值为每分钟 600 次请求", "f4")]},
    {"query": "用户服务登录注册的负责人", "expect": ["张伟"],
     "setup": [_fact("用户服务负责登录注册，负责人是张伟", "f5")]},
    {"query": "对象存储桶 media-prod 在哪个区域", "expect": ["media-prod"],
     "setup": [_fact("对象存储桶名为 media-prod，区域华东二", "f6")]},
    {"query": "Kafka 分区数和副本因子", "expect": ["24"],
     "setup": [_fact("消息队列 Kafka 分区数 24，副本因子 3", "f7")]},
    {"query": "监控告警用什么工具通知", "expect": ["pagerduty"],
     "setup": [_fact("监控告警走 PagerDuty 通知，值班轮换每周一", "f8")]},
    {"query": "数据仓库 ETL 任务运行时间", "expect": ["凌晨2点"],
     "setup": [_fact("数据仓库每晚凌晨2点跑 ETL，任务名 nightly-etl", "f9")]},
    {"query": "前端框架迁移到了什么", "expect": ["vue3"],
     "setup": [_fact("前端框架已迁移到 Vue3，仓库 fe-monorepo", "f10")]},
    {"query": "测试环境数据库实例配额", "expect": ["50g"],
     "setup": [_fact("测试环境数据库是 pg-test 实例，磁盘配额 50G", "f11")]},
    {"query": "CDN 回源地址配置", "expect": ["origin.internal"],
     "setup": [_fact("CDN 回源地址 origin.internal:8443，走内网专线", "f12")]},
    {"query": "Istio sidecar 资源限制", "expect": ["0.5"],
     "setup": [_fact("服务网格用 Istio，sidecar 资源限制 0.5 核 CPU", "f13")]},
    {"query": "日志保留策略是怎样的", "expect": ["365"],
     "setup": [_fact("日志保留策略：应用日志 30 天，审计日志 365 天", "f14")]},
    {"query": "单点登录 OIDC 的 issuer", "expect": ["sso.example.com"],
     "setup": [_fact("单点登录采用 OIDC，issuer 是 sso.example.com", "f15")]},
    {"query": "备份策略全量增量安排", "expect": ["周日"],
     "setup": [_fact("备份策略：全量每周日凌晨，增量每天，保留 28 天", "f16")]},
]

UPDATE_CASES = [
    {"query": "支付接口当前的请求路径", "new_kw": "/v2/pay", "old_kw": "/v1/charge",
     "old": _fact("支付接口请求路径 /v1/charge，走同步扣款", "u1a", confidence=0.55,
                  subject="pay.api", predicate="path", slot_key="current", value={"path": "/v1/charge"}),
     "new": _fact("支付接口路径已切换到 /v2/pay，旧路径下线", "u1b", confidence=0.9,
                  subject="pay.api", predicate="path", slot_key="current", value={"path": "/v2/pay"})},
    {"query": "生产数据库的端口号", "new_kw": "6432", "old_kw": "5432",
     "old": _fact("生产数据库端口 5432", "u2a", confidence=0.55,
                  subject="db.prod", predicate="config", slot_key="port", value={"port": "5432"}),
     "new": _fact("生产数据库已完成端口迁移，现用 6432", "u2b", confidence=0.9,
                  subject="db.prod", predicate="config", slot_key="port", value={"port": "6432"})},
    {"query": "用户服务的负责人", "new_kw": "李娜", "old_kw": "张伟",
     "old": _fact("用户服务负责人张伟", "u3a", confidence=0.55,
                  subject="svc.user", predicate="owner", slot_key="name", value={"owner": "张伟"}),
     "new": _fact("用户服务负责人变更为李娜", "u3b", confidence=0.9,
                  subject="svc.user", predicate="owner", slot_key="name", value={"owner": "李娜"})},
    {"query": "Redis 的内存配置", "new_kw": "64g", "old_kw": "32g",
     "old": _fact("Redis 内存配置 32G", "u4a", confidence=0.55,
                  subject="cache.redis", predicate="config", slot_key="memory", value={"memory": "32G"}),
     "new": _fact("Redis 已扩容到 64G 内存", "u4b", confidence=0.9,
                  subject="cache.redis", predicate="config", slot_key="memory", value={"memory": "64G"})},
    {"query": "Kafka 集群的分区数", "new_kw": "48个分区", "old_kw": "24个分区",
     "old": _fact("Kafka 集群配置 24个分区", "u5a", confidence=0.55,
                  subject="mq.kafka", predicate="config", slot_key="partitions", value={"partitions": 24}),
     "new": _fact("Kafka 扩容后为 48个分区", "u5b", confidence=0.9,
                  subject="mq.kafka", predicate="config", slot_key="partitions", value={"partitions": 48})},
    {"query": "暗色模式开关的状态", "new_kw": "已开启", "old_kw": "已关闭",
     "old": _fact("暗色模式开关状态：已关闭", "u6a", confidence=0.55,
                  subject="ui.darkmode", predicate="state", slot_key="enabled", value={"enabled": False}),
     "new": _fact("暗色模式开关状态：已开启", "u6b", confidence=0.9,
                  subject="ui.darkmode", predicate="state", slot_key="enabled", value={"enabled": True})},
    {"query": "文档站点的域名", "new_kw": "docs.new.com", "old_kw": "docs.old.com",
     "old": _fact("文档站点域名 docs.old.com", "u7a", confidence=0.55,
                  subject="docs.site", predicate="domain", slot_key="host", value={"host": "docs.old.com"}),
     "new": _fact("文档站点迁移到 docs.new.com", "u7b", confidence=0.9,
                  subject="docs.site", predicate="domain", slot_key="host", value={"host": "docs.new.com"})},
    {"query": "华北二 区域的业务分布", "new_kw": "华北二", "old_kw": "华东一",
     "old": _fact("业务部署区域华东一", "u8a", confidence=0.55,
                  subject="biz.deploy", predicate="region", slot_key="name", value={"region": "华东一"}),
     "new": _fact("业务已迁移到华北二区域", "u8b", confidence=0.9,
                  subject="biz.deploy", predicate="region", slot_key="name", value={"region": "华北二"})},
]

TEMPORAL_CASES = [
    {"query": "版本 发布 记录", "from": "2026-08-10T00:00:00+00:00",
     "to": "2026-08-31T23:59:59+00:00", "expect": ["v2.0"], "forbid": ["v1.0"],
     "setup": [
         _fact("8月1日 发布 v1.0 版本", "t1a", timestamp="2026-08-01T10:00:00+00:00"),
         _fact("8月20日 发布 v2.0 版本", "t1b", timestamp="2026-08-20T10:00:00+00:00"),
     ]},
    {"query": "版本 发布 记录", "from": "2026-08-01T00:00:00+00:00",
     "to": "2026-08-10T23:59:59+00:00", "expect": ["v1.0"], "forbid": ["v2.0"],
     "setup": [
         _fact("8月1日 发布 v1.0 版本", "t2a", timestamp="2026-08-01T10:00:00+00:00"),
         _fact("8月20日 发布 v2.0 版本", "t2b", timestamp="2026-08-20T10:00:00+00:00"),
     ]},
    {"query": "线上 事故 复盘", "from": "2026-08-05T00:00:00+00:00",
     "to": "2026-08-06T23:59:59+00:00", "expect": ["数据库连接池"], "forbid": ["缓存雪崩"],
     "setup": [
         _fact("8月2日 复盘缓存雪崩事故", "t3a", timestamp="2026-08-02T09:00:00+00:00"),
         _fact("8月5日 复盘数据库连接池耗尽事故", "t3b", timestamp="2026-08-05T09:00:00+00:00"),
     ]},
    {"query": "架构 评审 结论", "from": "2026-07-01T00:00:00+00:00",
     "to": "2026-07-31T23:59:59+00:00", "expect": ["单体拆分"], "forbid": ["多租户改造"],
     "setup": [
         _fact("7月10日 评审通过单体拆分方案", "t4a", timestamp="2026-07-10T14:00:00+00:00"),
         _fact("8月8日 评审多租户改造提案", "t4b", timestamp="2026-08-08T14:00:00+00:00"),
     ]},
    {"query": "证书 轮换 操作记录", "from": "2026-06-01T00:00:00+00:00",
     "to": "2026-06-30T23:59:59+00:00", "expect": ["网关证书"], "forbid": ["数据库证书"],
     "setup": [
         _fact("6月12日 轮换网关证书", "t5a", timestamp="2026-06-12T03:00:00+00:00"),
         _fact("7月3日 轮载数据库证书", "t5b", timestamp="2026-07-03T03:00:00+00:00"),
     ]},
    {"query": "节点 扩容 变更", "from": "2026-05-01T00:00:00+00:00",
     "to": "2026-05-31T23:59:59+00:00", "expect": ["节点扩容"], "forbid": ["缩容"],
     "setup": [
         _fact("5月9日 完成计算节点扩容", "t6a", timestamp="2026-05-09T08:00:00+00:00"),
         _fact("6月18日 执行测试环境缩容", "t6b", timestamp="2026-06-18T08:00:00+00:00"),
     ]},
    {"query": "安全 演练 报告", "from": "2026-04-01T00:00:00+00:00",
     "to": "2026-04-30T23:59:59+00:00", "expect": ["红蓝对抗"], "forbid": ["钓鱼邮件"],
     "setup": [
         _fact("4月15日 完成红蓝对抗演练", "t7a", timestamp="2026-04-15T13:00:00+00:00"),
         _fact("5月22日 开展钓鱼邮件模拟", "t7b", timestamp="2026-05-22T13:00:00+00:00"),
     ]},
    {"query": "依赖 升级 公告", "from": "2026-03-01T00:00:00+00:00",
     "to": "2026-03-31T23:59:59+00:00", "expect": ["OpenSSL 3.2"], "forbid": ["nginx 1.25"],
     "setup": [
         _fact("3月6日 完成 OpenSSL 3.2 升级", "t8a", timestamp="2026-03-06T11:00:00+00:00"),
         _fact("4月9日 完成 nginx 1.25 升级", "t8b", timestamp="2026-04-09T11:00:00+00:00"),
     ]},
]

ABSTAIN_QUERIES = [
    "量子计算退相干时长怎么测",
    "红楼梦的作者考证结论",
    "马拉松破三配速策略",
    "光合作用光反应效率",
    "爵士乐即兴演奏和弦进行",
    "冰川消融的年度数据",
    "古埃及象形文字的破译",
    "家猫品种的分类学",
]

ABSTAIN_CORPUS = [
    _fact("生产环境 PostgreSQL 数据库端口是 5432，部署在 db-01 主机", "ab1"),
    _fact("部署流水线使用 GitHub Actions，构建超时 30 分钟", "ab2"),
    _fact("消息队列 Kafka 分区数 24，副本因子 3", "ab3"),
    _fact("日志保留策略：应用日志 30 天，审计日志 365 天", "ab4"),
]


ENGLISH_FACT_CASES = [
    {"query": "what port does the production database use", "expect": ["5432"],
     "setup": [_fact("production PostgreSQL database runs on port 5432 at host db-01",
                     "ef1")]},
    {"query": "which host runs the cache service", "expect": ["cache-02"],
     "setup": [_fact("Redis cache deployed on cache-02 with 32G memory", "ef2")]},
    {"query": "what is the CI build timeout", "expect": ["30"],
     "setup": [_fact("deployment pipeline uses GitHub Actions with a 30 minute "
                     "build timeout", "ef3")]},
    {"query": "gateway rate limit configuration", "expect": ["600"],
     "setup": [_fact("API gateway rate limit is 600 requests per minute", "ef4")]},
    {"query": "who owns the user service", "expect": ["alice"],
     "setup": [_fact("user service handles login and signup, owner is alice",
                     "ef5")]},
    {"query": "which bucket stores media files", "expect": ["media-prod"],
     "setup": [_fact("object storage bucket media-prod lives in region east-2",
                     "ef6")]},
    {"query": "how many kafka partitions are configured", "expect": ["24"],
     "setup": [_fact("kafka cluster has 24 partitions with replication factor 3",
                     "ef7")]},
    {"query": "audit log retention period", "expect": ["365"],
     "setup": [_fact("audit logs are retained for 365 days while app logs keep "
                     "30 days", "ef8")]},
]

ENGLISH_UPDATE_CASES = [
    {"query": "current payment endpoint path", "new_kw": "/v2/pay",
     "old_kw": "/v1/charge",
     "old": _fact("payment endpoint is /v1/charge", "eu1a", confidence=0.55,
                  subject="pay.api", predicate="path", slot_key="current",
                  value={"path": "/v1/charge"}),
     "new": _fact("payment endpoint migrated to /v2/pay", "eu1b", confidence=0.9,
                  subject="pay.api", predicate="path", slot_key="current",
                  value={"path": "/v2/pay"})},
    {"query": "production database port number", "new_kw": "6432",
     "old_kw": "5432",
     "old": _fact("database port is 5432", "eu2a", confidence=0.55,
                  subject="db.prod", predicate="config", slot_key="port",
                  value={"port": "5432"}),
     "new": _fact("database port moved to 6432", "eu2b", confidence=0.9,
                  subject="db.prod", predicate="config", slot_key="port",
                  value={"port": "6432"})},
    {"query": "redis memory size", "new_kw": "64g", "old_kw": "32g",
     "old": _fact("redis memory is 32G", "eu3a", confidence=0.55,
                  subject="cache.redis", predicate="config", slot_key="memory",
                  value={"memory": "32G"}),
     "new": _fact("redis upgraded to 64G memory", "eu3b", confidence=0.9,
                  subject="cache.redis", predicate="config", slot_key="memory",
                  value={"memory": "64G"})},
    {"query": "which region hosts production workloads", "new_kw": "us-west-2",
     "old_kw": "us-east-1",
     "old": _fact("production runs in us-east-1", "eu4a", confidence=0.55,
                  subject="biz.deploy", predicate="region", slot_key="name",
                  value={"region": "us-east-1"}),
     "new": _fact("production migrated to us-west-2", "eu4b", confidence=0.9,
                  subject="biz.deploy", predicate="region", slot_key="name",
                  value={"region": "us-west-2"})},
    # 注：不用邮箱做用例——PII 脱敏层会把地址替换为 [REDACTED_EMAIL]（正确行为）。
    {"query": "support escalation slack channel", "new_kw": "#escalation",
     "old_kw": "#help-desk",
     "old": _fact("support escalations go to #help-desk channel", "eu5a",
                  confidence=0.55,
                  subject="org.support", predicate="channel", slot_key="slack",
                  value={"slack": "#help-desk"}),
     "new": _fact("support escalations moved to #escalation channel", "eu5b",
                  confidence=0.9,
                  subject="org.support", predicate="channel", slot_key="slack",
                  value={"slack": "#escalation"})},
    {"query": "api rate limit per minute", "new_kw": "1200",
     "old_kw": "600",
     "old": _fact("api rate limit is 600 requests per minute", "eu6a",
                  confidence=0.55,
                  subject="api.gateway", predicate="limit", slot_key="rpm",
                  value={"rpm": "600"}),
     "new": _fact("api rate limit raised to 1200 requests per minute", "eu6b",
                  confidence=0.9,
                  subject="api.gateway", predicate="limit", slot_key="rpm",
                  value={"rpm": "1200"})},
    {"query": "who is the on-call engineer", "new_kw": "Priya",
     "old_kw": "Marcus",
     "old": _fact("on-call engineer is Marcus this week", "eu7a",
                  confidence=0.55,
                  subject="ops.rotation", predicate="staff", slot_key="oncall",
                  value={"oncall": "Marcus"}),
     "new": _fact("on-call engineer rotated to Priya", "eu7b", confidence=0.9,
                  subject="ops.rotation", predicate="staff", slot_key="oncall",
                  value={"oncall": "Priya"})},
    {"query": "current staging branch name", "new_kw": "release/2.4",
     "old_kw": "release/2.3",
     "old": _fact("staging deploys from release/2.3", "eu8a", confidence=0.55,
                  subject="ci.staging", predicate="branch", slot_key="name",
                  value={"branch": "release/2.3"}),
     "new": _fact("staging switched to release/2.4 branch", "eu8b",
                  confidence=0.9,
                  subject="ci.staging", predicate="branch", slot_key="name",
                  value={"branch": "release/2.4"})},
]

ENGLISH_ABSTAIN_QUERIES = [
    "quantum decoherence measurement protocol",
    "marathon pacing strategy for beginners",
    "ancient egyptian hieroglyph translation methods",
    "jazz improvisation chord progression theory",
    "deep sea bioluminescence research papers",
    "medieval manuscript illumination techniques",
    "stock option vesting schedule negotiation",
    "alpine mountaineering rope team tactics",
]

ENGLISH_ABSTAIN_CORPUS = [
    _fact("production PostgreSQL database port is 5432 on host db-01", "eab1"),
    _fact("deployment pipeline uses GitHub Actions with 30 minute timeout", "eab2"),
    _fact("kafka cluster has 24 partitions with replication factor 3", "eab3"),
    _fact("audit logs retained 365 days, app logs 30 days", "eab4"),
]


ENGLISH_TEMPORAL_CASES = [
    {"query": "what did we deploy yesterday", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["v1.5"], "forbid": ["v1.4"],
     "setup": [
         _fact("deployed payment service v1.4", "et1a",
               timestamp="2026-08-15T10:00:00+00:00"),
         _fact("deployed payment service v1.5", "et1b",
               timestamp="2026-08-20T10:00:00+00:00"),
     ]},
    {"query": "incident postmortem last week", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["connection pool"], "forbid": ["cache stampede"],
     "setup": [
         _fact("postmortem report for cache stampede incident", "et2a",
               timestamp="2026-08-18T09:00:00+00:00"),
         _fact("postmortem for connection pool exhaustion", "et2b",
               timestamp="2026-08-12T09:00:00+00:00"),
     ]},
    {"query": "checkout timeout errors in the last 3 days", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["timeout spike"], "forbid": ["disk full"],
     "setup": [
         _fact("error: timeout spike on checkout endpoint", "et3a",
               timestamp="2026-08-19T12:00:00+00:00"),
         _fact("disk full alarm on db replica", "et3b",
               timestamp="2026-08-10T12:00:00+00:00"),
     ]},
    {"query": "backup completed this month", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["full backup completed"], "forbid": ["july archive"],
     "setup": [
         _fact("full backup completed successfully", "et4a",
               timestamp="2026-08-05T03:00:00+00:00"),
         _fact("july archive verification passed", "et4b",
               timestamp="2026-07-20T03:00:00+00:00"),
     ]},
    {"query": "billing reconciliation last month", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["july reconciliation"], "forbid": ["june ledger"],
     "setup": [
         _fact("july reconciliation closed with zero variance", "et5a",
               timestamp="2026-07-31T22:00:00+00:00"),
         _fact("june ledger archived after audit", "et5b",
               timestamp="2026-06-30T22:00:00+00:00"),
     ]},
    {"query": "standup notes today", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["blocker on flaky tests"], "forbid": ["demo script draft"],
     "setup": [
         _fact("standup: blocker on flaky tests in ci", "et6a",
               timestamp="2026-08-21T09:30:00+00:00"),
         _fact("standup: demo script draft reviewed", "et6b",
               timestamp="2026-08-20T09:30:00+00:00"),
     ]},
    {"query": "sprint retro decisions this week", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["wip limit"], "forbid": ["pair rotation"],
     "setup": [
         _fact("retro decision: adopt wip limit of three", "et7a",
               timestamp="2026-08-19T16:00:00+00:00"),
         _fact("retro decision: change pair rotation weekly", "et7b",
               timestamp="2026-08-14T16:00:00+00:00"),
     ]},
    {"query": "release signoff the day before yesterday", "parse": True,
     "frozen_now": "2026-08-21T15:00:00+00:00",
     "expect": ["v3.1 signoff"], "forbid": ["v3.0 signoff"],
     "setup": [
         _fact("v3.1 signoff recorded by qa lead", "et8a",
               timestamp="2026-08-19T11:00:00+00:00"),
         _fact("v3.0 signoff recorded by release manager", "et8b",
               timestamp="2026-08-18T11:00:00+00:00"),
     ]},
]


def run_memory_qa_suite(ledger_factory) -> dict:
    """运行中文记忆 QA 套件，返回分类指标。"""
    return _run_suite(
        ledger_factory,
        fact_cases=FACT_CASES,
        update_cases=UPDATE_CASES,
        temporal_cases=TEMPORAL_CASES,
        abstain_queries=ABSTAIN_QUERIES,
        abstain_corpus=ABSTAIN_CORPUS,
    )


def run_english_qa_suite(ledger_factory) -> dict:
    """英文 QA 套件：跨语言泛化检查（拉丁分词路径）。"""
    return _run_suite(
        ledger_factory,
        fact_cases=ENGLISH_FACT_CASES,
        update_cases=ENGLISH_UPDATE_CASES,
        temporal_cases=ENGLISH_TEMPORAL_CASES,
        abstain_queries=ENGLISH_ABSTAIN_QUERIES,
        abstain_corpus=ENGLISH_ABSTAIN_CORPUS,
    )


def _run_suite(ledger_factory, *, fact_cases, update_cases, temporal_cases,
               abstain_queries, abstain_corpus) -> dict:
    """通用评测内核：确定性关键词断言，不依赖 LLM。

    ``ledger_factory()`` 必须返回全新 SQLiteLedger（每例隔离，避免跨案例
    词项污染）。
    """
    from .context_pack import ContextPackBuilder, ContextPackOptions

    def _grade_pack(builder, case):
        pack = builder.build(case["query"], ContextPackOptions(max_items=8))
        text = str(pack.get("text", "")).lower()
        hit = any(kw.lower() in text for kw in case["expect"])
        return hit

    def _grade_search(ledger, case, limit=8):
        expect = case.get("expect") or [case.get("new_kw", "")]
        forbid = case.get("forbid") or [case.get("old_kw", "")] if case.get("forbid") or case.get("old_kw") else []
        hits = ledger.search(case["query"], scope="shared", limit=limit)
        joined = " ".join(str(h["content"]) for h in hits).lower()
        ok = any(kw.lower() in joined for kw in expect if kw)
        if not forbid:
            return ok
        return ok and not any(kw.lower() in joined for kw in forbid if kw)

    # ---- 事实召回：pack 路径（含可靠性门槛与去重） -----------------------
    recall_hits = 0
    for case in fact_cases:
        ledger = ledger_factory()
        ledger.ingest_many(case["setup"])
        recall_hits += int(_grade_pack(ContextPackBuilder(ledger), case))
    recall = recall_hits / len(fact_cases)

    # ---- 知识更新：claim 同槽位高置信取代 --------------------------------
    update_hits = 0
    for case in update_cases:
        ledger = ledger_factory()
        old = dict(case["old"])
        new = dict(case["new"])
        for rec in (old, new):
            rec.setdefault("subject", "eval.subject")
            rec.setdefault("predicate", "config")
            rec.setdefault("slot_key", "value")
        ledger.ingest(old)
        ledger.ingest(new)
        update_hits += int(_grade_search(ledger, case))
    update = update_hits / len(update_cases)

    # ---- 时间推理：窗口过滤 ---------------------------------------------
    temporal_hits = 0
    from .time_parse import parse_temporal_expression
    for case in temporal_cases:
        ledger = ledger_factory()
        ledger.ingest_many(case["setup"])
        if case.get("parse"):
            # 查询内嵌时间短语：先解析出窗口再检索（端到端契约）。
            now = datetime.fromisoformat(case["frozen_now"])
            from_time, to_time = parse_temporal_expression(case["query"], now=now)
        else:
            from_time, to_time = case["from"], case["to"]
        hits = ledger.search(case["query"], scope="shared", limit=8,
                             from_time=from_time, to_time=to_time)
        joined = " ".join(str(h["content"]) for h in hits).lower()
        ok = (any(kw.lower() in joined for kw in case["expect"])
              and not any(kw.lower() in joined for kw in case["forbid"]))
        temporal_hits += int(ok)
    temporal = (temporal_hits / len(temporal_cases)
                if temporal_cases else 1.0)

    # ---- 弃权：无关查询必须诚实空包 --------------------------------------
    abstain_ok = 0
    ledger = ledger_factory()
    ledger.ingest_many(abstain_corpus)
    builder = ContextPackBuilder(ledger)
    for query in abstain_queries:
        pack = builder.build(query, ContextPackOptions(max_items=8))
        abstain_ok += int(not pack.get("items"))
    abstention = abstain_ok / len(abstain_queries)

    return {
        "recall_at_8": round(recall, 4),
        "update_accuracy": round(update, 4),
        "temporal_accuracy": round(temporal, 4),
        "abstention_precision": round(abstention, 4),
        "cases": (len(fact_cases) + len(update_cases)
                  + len(temporal_cases) + len(abstain_queries)),
    }
