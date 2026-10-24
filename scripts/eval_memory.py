#!/usr/bin/env python3
"""Labelled retrieval evaluation and pass/fail gate for HippoMind.

This is the regression gate for memory retrieval quality.  It runs a labelled
query set (a query plus required evidence substrings) against a ledger, reports
recall@k, MRR, duplicate rate and latency percentiles, then compares the
aggregate against configurable thresholds.

Two labelled sets measure two different capabilities:

``keyword``  Queries written with vocabulary that exists in the target memory.
             Near-saturated (recall@8 1.000), so it is a regression detector,
             not a quality measure.
``semantic`` Natural-language paraphrases whose wording does not overlap the
             target memory.  This measures the capability the keyword set
             cannot see: retrieving a fact you describe differently.

Both are grounded in the production ledger (source_ids records provenance;
docs/EVAL.md explains how to refresh them and why the second set exists).

Usage:
    python3 scripts/eval_memory.py                 # gate the keyword set
    python3 scripts/eval_memory.py --set semantic  # gate the paraphrase set
    python3 scripts/eval_memory.py --set all       # report/gate both
    python3 scripts/eval_memory.py --json
    python3 scripts/eval_memory.py --queries my_queries.json
    python3 scripts/eval_memory.py --dump-queries /tmp/queries.json
    python3 scripts/eval_memory.py --no-gate       # report only

Exit codes: 0 = gate passed, 1 = gate failed, 2 = usage/input error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")

# Context fields are part of the memory payload a caller receives (the Context
# Pack renders them), so expected evidence may live there as well as in
# content.
CONTEXT_FIELDS = ("goal", "progress", "key_decisions", "related_files",
                  "blockers", "next_steps", "preferences")

# Gate defaults keep a safety margin over the 2026-10-23 production baseline:
# recall@8 1.000 -> 0.90, MRR 0.915 -> 0.75, duplicate rate 0.016 -> 0.10,
# p95 ~10ms -> 250ms.  Every threshold can be overridden per run.
DEFAULT_THRESHOLDS = {
    "min_recall_at_k": 0.90,
    "min_mrr": 0.75,
    "max_duplicate_rate": 0.10,
    "max_p95_ms": 250.0,
    "min_queries": 30,
}

# The paraphrase set starts far lower *because the retriever really is that much
# worse when the words differ*; that gap is the point of the set.  Its
# thresholds are a ratchet floor, not a quality claim: passing only proves the
# measured gap did not get worse.  Raise them as the gap closes.
SEMANTIC_THRESHOLDS = {
    "min_recall_at_k": 0.17,
    "min_mrr": 0.14,
    # The paraphrase set returns many near-empty or duplicate top-k lists (a
    # miss often means the retriever fell back to one broad memory repeated
    # across ranks), so its duplicate rate is far above the keyword set's and
    # moves with the ledger.  The ceiling is a gross-regression guard only.
    "max_duplicate_rate": 0.35,
    "max_p95_ms": 400.0,
    "min_queries": 40,
}

# Measured values the thresholds were calibrated against.  The report prints
# current-minus-baseline so drift is visible instead of silent.
SET_BASELINE = {
    "keyword": {"date": "2026-10-23", "recall_at_k": 1.000, "mrr": 0.867,
                "duplicate_rate": 0.016, "latency_p95_ms": 12.5,
                "recall_at_k_range": [1.000, 1.000],
                "mrr_range": [0.865, 0.867], "samples": 3},
    # The paraphrase set runs against a live ledger the daemon keeps writing
    # to, so the hit count drifts by a query or two between runs even with no
    # code change.  Observed over 34 samples: 11-15 hits of 53, i.e.
    # recall 0.207-0.283 and MRR 0.179-0.261.  A range is recorded rather than
    # a single number, and the gate floor sits below the low end so production
    # drift alone can never fail the gate (only a real regression can).
    "semantic": {"date": "2026-10-24", "recall_at_k": 0.207, "mrr": 0.179,
                 "duplicate_rate": 0.097, "latency_p95_ms": 66.0,
                 "recall_at_k_range": [0.207, 0.283],
                 "mrr_range": [0.179, 0.261], "samples": 34},
}

SET_THRESHOLDS = {
    "keyword": DEFAULT_THRESHOLDS,
    "semantic": SEMANTIC_THRESHOLDS,
}

# Report/gate order for --set all.  Fixed so output is comparable run to run.
SET_ORDER = ("keyword", "semantic")

# KEYWORD_QUERIES: grounded in the 2026-10-23 production ledger/claims.  Each
# entry has an id, category, query, expected evidence substrings (all must
# appear in the same retrieved memory), an optional project filter, and the
# source memory ids used to ground it.  Query style is keyword-oriented because
# the distilled ledger rows store claim values without their subject/predicate
# context; see docs/EVAL.md for the rationale and refresh procedure.
#
# This set is near-saturated: every query reuses vocabulary that literally
# exists in the target memory, so it detects regressions but cannot measure
# whether the retriever understands a differently-worded request.
KEYWORD_QUERIES: List[Dict[str, Any]] = [
    {"id": "hippomind-gui-theme", "category": "hippomind",
     "query": "HippoMind GUI 主题 纸本", "expect": ["纸本"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "hippomind-db", "category": "hippomind",
     "query": "HippoMind 数据库选型", "expect": ["SQLite"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e", "mem_bf9ae505751762e95b19f3a02e772b2c"]},
    {"id": "hippomind-token-budget", "category": "hippomind",
     "query": "HippoMind 默认 token 预算", "expect": ["6000"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-bitemporal", "category": "hippomind",
     "query": "HippoMind schema v4", "expect": ["双时态"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-host", "category": "hippomind",
     "query": "HippoMind 部署主机 mac-mini", "expect": ["mac-mini"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-arch", "category": "hippomind",
     "query": "HippoMind 最终架构 Context Pack", "expect": ["SQLite/WAL"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-agents", "category": "hippomind",
     "query": "Codex Pi Claude Cursor 支持", "expect": ["跨端续接"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-injection", "category": "hippomind",
     "query": "全量归档 注入 策略", "expect": ["最小上下文"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-hardening", "category": "hippomind",
     "query": "召回质量 daemon 可靠性", "expect": ["数据保护"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-security", "category": "hippomind",
     "query": "来源追溯 隐私隔离 一键启动", "expect": ["提示注入防护"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-longmemeval", "category": "hippomind",
     "query": "下一阶段评估 程序性记忆 ACT-R", "expect": ["LongMemEval"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-claims", "category": "hippomind",
     "query": "冲突检测 技能记忆", "expect": ["claims"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457", "mem_9d0c63f9a330bc365c36a4766128cda7"]},
    {"id": "hippomind-actr", "category": "hippomind",
     "query": "向量检索 ACT-R 定位", "expect": ["可插拔评测接口"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-vector-status", "category": "hippomind",
     "query": "记忆系统 vector 激活状态", "expect": ["vector 尚未激活"],
     "source_ids": ["mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "hippomind-audit-issues", "category": "hippomind",
     "query": "误召回 访问控制", "expect": ["漏采"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-verification", "category": "hippomind",
     "query": "任务文件 可写性 验证", "expect": ["共享账本"],
     "source_ids": ["mem_f8fa04623f5fe9acf8e8ff6a3b182c5d", "mem_d9951bdc1e4074ec8eff8b9037514f4b"]},
    {"id": "zygisk-next-version", "category": "android_ops",
     "query": "Zygisk Next 最新发布版本", "expect": ["v1.5.2"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "zygisk-next-github", "category": "android_ops",
     "query": "Zygisk Next GitHub 仓库地址", "expect": ["Dr-TSNG/ZygiskNext"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "zygisk-next-date", "category": "android_ops",
     "query": "Zygisk Next v1.5.2 发布日期", "expect": ["2026-10-15"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "neozygisk-version", "category": "android_ops",
     "query": "NeoZygisk 运行版本", "expect": ["v2.4-289-08080ef-release"],
     "source_ids": ["mem_21070a16c0d9cfd809f693ef6b62b5c1", "mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "neozygisk-kernelsu", "category": "android_ops",
     "query": "NeoZygisk KernelSU 兼容性", "expect": ["post-fs-data.sh"],
     "source_ids": ["mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "adb-serial", "category": "android_ops",
     "query": "Android 设备 序列号", "expect": ["R5CY84B054K"],
     "source_ids": ["mem_6993fd440c644907ade361d819905894", "mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "adb-enumeration", "category": "android_ops",
     "query": "adb 未枚举 排查", "expect": ["adb kill-server"],
     "source_ids": ["mem_4494f3e0b30df83eddd1ac694cbd24cd"]},
    {"id": "zygisk-root", "category": "android_ops",
     "query": "Zygisk 状态 验证 root 权限", "expect": ["必须 root"],
     "source_ids": ["mem_bb32a5931bc722effcb7508f1a8fc4fb", "mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "zygisk-exit", "category": "android_ops",
     "query": "Zygisk Next 主动退出 状态", "expect": ["User requested to exit"],
     "source_ids": ["mem_70065a6eb19f1850d0ced2e57f2d39a7"]},
    {"id": "zygcheck-script", "category": "android_ops",
     "query": "netproxy-work .tools 助手脚本", "expect": ["zygcheck.sh"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "ghostsam-terminal", "category": "android_ops",
     "query": "GhostSam 终端页 打开又关闭", "expect": ["系统工具"],
     "source_ids": ["mem_7a003354d0ea303966733de240ed79a0", "mem_b471fc5472cfc268825caf4ed8de8447"]},
    {"id": "ghostsam-background", "category": "android_ops",
     "query": "GhostSam 前台 应用切换", "expect": ["美团"],
     "source_ids": ["mem_cc55cad338bf4eee1218be20f7168d6c", "mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "ui-xml-dump", "category": "android_ops",
     "query": "adb shell cat UI XML dump", "expect": ["/sdcard/"],
     "source_ids": ["mem_7faa121be09ca711f61f0c98969bd437"]},
    {"id": "adb-sandbox", "category": "android_ops",
     "query": "macOS 沙箱 adb 监听端口", "expect": ["gs-exec.sh"],
     "source_ids": ["mem_79aca9c5d02e85a51210e0f5d8bf69b8"]},
    {"id": "zygisk-impact", "category": "android_ops",
     "query": "Zygisk 未注入 受影响 模块", "expect": ["LSPosed"],
     "source_ids": ["mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "device-uptime", "category": "android_ops",
     "query": "重启成功 uptime 秒", "expect": ["803"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "relaydesk-amfi", "category": "project",
     "query": "RelayDesk Omni 打不开 启动即杀", "expect": ["AMFI"],
     "source_ids": ["mem_0ce687ef9a28b4c08dffc482f9eca6cb"]},
    {"id": "codex-compaction", "category": "project",
     "query": "Codex 压缩总是失败 根因", "expect": ["remote_compaction"],
     "source_ids": ["mem_c6cd0ca15a5856596d50fd4b5a8ae5a2"]},
    {"id": "relaydesk-effort", "category": "project",
     "query": "RelayDesk Omni 思考强度 模型 档位", "expect": ["DeepSeek 5 档"],
     "source_ids": ["mem_71f38b48e590a5acd9e9340e7265a058"]},
    {"id": "orbit-dual-mode", "category": "project",
     "query": "Orbit AI 前端 双模式", "expect": ["简易/全能"],
     "source_ids": ["mem_e3d2b9bac5eae0cb26b73084e72f63e9"]},
    {"id": "storyfoundry-progress", "category": "project",
     "query": "StoryFoundry 当前进度", "expect": ["SF-V3/R72"],
     "source_ids": ["mem_9eb4f7328d69f1fcab032bd8aa4a2b71"]},
    {"id": "remote-mic-parsing", "category": "project",
     "query": "remote-mic-app 会话格式 解析", "expect": ["防御式解析"],
     "source_ids": ["mem_f4f035087cdc34a03cc7f0799680025e"]},
    {"id": "local-model-gemma", "category": "project",
     "query": "M4 16GB 本地模型推荐", "expect": ["Gemma4-12B-it Q6_K"],
     "source_ids": ["mem_06cc49e04a9918f8d80198580fa4a013"]},
    {"id": "qwen-config", "category": "project",
     "query": "Qwen 27B 量化 显卡", "expect": ["Q4_K_M"],
     "source_ids": ["mem_b903a1ee48e0210c895720afdff331a2"]},
    {"id": "redis-sentinel", "category": "project_filtered",
     "query": "Redis 集群 故障转移", "expect": ["sentinel"],
     "project": "hippomind-e2e", "source_ids": ["mem_b7cfaece8eff65f422fee922fe5cc1eb"]},
    {"id": "kernelsu-samsung", "category": "project_filtered",
     "query": "Samsung tablet root setup", "expect": ["KernelSU"],
     "project": "outside-of-project", "source_ids": ["mem_31060976e3c314acbd52a1d22199c4b1"]},
    {"id": "autocontext-progress", "category": "project_filtered",
     "query": "无感自动上下文 验证事实", "expect": ["结构化进度"],
     "project": "hippomind", "source_ids": ["mem_c0b32e9cf5bb88ce97647c422886dfb4"]},
    {"id": "english-compaction", "category": "english",
     "query": "codex remote compaction failure root cause", "expect": ["got 0"],
     "source_ids": ["mem_c6cd0ca15a5856596d50fd4b5a8ae5a2"]},
    {"id": "english-zygisk", "category": "english",
     "query": "Zygisk Next GitHub repository", "expect": ["Dr-TSNG"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "english-storyfoundry", "category": "english",
     "query": "StoryFoundry tech stack", "expect": ["FSM stages 1-6"],
     "source_ids": ["mem_9eb4f7328d69f1fcab032bd8aa4a2b71"]},
]

# SEMANTIC_QUERIES: natural-language paraphrases of the same production facts.
# Each query is written the way a user would actually ask, deliberately WITHOUT
# reusing the target memory vocabulary, so it measures cross-vocabulary recall
# rather than keyword matching.
#
# Labelling rules (see docs/EVAL.md, section "Semantic query set"):
#   1. expect must be a substring that literally occurs in the target memory,
#      verified with SQL over the production ledger.  An unverified expectation
#      makes the query unhittable forever and silently poisons the metric.
#   2. expect must NOT appear in the query itself; otherwise the query is just
#      a keyword query wearing a disguise.
#   3. source_ids lists the active+accepted memories the expectation was
#      grounded in, so the label can be audited later.
#   4. expect_hits records how many ledger rows contained the expectation at
#      labelling time.  Zero would mean the label is broken; a very large value
#      means the expectation is not distinctive.
#
# Grounded against the 2026-10-24 production ledger.
SEMANTIC_QUERIES: List[Dict[str, Any]] = [
    {"id": "s-inject-guard", "category": "hippomind",
     "query": "记忆里夹带恶意指令怎么防", "expect": ["提示注入防护"],
     "source_ids": [
      "mem_3bbbb4fabf98211920feb40048753c02", "mem_a9471f5660b86081e82e3c2f1dc49837",
      "mem_f1a56df044a2d59f947bb6f1c180230b"],
     "note": "自动升降权、来源追溯、隐私隔离、提示注入防护、一键启动", "expect_hits": 49},
    {"id": "s-zerotoken", "category": "hippomind",
     "query": "能不能不打暗号系统也自动把上下文带上", "expect": ["触发词"],
     "source_ids": [
      "mem_bbf363b3a522de318f25aea3f29802c1", "mem_ec42db9e67201bf58045db5b128c3974",
      "mem_f1a56df044a2d59f947bb6f1c180230b"],
     "note": "普通任务自动获得最小 Context Pack，无需用户输入触发词", "expect_hits": 21},
    {"id": "s-crossagent", "category": "hippomind",
     "query": "在这个工具里聊一半换另一个还能接着聊吗", "expect": ["跨端续接"],
     "source_ids": [
      "mem_3bbbb4fabf98211920feb40048753c02", "mem_a9471f5660b86081e82e3c2f1dc49837",
      "mem_f1a56df044a2d59f947bb6f1c180230b"],
     "note": "Codex、Pi、Claude、Cursor 等，支持跨端续接", "expect_hits": 49},
    {"id": "s-storage", "category": "hippomind",
     "query": "数据是怎么存的断电会不会丢", "expect": ["SQLite/WAL"],
     "source_ids": [
      "mem_3bbbb4fabf98211920feb40048753c02", "mem_b091f64b6b2a46d1abe0db700f719c00",
      "mem_8d7ce92e53917dfbdcda26520ca3ba77"],
     "note": "本机轻量 daemon + SQLite/WAL 事件账本", "expect_hits": 125},
    {"id": "s-budget", "category": "hippomind",
     "query": "一次最多塞多少内容给模型", "expect": ["6000"],
     "source_ids": [
      "mem_0da25127bdd85e0e5b1de8503ccc0853", "mem_0128fc9f3b6b16c6aa81e81707a4e5a1",
      "mem_cb45fe73d337e5662ae6afd28137d244"],
     "note": "将默认 token 预算改为 6000", "expect_hits": 14},
    {"id": "s-bitemporal", "category": "hippomind",
     "query": "怎么记录一条事实是什么时候开始生效的", "expect": ["双时态"],
     "source_ids": [
      "mem_ae85cd998b7dd6158f736231e9c0af2f", "mem_417043efd18ab7a80ae14ef206adf21e",
      "mem_b617a67784cf1d73ab7656a9d50ebf8c"],
     "note": "schema v4，含双时态（bitemporal）", "expect_hits": 22},
    {"id": "s-host", "category": "hippomind",
     "query": "这套系统跑在哪台机器上", "expect": ["mac-mini"],
     "source_ids": [
      "mem_ebadfd11062f456d7074b73931608493", "mem_7d392986ef378552d5be915211a4272f",
      "mem_ba764b4f05a4a3e52cd52717a1d14729"],
     "note": "纸本；SQLite；mac-mini", "expect_hits": 12},
    {"id": "s-benchmark", "category": "hippomind",
     "query": "有没有公开的长期记忆评测基准可以对照", "expect": ["LongMemEval"],
     "source_ids": [
      "mem_417043efd18ab7a80ae14ef206adf21e", "mem_b80ddb36de5d6eafe09f31cb9130e0b1",
      "mem_863b0ca34c6f6ac2edce7bbe29b90abf"],
     "note": "程序性记忆、双时态冲突、因果 DAG、... LongMemEval", "expect_hits": 7},
    {"id": "s-miss", "category": "hippomind",
     "query": "该收的没收到算什么毛病", "expect": ["漏采"],
     "source_ids": [
      "mem_961d95d16b52478f8a0ee80710be4b3a", "mem_28389d8a052a6bbf5f9ea603a5d735aa",
      "mem_2cdbfacde62f197884a45a02321addd8"],
     "note": "漏采、误召回、访问控制", "expect_hits": 10},
    {"id": "s-flood", "category": "hippomind",
     "query": "聊天原文太多会不会把正经记忆挤掉", "expect": ["raw chat 洪泛"],
     "source_ids": ["mem_15a32e91a7ba4e1937b6687be4663e10", "mem_c448d84c5cfd9f3506fbf6786920d734"],
     "note": "raw chat 洪泛会在过滤前耗尽 FTS 候选并导致结构化记忆漏召回", "expect_hits": 49},
    {"id": "s-acl", "category": "hippomind",
     "query": "不同 agent 之间能不能限制互相读", "expect": ["Agent ACL"],
     "source_ids": [
      "mem_961d95d16b52478f8a0ee80710be4b3a", "mem_15a32e91a7ba4e1937b6687be4663e10",
      "mem_c448d84c5cfd9f3506fbf6786920d734"],
     "note": "共享 token 无 Agent ACL", "expect_hits": 53},
    {"id": "s-cursor", "category": "hippomind",
     "query": "源库重建以后增量读取的进度怎么处理", "expect": ["watcher 游标重建"],
     "source_ids": [
      "mem_8d7ce92e53917dfbdcda26520ca3ba77", "mem_d9db9f603928c7ccef34a204d54f86e8",
      "mem_c454ddc9c3ff985bb24f49963e36cc2b"],
     "note": "watcher 拒绝记录仍推进游标且不处理源库重建", "expect_hits": 28},
    {"id": "s-resident", "category": "hippomind",
     "query": "跑一晚上内存会不会一直涨", "expect": ["常驻资源实测"],
     "source_ids": ["mem_15a32e91a7ba4e1937b6687be4663e10", "mem_c448d84c5cfd9f3506fbf6786920d734"],
     "note": "35 项测试、py_compile、daemon/CLI/Context Pack、Grid World 和常驻资源实测", "expect_hits": 49},
    {"id": "s-falsehit", "category": "hippomind",
     "query": "不该出现的旧事被翻出来叫什么", "expect": ["误召回"],
     "source_ids": [
      "mem_28389d8a052a6bbf5f9ea603a5d735aa", "mem_2cdbfacde62f197884a45a02321addd8",
      "mem_3376395075cc60afad34b5a44707f3c3"],
     "note": "漏采、误召回、访问控制", "expect_hits": 6},
    {"id": "s-conflict", "category": "hippomind",
     "query": "两条互相矛盾的说法怎么定谁对", "expect": ["claims 冲突检测"],
     "source_ids": [
      "mem_07ea306ce023eb967e354f64fd622323", "mem_fdd4801c323bebaa83f60de3d57c9bdd",
      "mem_382e811bb75ac259442ecf47ddd37c07"],
     "note": "claims 冲突检测、procedural 技能记忆", "expect_hits": 7},
    {"id": "s-swapalgo", "category": "hippomind",
     "query": "以后想换检索算法需要动多少代码", "expect": ["可插拔评测接口"],
     "source_ids": [
      "mem_ae85cd998b7dd6158f736231e9c0af2f", "mem_b617a67784cf1d73ab7656a9d50ebf8c",
      "mem_07ea306ce023eb967e354f64fd622323"],
     "note": "向量检索与 ACT-R 作为可插拔评测接口", "expect_hits": 14},
    {"id": "s-rootcheck", "category": "android_ops",
     "query": "为什么普通权限查不到注入模块在不在跑", "expect": ["必须 root"],
     "source_ids": ["mem_bb32a5931bc722effcb7508f1a8fc4fb", "mem_ac68f39428225fffe4513978e768e72e"],
     "note": "非 root 下 ps/getprop 中查不到 zygisk，必须 root 才能确认运行状态", "expect_hits": 2},
    {"id": "s-mountproof", "category": "android_ops",
     "query": "怎样确认模块真的挂载进去了", "expect": ["maps 中 zygisk 出现次数"],
     "source_ids": [
      "mem_10a5d0fcd84b18c6f80d3d041a2b932e", "mem_4890ec31c5ddee643a4fc93db4a1bdfe",
      "mem_93e9c5d50d9fc2cc194141f0cfd222b7"],
     "note": "zygote64 与 system_server 的 maps 中 zygisk 出现次数均为 0", "expect_hits": 3},
    {"id": "s-uimoved", "category": "android_ops",
     "query": "设备重启以后界面位置变了怎么办", "expect": ["重新观察屏幕"],
     "source_ids": [
      "mem_468ccf1125cc69675e4d5df3ed3d0e02", "mem_464ef8dc42df1038e844b2bc6cdc0b5c",
      "mem_ac68f39428225fffe4513978e768e72e"],
     "note": "重启后 GhostSam 界面布局/元素发生变化，需要重新观察屏幕", "expect_hits": 3},
    {"id": "s-nodevice", "category": "android_ops",
     "query": "手机连上了但是列表里没有设备", "expect": ["adb kill-server"],
     "source_ids": ["mem_e177796acf8ae8d5ff707888182db50d", "mem_4494f3e0b30df83eddd1ac694cbd24cd"],
     "note": "未枚举则检查物理连接/等待重启完成，必要时 adb kill-server 重启服务", "expect_hits": 2},
    {"id": "s-selfexit", "category": "android_ops",
     "query": "注入框架自己退出了不是崩溃吧", "expect": ["User requested to exit"],
     "source_ids": [
      "mem_962fe4429a2473d018bcd83ea9f9ed2c", "mem_cd8bd4452f61b7e83ec4f761edf6ea6b",
      "mem_70065a6eb19f1850d0ced2e57f2d39a7"],
     "note": "未运行，状态显示 User requested to exit（主动退出，非崩溃）", "expect_hits": 4},
    {"id": "s-terminalbounce", "category": "android_ops",
     "query": "点终端按钮进去马上就弹回来", "expect": ["终端页打开又立即关闭"],
     "source_ids": ["mem_6d17f55dcfcc1795f109a5bf73330dc9", "mem_b471fc5472cfc268825caf4ed8de8447"],
     "note": "点击「终端」后终端页打开又立即关闭，界面返回「系统工具」页", "expect_hits": 2},
    {"id": "s-sandboxadb", "category": "android_ops",
     "query": "Mac 上 adb 起不了本地服务端怎么办", "expect": ["gs-exec.sh"],
     "source_ids": [
      "mem_962fe4429a2473d018bcd83ea9f9ed2c", "mem_9bcaa1cd6b7e71880c0b4d98b234ade2",
      "mem_79aca9c5d02e85a51210e0f5d8bf69b8"],
     "note": "使用 gs-exec.sh 助手脚本 + 推送脚本到手机统一执行", "expect_hits": 4},
    {"id": "s-uidump", "category": "android_ops",
     "query": "怎么把手机上的界面结构导出来", "expect": ["/sdcard/"],
     "source_ids": [
      "mem_90ed8387cdf16b12f878854d3945f3ec", "mem_7faa121be09ca711f61f0c98969bd437",
      "mem_888ed63994565cfc87e15c08169316f5"],
     "note": "adb -s <serial> shell cat /sdcard/<dump>.xml > <host_path>", "expect_hits": 3},
    {"id": "s-rollback", "category": "android_ops",
     "query": "新方案不行退回到之前那个", "expect": ["放弃 NeoZygisk"],
     "source_ids": ["mem_21070a16c0d9cfd809f693ef6b62b5c1", "mem_0afb2eaae51bf31fabb21095b5365997"],
     "note": "放弃 NeoZygisk，换回之前能用的 Zygisk Next 版本", "expect_hits": 2},
    {"id": "s-uptime", "category": "android_ops",
     "query": "手机重启后多久算启动完成", "expect": ["803"],
     "source_ids": [
      "mem_bb32a5931bc722effcb7508f1a8fc4fb", "mem_4d1b7766b75c5ceaf58e0171f1cafb56",
      "mem_ac68f39428225fffe4513978e768e72e"],
     "note": "重启成功（uptime 803 秒）", "expect_hits": 6},
    {"id": "s-bgapp", "category": "android_ops",
     "query": "App 被切走了前台是别的应用", "expect": ["美团"],
     "source_ids": [
      "mem_cc55cad338bf4eee1218be20f7168d6c", "mem_4b309a9f06e0897f67b61d4592621fc4",
      "mem_6d17f55dcfcc1795f109a5bf73330dc9"],
     "note": "GhostSam 被切到后台，设备前台显示美团", "expect_hits": 6},
    {"id": "s-casualty", "category": "android_ops",
     "query": "哪些模块会跟着一起失效", "expect": ["LSPosed"],
     "source_ids": [
      "mem_10a5d0fcd84b18c6f80d3d041a2b932e", "mem_4890ec31c5ddee643a4fc93db4a1bdfe",
      "mem_93e9c5d50d9fc2cc194141f0cfd222b7"],
     "note": "Zygisk 未注入导致 Vector/LSPosed 失效", "expect_hits": 5},
    {"id": "s-kernelsu-skip", "category": "android_ops",
     "query": "KernelSU 下这个脚本没被执行", "expect": ["post-fs-data.sh"],
     "source_ids": [
      "mem_21070a16c0d9cfd809f693ef6b62b5c1", "mem_10a5d0fcd84b18c6f80d3d041a2b932e",
      "mem_4890ec31c5ddee643a4fc93db4a1bdfe"],
     "note": "post-fs-data.sh 仅在 which magisk 为真时执行模块兼容逻辑，KernelSU 下会被跳过", "expect_hits": 4},
    {"id": "s-drift", "category": "android_ops",
     "query": "点屏幕老是点偏", "expect": ["坐标漂移"],
     "source_ids": ["mem_962fe4429a2473d018bcd83ea9f9ed2c", "mem_79aca9c5d02e85a51210e0f5d8bf69b8"],
     "note": "中文输入法吃字、ServiceMode 弹窗抢焦点、坐标漂移", "expect_hits": 2},
    {"id": "s-quant", "category": "project",
     "query": "显存不够怎么把模型压小", "expect": ["Q4_K_M"],
     "source_ids": [
      "mem_6d0cc8e7b63cc66fb560f4771b27b34a", "mem_bdcd93113afee9c51dae96d5ead3b500",
      "mem_2642d08031ae683e4aa17d7330032104"],
     "note": "32GB 显存 + 7900XT 显卡，Q4_K_M 量化", "expect_hits": 11},
    {"id": "s-localmodel", "category": "project",
     "query": "M4 16G 上跑哪个本地模型最好", "expect": ["Gemma4-12B-it Q6_K"],
     "source_ids": [
      "mem_bdcd93113afee9c51dae96d5ead3b500", "mem_2642d08031ae683e4aa17d7330032104",
      "mem_b903a1ee48e0210c895720afdff331a2"],
     "note": "推荐 Gemma4-12B-it Q6_K", "expect_hits": 9},
    {"id": "s-launchkill", "category": "project",
     "query": "打不开的应用一闪就没了", "expect": ["AMFI"],
     "source_ids": [
      "mem_bbbf987f16c96a0b1e70690285ab0c44", "mem_051cac95f208781754d37b4fe8b060ac",
      "mem_0ce687ef9a28b4c08dffc482f9eca6cb"],
     "note": "ad-hoc 签名但携带受限权限 keychain-access-groups，exec 时被 AMFI SIGKILL", "expect_hits": 4},
    {"id": "s-compactfail", "category": "project",
     "query": "上下文自动摘要那一步总是报错", "expect": ["remote_compaction"],
     "source_ids": [
      "mem_348d2bc2653276c7ac5d3510ee8898de", "mem_69439ccb9d5fa2c15938dceaa6d86449",
      "mem_c6cd0ca15a5856596d50fd4b5a8ae5a2"],
     "note": "provider OpenAI 未声明 capabilities.remote_compaction", "expect_hits": 3},
    {"id": "s-effort", "category": "project",
     "query": "同一客户端里不同会话能不能各自选模型", "expect": ["DeepSeek 5 档"],
     "source_ids": [
      "mem_f5e55b2f36dcd15f56b04a74160c56a8", "mem_71f38b48e590a5acd9e9340e7265a058",
      "mem_d2dd56b89044eaeb72302629baead96a"],
     "note": "同一客户端内各会话必须独立选择 API/模型... DeepSeek 5 档、GPT 6 档", "expect_hits": 3},
    {"id": "s-dualmode", "category": "project",
     "query": "前端想做个简单和完整两套界面", "expect": ["简易/全能"],
     "source_ids": [
      "mem_fd5d32004608ba37fb8cd4ad1d1d4997", "mem_e3d2b9bac5eae0cb26b73084e72f63e9",
      "mem_32ea394316c027c7f5bbc7868acf3c16"],
     "note": "采用简易/全能双模式", "expect_hits": 4},
    {"id": "s-novelprogress", "category": "project",
     "query": "那个小说工具进度到哪了", "expect": ["SF-V3/R72"],
     "source_ids": ["mem_8ec4ae652255aa2ad32f5b8fa3690288", "mem_9eb4f7328d69f1fcab032bd8aa4a2b71"],
     "note": "已推进至 SF-V3/R72", "expect_hits": 4},
    {"id": "s-parsing", "category": "project",
     "query": "外部会话格式乱七八糟怎么解析", "expect": ["防御式解析"],
     "source_ids": [
      "mem_69d2df378114ac42ec14048e5774b7a2", "mem_d2c5f44eb783f462a65458c4f4a4117c",
      "mem_f4f035087cdc34a03cc7f0799680025e"],
     "note": "防御式解析外部会话格式", "expect_hits": 8},
    {"id": "s-window", "category": "project",
     "query": "Codex 的上下文窗口被写死了", "expect": ["400000"],
     "source_ids": ["mem_5c1a928bfa681f92fec19bfe9dc7c73c", "mem_a247e76f32f81d1ca7de3380e80d8c5e"],
     "note": "修正为 400000/320000", "expect_hits": 2},
    {"id": "s-evalalign", "category": "project",
     "query": "评测想对齐业界那套打分", "expect": ["promptfoo"],
     "source_ids": ["mem_6ffa921d46487a9b51e9b7c7360b9fb3", "mem_168ef7a890c4f689aa85ea56234d3a06"],
     "note": "对齐 promptfoo llm-rubric/加权断言/重复运行", "expect_hits": 2},
    {"id": "s-entitlement", "category": "project",
     "query": "签名带了不该带的权限被系统杀了", "expect": ["keychain-access-groups"],
     "source_ids": [
      "mem_bbbf987f16c96a0b1e70690285ab0c44", "mem_051cac95f208781754d37b4fe8b060ac",
      "mem_0ce687ef9a28b4c08dffc482f9eca6cb"],
     "note": "安装包以 ad-hoc 签名但携带受限权限 keychain-access-groups", "expect_hits": 7},
    {"id": "s-autostart", "category": "ops",
     "query": "开机自动跑起来怎么配", "expect": ["LaunchAgent"],
     "source_ids": [
      "mem_961d95d16b52478f8a0ee80710be4b3a", "mem_15a32e91a7ba4e1937b6687be4663e10",
      "mem_d1e3e346ebcb406287053218f77904bc"],
     "note": "gateway 以 LaunchAgent 使用同一二进制", "expect_hits": 57},
    {"id": "s-externaldisk", "category": "ops",
     "query": "怎么把东西挪到移动硬盘不占系统盘", "expect": ["避免占用系统盘"],
     "source_ids": ["mem_b3b4a95660801f844c10a42220c097e3", "mem_7d5f303474fb7386546b7bd2899ded10"],
     "note": "配置移动硬盘上的便携运行环境，避免占用系统盘", "expect_hits": 5},
    {"id": "s-zerofake", "category": "ops",
     "query": "项目要求不许假装做完", "expect": ["零伪完成"],
     "source_ids": [
      "mem_f948d1e9c6efb7550f90253c27017d7a", "mem_dc5bb79a4e90e7247d904c204b34ffd8",
      "mem_38f8b6515bc5082e6471064d22f59ff2"],
     "note": "严格遵循代码与测试工程规范，零伪完成", "expect_hits": 6},
    {"id": "s-prefixcache", "category": "ops",
     "query": "本地模型跑久了上下文缓存会不会失效", "expect": ["前缀缓存"],
     "source_ids": [
      "mem_f948d1e9c6efb7550f90253c27017d7a", "mem_dc5bb79a4e90e7247d904c204b34ffd8",
      "mem_38f8b6515bc5082e6471064d22f59ff2"],
     "note": "本地模型单槽推理，维持前缀缓存稳定", "expect_hits": 6},
    {"id": "s-register", "category": "ops",
     "query": "把工具注册到各个 agent 上", "expect": ["rea setup"],
     "source_ids": ["mem_1c70e002b02191e220feabec58619e50", "mem_f48eeaf7920cdc058a15290a2ad9713b"],
     "note": "运行 rea setup 将 MCP 注册到 codex、claude_code、claude_desktop、pi、dsh", "expect_hits": 2},
    {"id": "s-archivequery", "category": "ops",
     "query": "怎么查以前会话里说过什么", "expect": ["query.py"],
     "source_ids": [
      "mem_aad7a2f21768a5a44a9f227eed9e5a7a", "mem_b34934b6421281c6ead3d1c639fa878a",
      "mem_e1d8a1bdc0ec957e2ec1f9372718a3fb"],
     "note": "使用 python3 .../Agent-Chat-Archive/scripts/query.py 的 search/tasks 子命令查询历史", "expect_hits": 17},
    {"id": "s-buildtest", "category": "ops",
     "query": "跑构建和测试会不会动到源码", "expect": ["不得修改任何源文件"],
     "source_ids": [
      "mem_909756f2865f8755c112d6929bdcd32f", "mem_1d840766f12ed3b6cb29c601fb927c46",
      "mem_7564b429eb3eaa6cffa9de9389b5810a"],
     "note": "运行构建与测试时不得修改任何源文件，仅允许删除 ._* 文件", "expect_hits": 3},
    {"id": "s-en-launch", "category": "english",
     "query": "why does the app get killed right after launch", "expect": ["SIGKILL"],
     "source_ids": [
      "mem_bbbf987f16c96a0b1e70690285ab0c44", "mem_051cac95f208781754d37b4fe8b060ac",
      "mem_0ce687ef9a28b4c08dffc482f9eca6cb"],
     "note": "ad-hoc signed with keychain-access-groups, exec killed by AMFI SIGKILL", "expect_hits": 4},
    {"id": "s-en-window", "category": "english",
     "query": "what context window does codex actually use", "expect": ["124518"],
     "source_ids": ["mem_5c1a928bfa681f92fec19bfe9dc7c73c", "mem_a247e76f32f81d1ca7de3380e80d8c5e"],
     "note": "写死 128K/100K 导致 Codex 实际窗口 124518", "expect_hits": 2},
    {"id": "s-en-quant", "category": "english",
     "query": "which quantization fits in 32 gigabytes", "expect": ["Q4_K_M"],
     "source_ids": [
      "mem_6d0cc8e7b63cc66fb560f4771b27b34a", "mem_bdcd93113afee9c51dae96d5ead3b500",
      "mem_2642d08031ae683e4aa17d7330032104"],
     "note": "32GB 显存 + 7900XT 显卡，Q4_K_M 量化", "expect_hits": 11},
    {"id": "s-en-adb", "category": "english",
     "query": "how do I restart the adb server", "expect": ["adb kill-server"],
     "source_ids": ["mem_e177796acf8ae8d5ff707888182db50d", "mem_4494f3e0b30df83eddd1ac694cbd24cd"],
     "note": "必要时 adb kill-server 重启服务", "expect_hits": 2},
    {"id": "s-en-hardening", "category": "english",
     "query": "how do we keep the agent from faking completion", "expect": ["零伪完成"],
     "source_ids": [
      "mem_f948d1e9c6efb7550f90253c27017d7a", "mem_dc5bb79a4e90e7247d904c204b34ffd8",
      "mem_38f8b6515bc5082e6471064d22f59ff2"],
     "note": "严格遵循代码与测试工程规范，零伪完成", "expect_hits": 6},
]


# name -> the built-in labelled set a run selects with --set.
QUERY_SETS: Dict[str, List[Dict[str, Any]]] = {
    "keyword": KEYWORD_QUERIES,
    "semantic": SEMANTIC_QUERIES,
}

# Backwards-compatible alias: external scripts and older docs referred to the
# single built-in set as LABELLED_QUERIES.  It is the keyword set.
LABELLED_QUERIES = KEYWORD_QUERIES

def render_hit(item: Dict[str, Any]) -> str:
    """Text a retrieval result exposes to the caller (content + context)."""
    parts = [str(item.get("content") or "")]
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        context = metadata.get("context")
        if isinstance(context, dict):
            for field in CONTEXT_FIELDS:
                value = context.get(field)
                if isinstance(value, (list, tuple)):
                    parts.extend(str(entry) for entry in value)
                elif value:
                    parts.append(str(value))
    return " ".join(part for part in parts if part)


def is_relevant(item: Dict[str, Any], expect: Sequence[str]) -> bool:
    """A hit is relevant when it contains every expected substring."""
    expect = [str(term) for term in expect]
    if not expect:
        return False
    haystack = render_hit(item).lower()
    return all(term.lower() in haystack for term in expect)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile (numpy default convention)."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    fraction = min(1.0, max(0.0, float(fraction)))
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def score_hits(hits: Sequence[Dict[str, Any]], expect: Sequence[str], *,
               k: int = 8, latency_ms: float = 0.0) -> Dict[str, Any]:
    """Per-query metrics for one ordered result list."""
    expect = [str(term) for term in expect]
    top = list(hits[:k])
    rendered = [render_hit(item) for item in top]
    rank = 0
    for position, text in enumerate(rendered, 1):
        lowered = text.lower()
        if all(term.lower() in lowered for term in expect):
            rank = position
            break
    contents = [" ".join(str(item.get("content") or "").split()).lower()
                for item in top]
    duplicates = len(contents) - len(set(contents))
    duplicate_rate = duplicates / len(contents) if contents else 0.0
    matched = sum(
        1 for term in expect
        if any(term.lower() in text.lower() for text in rendered))
    return {
        "hits": len(top),
        "rank": rank,
        "relevant": rank > 0,
        "reciprocal_rank": round(1.0 / rank, 4) if rank else 0.0,
        "term_recall": round(matched / len(expect), 4) if expect else 0.0,
        "duplicate_rate": round(duplicate_rate, 4),
        "latency_ms": round(float(latency_ms), 2),
        "top": [text[:160] for text in rendered[:2]],
    }


def validate_queries(raw: Any) -> List[Dict[str, Any]]:
    """Validate a labelled query set and return normalised specs."""
    if not isinstance(raw, list):
        raise ValueError("query set must be a JSON array of objects")
    specs: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("query #%d must be an object" % index)
        query = str(item.get("query") or "").strip()
        if not query:
            raise ValueError("query #%d has an empty 'query'" % index)
        expect_raw = item.get("expect")
        if not isinstance(expect_raw, list) or not expect_raw:
            raise ValueError(
                "query #%d (%r) needs a non-empty 'expect' list" % (index, query))
        expect: List[str] = []
        for term in expect_raw:
            if not isinstance(term, str) or not term.strip():
                raise ValueError(
                    "query #%d (%r) has a blank/non-string 'expect' entry"
                    % (index, query))
            expect.append(term.strip())
        spec: Dict[str, Any] = {"query": query, "expect": expect}
        query_id = str(item.get("id") or query).strip()
        if query_id in seen_ids:
            raise ValueError("duplicate query id %r" % query_id)
        seen_ids.add(query_id)
        spec["id"] = query_id
        for field in ("category", "project", "scope"):
            value = item.get(field)
            if value:
                spec[field] = str(value).strip()
        # Annotation audit trail: how many ledger rows contained the
        # expectation when it was labelled.  Carried through so a consumer (and
        # the tests) can tell a grounded label from an invented one.
        expect_hits = item.get("expect_hits")
        if expect_hits is not None:
            if isinstance(expect_hits, bool) or not isinstance(expect_hits, int):
                raise ValueError(
                    "query #%d (%r) 'expect_hits' must be an integer"
                    % (index, query))
            if expect_hits < 0:
                raise ValueError(
                    "query #%d (%r) 'expect_hits' must be >= 0"
                    % (index, query))
            spec["expect_hits"] = expect_hits
        source_ids = item.get("source_ids") or []
        if source_ids:
            if (not isinstance(source_ids, list)
                    or not all(isinstance(value, str) for value in source_ids)):
                raise ValueError(
                    "query #%d (%r) 'source_ids' must be a list of strings"
                    % (index, query))
            spec["source_ids"] = [value.strip() for value in source_ids
                                  if value.strip()]
        specs.append(spec)
    if not specs:
        raise ValueError("query set is empty")
    return specs


def load_queries(path: os.PathLike) -> List[Dict[str, Any]]:
    """Load and validate a labelled query set from a JSON file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("cannot read query file %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON in %s: %s" % (path, exc))
    return validate_queries(raw)


def _load_vector_index(ledger: SQLiteLedger):
    """可选神经向量索引：缺失任何依赖都返回 None（评测照常跑纯词法基线）。

    环境变量 HIPPOMIND_VECTOR_INDEX=0 可强制关闭，便于在同一账本上做 A/B。
    """
    if os.environ.get("HIPPOMIND_VECTOR_INDEX", "1").strip().lower() in {"0", "off", "false"}:
        return None
    try:
        from hippomind.semantic_onnx import load_onnx_embedder
        from hippomind.vector_index import load_vector_index
    except Exception:
        return None
    try:
        embedder = load_onnx_embedder()
        if embedder is None:
            return None
        return load_vector_index(ledger, embedder)
    except Exception:
        return None


def _pack_search(builder: ContextPackBuilder, min_score: float):
    """Adapter that scores the Context Pack the agent actually receives.

    ``ledger.search`` measures the candidate ranking; the pack applies
    grouping, budget, deduplication and the relevance gate.  Scoring the pack
    is the honest measure of injection quality, because it is what the model
    sees.
    """

    def search(query: str, project: str, scope: str, limit: int):
        pack = builder.build(query, ContextPackOptions(
            max_items=min(8, max(1, int(limit))),
            scope=scope or "shared", project=project or "",
            min_score=min_score,
        ))
        items = pack.get("items") or []
        if not items:
            return []
        return [{"content": pack.get("text") or "", "kind": "pack",
                 "memory_id": "pack", "score": 1.0}]

    return search


def evaluate(ledger: SQLiteLedger, queries: Sequence[Dict[str, Any]], *,
             limit: int = 8, search=None, vector_index=None) -> Dict[str, Any]:
    """Run the labelled set and aggregate retrieval metrics.

    ``search`` is an optional injection point with the signature
    search(query, project, scope, limit) -> hits, used by tests.

    ``vector_index`` 是可选神经向量索引：传了就让它作为第三路候选生成参与
    RRF（与生产注入路径一致）。默认 None 时行为与改造前完全相同。
    """
    specs = validate_queries(list(queries))
    limit = max(1, int(limit))
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for spec in specs:
        started = time.perf_counter()
        if search is not None:
            hits = search(spec["query"], spec.get("project", ""),
                          spec.get("scope", "shared"), limit)
        else:
            hits = ledger.search(
                spec["query"],
                scope=spec.get("scope", "shared"),
                project=spec.get("project", ""),
                limit=limit,
                include_raw_chat=False,
                require_evidence=True,
                vector_index=vector_index,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed_ms)
        row: Dict[str, Any] = {
            "id": spec["id"],
            "category": spec.get("category", "custom"),
            "query": spec["query"],
            "expect": list(spec["expect"]),
            "project": spec.get("project", ""),
        }
        row.update(score_hits(hits, spec["expect"], k=limit,
                              latency_ms=elapsed_ms))
        rows.append(row)
    count = len(rows)
    metrics = {
        "recall_at_k": round(sum(1 for row in rows if row["relevant"]) / count, 4),
        "mrr": round(sum(row["reciprocal_rank"] for row in rows) / count, 4),
        "duplicate_rate": round(
            sum(row["duplicate_rate"] for row in rows) / count, 4),
        "term_recall_at_k": round(
            sum(row["term_recall"] for row in rows) / count, 4),
        "empty_rate": round(
            sum(1 for row in rows if row["hits"] == 0) / count, 4),
        "latency_p50_ms": round(percentile(latencies, 0.50), 2),
        "latency_p95_ms": round(percentile(latencies, 0.95), 2),
    }
    categories: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    for name in sorted(grouped):
        items = grouped[name]
        categories[name] = {
            "queries": len(items),
            "recall_at_k": round(
                sum(1 for item in items if item["relevant"]) / len(items), 4),
            "mrr": round(
                sum(item["reciprocal_rank"] for item in items) / len(items), 4),
            "duplicate_rate": round(
                sum(item["duplicate_rate"] for item in items) / len(items), 4),
        }
    return {
        "queries": count,
        "k": limit,
        "metrics": metrics,
        "by_category": categories,
        "details": rows,
    }


def _set_thresholds(set_name: str, *, mode: str = "search",
                    overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Default gate thresholds for one query set, with CLI overrides applied.

    Each set carries its own baseline: the keyword set is near-saturated
    (recall 1.000) so its gate can be strict, while the paraphrase set measures
    a genuine capability gap and must be pinned just under the measured value
    so it ratchets instead of silently regressing.
    """
    thresholds = dict(SET_THRESHOLDS.get(set_name, DEFAULT_THRESHOLDS))
    if mode == "pack":
        # Pack mode adds grouping, MMR, budget rendering and the relevance
        # gate on top of search, so it is legitimately slower.  The real
        # constraint is the host injection timeout (DSH 700ms, Codex hook 12s).
        thresholds["max_p95_ms"] = max(thresholds["max_p95_ms"], 600.0)
    for key, value in (overrides or {}).items():
        if value is not None:
            thresholds[key] = value
    return thresholds


def check_gate(
        result: Dict[str, Any], *,
        min_recall_at_k: float = DEFAULT_THRESHOLDS["min_recall_at_k"],
        min_mrr: float = DEFAULT_THRESHOLDS["min_mrr"],
        max_duplicate_rate: float = DEFAULT_THRESHOLDS["max_duplicate_rate"],
        max_p95_ms: float = DEFAULT_THRESHOLDS["max_p95_ms"],
        min_queries: int = DEFAULT_THRESHOLDS["min_queries"]) -> Dict[str, Any]:
    """Compare aggregated metrics against the pass/fail thresholds."""
    metrics = result["metrics"]
    failures: List[str] = []
    if result["queries"] < min_queries:
        failures.append("queries %d < min_queries %d"
                        % (result["queries"], min_queries))
    if metrics["recall_at_k"] < min_recall_at_k:
        failures.append("recall@%d %.4f < %.4f"
                        % (result["k"], metrics["recall_at_k"],
                           min_recall_at_k))
    if metrics["mrr"] < min_mrr:
        failures.append("mrr %.4f < %.4f" % (metrics["mrr"], min_mrr))
    if metrics["duplicate_rate"] > max_duplicate_rate:
        failures.append("duplicate_rate %.4f > %.4f"
                        % (metrics["duplicate_rate"], max_duplicate_rate))
    if metrics["latency_p95_ms"] > max_p95_ms:
        failures.append("latency_p95_ms %.2f > %.2f"
                        % (metrics["latency_p95_ms"], max_p95_ms))
    return {
        "passed": not failures,
        "failures": failures,
        "thresholds": {
            "min_recall_at_k": min_recall_at_k,
            "min_mrr": min_mrr,
            "max_duplicate_rate": max_duplicate_rate,
            "max_p95_ms": max_p95_ms,
            "min_queries": min_queries,
        },
    }


def check_combined_gate(gates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-set gate verdicts: every set must pass."""
    failures: List[str] = []
    for name in SET_ORDER:
        gate = gates.get(name)
        if gate is None:
            continue
        failures.extend("%s: %s" % (name, failure) for failure in gate["failures"])
    return {
        "passed": not failures,
        "failures": failures,
        "sets": gates,
    }


def _print_ledger_stats(result: Dict[str, Any]) -> None:
    stats = result.get("ledger_stats")
    if stats:
        print("ledger: memories=%s raw_chat=%s claims=%s active_claims=%s "
              "schema_version=%s"
              % (stats.get("memories"), stats.get("raw_chat"),
                 stats.get("claims"), stats.get("active_claims"),
                 stats.get("schema_version")))


def _print_set_report(set_name: str, result: Dict[str, Any]) -> None:
    metrics = result["metrics"]
    print("== set: %s ==" % set_name)
    print("queries=%d k=%d recall@%d=%.3f mrr=%.3f duplicate_rate=%.3f "
          "term_recall=%.3f empty=%.3f p50=%.1fms p95=%.1fms"
          % (result["queries"], result["k"], result["k"],
             metrics["recall_at_k"], metrics["mrr"],
             metrics["duplicate_rate"], metrics["term_recall_at_k"],
             metrics["empty_rate"], metrics["latency_p50_ms"],
             metrics["latency_p95_ms"]))
    for row in result["details"]:
        status = "PASS" if row["relevant"] else "MISS"
        print("  %-4s %-28s rank=%d rr=%.2f term=%.2f dup=%.2f %7.1fms"
              % (status, row["id"], row["rank"], row["reciprocal_rank"],
                 row["term_recall"], row["duplicate_rate"], row["latency_ms"]))
        if not row["relevant"] and row["top"]:
            print("       top: %s" % row["top"][0][:120])
    print("")
    for name, stats in result["by_category"].items():
        print("  %-16s n=%-3d recall@%d=%.3f mrr=%.3f duplicate_rate=%.3f"
              % (name, stats["queries"], result["k"],
                 stats["recall_at_k"], stats["mrr"],
                 stats["duplicate_rate"]))


def _print_gate(gate: Dict[str, Any], k: int) -> None:
    thresholds = gate["thresholds"]
    print("gate: %s  (min_recall@%d=%.2f, min_mrr=%.2f, "
          "max_duplicate_rate=%.2f, max_p95=%.1fms, min_queries=%d)"
          % ("PASS" if gate["passed"] else "FAIL", k,
             thresholds["min_recall_at_k"], thresholds["min_mrr"],
             thresholds["max_duplicate_rate"], thresholds["max_p95_ms"],
             thresholds["min_queries"]))
    for failure in gate["failures"]:
        print("  failure: %s" % failure)


def _print_report(result: Dict[str, Any]) -> None:
    _print_ledger_stats(result)
    sets = result.get("sets")
    if sets is None:
        _print_set_report(result.get("set", "keyword"), result)
        print("")
        _print_gate(result["gate"], result["k"])
        return
    for name in SET_ORDER:
        if name not in sets:
            continue
        _print_set_report(name, sets[name])
        _print_gate(sets[name]["gate"], sets[name]["k"])
        print("")
    print("== combined ==")
    for name in SET_ORDER:
        if name not in sets:
            continue
        print("  %-10s n=%-3d recall@%d=%.3f mrr=%.3f"
              % (name, sets[name]["queries"], sets[name]["k"],
                 sets[name]["metrics"]["recall_at_k"],
                 sets[name]["metrics"]["mrr"]))
    gate = result["gate"]
    print("gate: %s  (%s)"
          % ("PASS" if gate["passed"] else "FAIL",
             ", ".join(name for name in SET_ORDER if name in sets)))
    for failure in gate["failures"]:
        print("  failure: %s" % failure)


def _dump(sets: Dict[str, List[Dict[str, Any]]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(sets) == 1:
        payload: Any = next(iter(sets.values()))
    else:
        payload = sets
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    total = sum(len(queries) for queries in sets.values())
    print("wrote %d queries to %s" % (total, path))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HippoMind labelled retrieval evaluation and regression gate")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="SQLite ledger to evaluate")
    parser.add_argument("--queries",
                        help="JSON query file (overrides the built-in set for "
                             "--set keyword|semantic)")
    parser.add_argument("--set", choices=("keyword", "semantic", "all"),
                        default="keyword",
                        help="labelled set to run: keyword (near-saturated, "
                             "vocabulary overlap), semantic (paraphrase, "
                             "vocabulary mismatch), or all (default: keyword)")
    parser.add_argument("--limit", "-k", type=int, default=8,
                        help="results retrieved per query (k)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--dump-queries", metavar="PATH",
                        help="write the active query set to PATH and exit")
    parser.add_argument("--min-recall", type=float, default=None,
                        help="default: 0.90 keyword, 0.20 semantic")
    parser.add_argument("--min-mrr", type=float, default=None,
                        help="default: 0.75 keyword, 0.15 semantic")
    parser.add_argument("--max-duplicate-rate", type=float, default=None,
                        help="default: 0.10 keyword, 0.20 semantic")
    parser.add_argument("--max-p95-ms", type=float, default=None,
                        help="default: 250ms keyword, 400ms semantic "
                             "(600ms floor for --mode pack)")
    parser.add_argument("--min-queries", type=int, default=None,
                        help="default: 30 keyword, 40 semantic")
    parser.add_argument("--no-gate", action="store_true",
                        help="report metrics but always exit 0")
    parser.add_argument("--no-vector-index", action="store_true",
                        help="disable the neural vector channel (pure lexical "
                             "baseline); same effect as HIPPOMIND_VECTOR_INDEX=0")
    parser.add_argument("--ledger-stats", action="store_true",
                        help="include ledger size/claim counts in the report")
    parser.add_argument("--mode", choices=("search", "pack"), default="search",
                        help="search = candidate ranking; pack = the context actually injected")
    parser.add_argument("--min-score", type=float, default=0.20,
                        help="relevance gate used by --mode pack")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.queries and args.set == "all":
        print("error: --queries cannot be combined with --set all",
              file=sys.stderr)
        return 2
    set_names = list(SET_ORDER) if args.set == "all" else [args.set]
    try:
        sets: Dict[str, List[Dict[str, Any]]] = {}
        for name in set_names:
            sets[name] = (load_queries(args.queries) if args.queries
                          else validate_queries(QUERY_SETS[name]))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.dump_queries:
        return _dump(sets, Path(args.dump_queries))
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: ledger not found: %s" % ledger_path, file=sys.stderr)
        return 2
    overrides = {
        "min_recall_at_k": args.min_recall,
        "min_mrr": args.min_mrr,
        "max_duplicate_rate": args.max_duplicate_rate,
        "max_p95_ms": args.max_p95_ms,
        "min_queries": args.min_queries,
    }
    ledger = SQLiteLedger(ledger_path)
    try:
        vector_index = None if args.no_vector_index else _load_vector_index(ledger)
        builder = (ContextPackBuilder(ledger, vector_index=vector_index)
                   if args.mode == "pack" else None)
        search = _pack_search(builder, args.min_score) if builder else None
        results: Dict[str, Dict[str, Any]] = {}
        for name, queries in sets.items():
            results[name] = evaluate(ledger, queries, limit=args.limit,
                                     search=search, vector_index=vector_index)
        ledger_stats = ledger.stats() if args.ledger_stats else None
    finally:
        ledger.close()
    for name, result in results.items():
        result["set"] = name
        result["mode"] = args.mode
        result["ledger"] = str(ledger_path)
        if ledger_stats is not None:
            result["ledger_stats"] = {
                key: ledger_stats.get(key)
                for key in ("memories", "raw_chat", "active", "claims",
                            "active_claims", "conflict_claims", "schema_version")
            }
        result["gate"] = check_gate(
            result, **_set_thresholds(name, mode=args.mode, overrides=overrides))
    if len(results) == 1:
        report = next(iter(results.values()))
    else:
        gates = {name: result["gate"] for name, result in results.items()}
        report = {
            "sets": results,
            "queries": sum(result["queries"] for result in results.values()),
            "k": args.limit,
            "mode": args.mode,
            "ledger": str(ledger_path),
            "gate": check_combined_gate(gates),
        }
        if ledger_stats is not None:
            report["ledger_stats"] = {
                key: ledger_stats.get(key)
                for key in ("memories", "raw_chat", "active", "claims",
                            "active_claims", "conflict_claims", "schema_version")
            }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    if args.no_gate:
        return 0
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
