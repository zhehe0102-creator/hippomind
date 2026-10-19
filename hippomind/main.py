"""hippomind 入口。

用法：
    # 图形界面（记忆网络可视化 + 控制面板）
    python -m hippomind.main --mode gui

    # 全屏层状海马网络（参考 Wazone Memory Network）
    python -m hippomind.main --mode network --shared

    # 无界面训练 N 步并打印学习曲线（可自动化测试）
    python -m hippomind.main --mode headless --steps 5000

    # 接入 LLM（给大模型接上海马体，需 OPENAI_API_KEY）
    python -m hippomind.main --llm --task "帮我想个周末活动"

    # 记忆持久化（默认自动保存/读取 hippocampus_memory.json）
    python -m hippomind.main --mode gui --memory my_mem.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque

from .mind import Mind
from .grid_world import GridWorld
from .agent import MemoryAgent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="hippomind · 给 Agent 搭建的海马体")
    p.add_argument("--mode", choices=["gui", "network", "headless"], default="gui")
    p.add_argument("--steps", type=int, default=5000, help="headless 模式步数")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--memory", default="hippocampus_memory.json",
                   help="记忆库 JSON 路径")
    p.add_argument("--no-persist", action="store_true", help="结束后不保存")
    p.add_argument("--epsilon", type=float, default=0.15, help="Agent 探索率")
    p.add_argument("--decay-episodic", type=float, default=None,
                   help="情景记忆遗忘速率（降权速度）")
    p.add_argument("--recall-boost", type=float, default=None,
                   help="回忆升权量")
    p.add_argument("--llm", action="store_true", help="使用 LLM Agent（需 API Key）")
    p.add_argument("--task", default="帮我想个周末去哪玩", help="LLM 模式的任务")
    p.add_argument("--shared", action="store_true",
                   help="连接全电脑 Agent 聊天共通库（推荐用于 LLM 模式）")
    p.add_argument("--allow-raw-chat-context", action="store_true",
                   help="LLM 模式也发送原始跨 Agent 对话（默认只发送本地记忆和共享规则）")
    p.add_argument("--archive", help="Agent-Chat-Archive 根目录")
    p.add_argument("--warmup", type=int, default=600,
                   help="network 模式在空记忆库上的演示预热步数")
    p.add_argument("--fullscreen", action="store_true",
                   help="network 模式启动后切换全屏")
    p.add_argument("--topology-refresh-frames", type=int, default=900,
                   help="network GUI 全量拓扑重建间隔（默认 900 帧≈30 秒；降低会增加内存）")
    p.add_argument("--one-click", action="store_true",
                   help="一键启动全屏记忆网络，并初始化可用的共享语义账本")
    p.add_argument("--persist-legacy-json", action="store_true",
                   help="仅兼容旧演示：允许 --one-click 读取和写回 hippocampus_memory.json")
    p.add_argument("--hub", action="store_true", help="在按需 GUI 中显示本机 Memory Hub 状态")
    p.add_argument("--hub-port", type=int, help="Memory Hub localhost 端口")
    p.add_argument("--hub-runtime-dir", help="Memory Hub 状态和 token 目录")
    return p


ACTIVE_FPS = 30.0
IDLE_FPS = 2.0
IDLE_AFTER_SECONDS = 30.0


def _attach_idle_tracking(fig) -> dict:
    """记录最后一次用户交互时间，用于空闲降帧省电。"""
    state = {"last": time.monotonic()}

    def _mark(event):
        state["last"] = time.monotonic()

    for name in ("motion_notify_event", "button_press_event",
                 "key_press_event", "scroll_event"):
        fig.canvas.mpl_connect(name, _mark)
    return state


def _frame_pause(idle: dict) -> None:
    import matplotlib.pyplot as plt
    fps = ACTIVE_FPS if time.monotonic() - idle["last"] < IDLE_AFTER_SECONDS else IDLE_FPS
    plt.pause(1 / fps)


def make_hippo(args) -> Mind:
    cfg = {}
    if args.decay_episodic is not None:
        cfg["hippo_decay_episodic"] = args.decay_episodic
    if args.recall_boost is not None:
        cfg["hippo_recall_boost"] = args.recall_boost
    hippo = Mind(seed=args.seed, **cfg)
    if args.shared:
        try:
            commons = hippo.attach_commons(args.archive)
            if args.one_click:
                commons.initialize()
        except (FileNotFoundError, OSError) as exc:
            # A local visualizer remains useful without the optional archive.
            print(f"[警告] 未连接全机共通库：{exc}")
    if not args.no_persist and os.path.exists(args.memory):
        try:
            hippo.load(args.memory)
            print(f"[载入] 从 {args.memory} 恢复 {len(hippo.episodic)} 条情景、"
                  f"{len(hippo.semantic)} 条知识")
        except Exception as e:
            print(f"[警告] 读取记忆库失败：{e}")
    return hippo


def run_gui(args, hippo: Mind) -> None:
    import matplotlib.pyplot as plt
    from .visualizer import Visualizer

    env = GridWorld(seed=args.seed)
    agent = MemoryAgent(env, hippo, epsilon=args.epsilon, seed=args.seed)
    vis = Visualizer(hippo, env, agent)

    print("=" * 64)
    print("  hippomind · 给 Agent 搭建的海马体")
    print("  记忆会随时间自动降权（遗忘曲线），被回忆/用得好自动升权。")
    print("  按 Space 暂停/继续学习；M 手动回忆看联想扩散。")
    print("=" * 64)

    frame_dt = 0.05  # 每帧 = 0.05s 记忆时钟
    idle = _attach_idle_tracking(vis.fig)
    while not vis.closing:
        if vis.learning_on:
            agent.step()
        else:
            hippo.tick(dt=frame_dt)
        vis.frame()
        _frame_pause(idle)  # 交互时 30FPS，空闲 30s 后降到 2FPS 省 CPU

    if not args.no_persist:
        hippo.save(args.memory)
        print(f"[保存] 记忆库 → {args.memory}")
    plt.close("all")


def run_headless(args, hippo: Mind) -> None:
    env = GridWorld(seed=args.seed)
    agent = MemoryAgent(env, hippo, epsilon=args.epsilon, seed=args.seed)

    returns = deque(maxlen=500)
    total_return = 0.0
    print(f"{'step':>7} {'food':>6} {'poison':>7} {'avgR-500':>9} {'episodic':>9} "
          f"{'archive':>8} {'semantic':>9} {'assoc':>7} {'pruned':>7}")
    for i in range(1, args.steps + 1):
        reward = agent.step()
        returns.append(reward)
        total_return += reward
        if i % 500 == 0:
            s = hippo.stats_summary()
            print(f"{i:>7} {env.event_counts['food']:>6} {env.event_counts['poison']:>7} "
                  f"{sum(returns) / len(returns):>+9.3f} {s['episodic']:>9} "
                  f"{s['archived']:>8} {s['semantic']:>9} {s['associations']:>7} "
                  f"{s['pruned']:>7}")

    # 对照实验：没有海马体的随机策略
    random_env = GridWorld(seed=args.seed)
    import random as _random
    baseline_rng = _random.Random((args.seed or 0) + 100_003)
    total = 0.0
    for _ in range(args.steps):
        r = random_env.step(baseline_rng.choice(random_env.ACTIONS))
        total += r
    baseline = total / args.steps

    s = hippo.stats_summary()
    print("-" * 64)
    print(f"随机策略：觅食 {random_env.event_counts['food']} / 毒药 "
          f"{random_env.event_counts['poison']} / 撞墙 {random_env.event_counts['wall']}，"
          f"平均每步 {baseline:+.4f}")
    print(f"记忆Agent：觅食 {env.event_counts['food']} / 毒药 "
          f"{env.event_counts['poison']} / 撞墙 {env.event_counts['wall']}，"
          f"平均每步 {total_return / args.steps:+.4f}")
    print(f"记忆库：{s['episodic']} 活跃情景 / {s['archived']} 冷归档 / "
          f"{s['semantic']} 知识 / {s['associations']} 联想，"
          f"剪枝 {s['pruned']}，巩固 {s['consolidated']}，梦境 {s.get('dreams', 0)}")
    if not args.no_persist:
        hippo.save(args.memory)
        print(f"[保存] 记忆库 → {args.memory}")


def run_network(args, hippo: Mind) -> None:
    import matplotlib.pyplot as plt
    from .network_visualizer import LayeredMemoryVisualizer

    env = GridWorld(seed=args.seed)
    agent = MemoryAgent(env, hippo, epsilon=args.epsilon, seed=args.seed)
    if not hippo.all_memories() and args.warmup > 0:
        for _ in range(args.warmup):
            agent.step()
    hub_client = None
    if args.hub:
        from .hub_client import HubStatusClient
        hub_client = HubStatusClient(runtime_dir=args.hub_runtime_dir or "", port=args.hub_port)
    vis = LayeredMemoryVisualizer(
        hippo, env, agent,
        topology_refresh_frames=args.topology_refresh_frames,
        hub_client=hub_client,
    )
    if args.fullscreen:
        try:
            vis.fig.canvas.manager.full_screen_toggle()
        except AttributeError:
            pass

    print("=" * 64)
    print("  hippomind · 全屏层状海马网络")
    print("  节点和连接来自真实驱力、记忆域、经历、巩固、归档与共通库。")
    print("=" * 64)
    idle = _attach_idle_tracking(vis.fig)
    while not vis.closing:
        if vis.learning_on:
            agent.step()
        else:
            hippo.tick(dt=0.05)
        vis.frame()
        _frame_pause(idle)  # 交互时 30FPS，空闲 30s 后降到 2FPS 省 CPU

    if not args.no_persist:
        hippo.save(args.memory)
        print(f"[保存] 记忆库 → {args.memory}")
    plt.close("all")


def run_llm(args, hippo: Mind) -> None:
    from .llm_agent import LLMHippoAgent

    lh = LLMHippoAgent(
        hippo, include_raw_chat_memories=args.allow_raw_chat_context)
    if not lh.available:
        print("未设置 OPENAI_API_KEY。LLM 模式需要 API Key：")
        print("  export OPENAI_API_KEY=sk-...   # 或任意 OpenAI 兼容服务")
        print("  export OPENAI_BASE_URL=https://api.openai.com/v1")
        print("  export OPENAI_MODEL=gpt-4o-mini")
        sys.exit(1)
    print(f"海马体已载入 {len(hippo.episodic)} 条经历、{len(hippo.semantic)} 条知识。")
    if hippo.commons is not None:
        common_stats = hippo.commons.stats()
        print(f"共通库已连接 {common_stats['messages']} 条消息 / "
              f"{len(common_stats['agents'])} 类 Agent / "
              f"{common_stats['shared_memories']} 条共享规则。")
    reply = lh.run_task(args.task)
    print("-" * 64)
    print(reply)
    if not args.no_persist:
        hippo.save(args.memory)
        print(f"[保存] 记忆库 → {args.memory}")


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.one_click:
        if args.llm:
            parser.error("--one-click 只启动可视化网络；LLM 请单独运行")
        args.mode = "network"
        args.shared = True
        args.fullscreen = True
        args.hub = True
        # The Hub's daemon ledger is now the long-lived store. Never make a
        # double-click visual inspection rewrite a user's old JSON snapshot.
        if not args.persist_legacy_json:
            args.no_persist = True
    hippo = make_hippo(args)
    if args.llm:
        run_llm(args, hippo)
    elif args.mode == "network":
        run_network(args, hippo)
    elif args.mode == "gui":
        run_gui(args, hippo)
    else:
        run_headless(args, hippo)


if __name__ == "__main__":
    main()
