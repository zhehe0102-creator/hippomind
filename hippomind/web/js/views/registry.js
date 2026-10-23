/* ============================================================================
   HippoMind Atlas — view registry
   Loaded lazily: opening the console does not pay for views you never open.
   Metadata here is the single source of truth for navigation and the topbar.
   ========================================================================= */
"use strict";

export const VIEWS = [
  { id: "pulse", index: "01", title: "脉搏", eyebrow: "PULSE",
    subtitle: "账本的实时体征：规模、构成、增长与健康信号",
    load: () => import("./pulse.js") },
  { id: "archive", index: "02", title: "记忆库", eyebrow: "ARCHIVE",
    subtitle: "逐条翻阅账本中的结构化记忆，查看来源与谱系",
    load: () => import("./archive.js") },
  { id: "recall", index: "03", title: "检索", eyebrow: "RECALL",
    subtitle: "用关键词召回记忆：FTS 与 LIKE 双通道，按相关性排序",
    load: () => import("./recall.js") },
  { id: "compose", index: "04", title: "上下文", eyebrow: "COMPOSE",
    subtitle: "为当前任务组装最小、带证据、可注入的 Context Pack",
    load: () => import("./compose.js") },
  { id: "graph", index: "05", title: "图谱", eyebrow: "GRAPH",
    subtitle: "实体与记忆的关系网络：主语、claim 与因果连线",
    load: () => import("./graph.js") },
  { id: "facts", index: "06", title: "事实", eyebrow: "FACTS",
    subtitle: "claims、冲突谱系、事实投影与程序性技能",
    load: () => import("./facts.js") },
  { id: "timeline", index: "07", title: "年轮", eyebrow: "TIMELINE",
    subtitle: "记忆随时间的生长：按日/周堆叠，可下钻到具体时段",
    load: () => import("./timeline.js") },
  { id: "agents", index: "08", title: "端点", eyebrow: "AGENTS",
    subtitle: "来源 Agent、采集游标、接入发现与自动安装状态",
    load: () => import("./agents.js") },
  { id: "health", index: "09", title: "体检", eyebrow: "HEALTH",
    subtitle: "账本完整性、索引同步、容量与可执行的维护建议",
    load: () => import("./health.js") },
  { id: "reference", index: "10", title: "参考", eyebrow: "REFERENCE",
    subtitle: "HTTP 接口、界面设置与版本信息",
    load: () => import("./reference.js") },
];

export function viewById(id) {
  return VIEWS.find((view) => view.id === id) || VIEWS[0];
}
