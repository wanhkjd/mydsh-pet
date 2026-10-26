# -*- coding: utf-8 -*-
"""Codex 联动的纯映射层（零 Qt，只吃 stdlib）。

数据源：Codex Desktop 写在 ``$CODEX_HOME/thread_history_1.sqlite``（默认
``~/.codex``）的两张表——``thread_turns``（每个回合一行，``status`` 为
``inProgress / completed / failed / interrupted``）与 ``thread_items``（回合内的
每一步，``item_type`` 为 ``userMessage / reasoning / commandExecution /
fileChange / mcpToolCall / agentMessage …``）。读库与线程归
``CodexMonitor``（``pet/agent_link/__init__.py``）；本模块只做**行 → 桌宠状态**
的换算，所以能脱离 Qt 和真实数据库直接跑真值表。

映射思路参考 Codex-dafeiyu（https://github.com/bianbian-666/Codex-dafeiyu ，MIT）
的 ``codex-activity.mjs``，按本项目的状态词汇重写。

隐私口径：只用 ``item_type``、条目状态、MCP ``server/tool`` 名与命令动作类型
（read / search …），**永不读取消息正文、命令内容、参数或输出**——过程气泡只说
「正在跑命令」，与其它 Agent 的脱敏口径一致。

这是 Codex 的**内部格式**，没有公开契约（文件名里的 ``_1`` 与表结构都随版本迁移
过）：读库一侧做结构探测，不认识就降级，这里不做任何格式假设之外的兜底。
"""
from __future__ import annotations

import os
from pathlib import Path

#: Codex 的本地状态库文件名（``thread_history`` 投影，版本号在文件名里）。
DB_FILENAME = "thread_history_1.sqlite"

#: 只看最近这段时间开始的回合（与参考实现一致：一天、最多 30 条）。
TURN_WINDOW_S = 24 * 3600
TURN_LIMIT = 30

#: ``inProgress`` 回合超过这么久没有任何进展就不再算忙。Codex 崩溃 / 被强杀会留下
#: 永远 ``inProgress`` 的脏行；参考实现取 10 分钟，这里放宽到 30 分钟，免得一条
#: 跑得久的构建命令被误判成「停了」。
STALE_AFTER_MS = 30 * 60 * 1000

#: 条目处于这些状态 = Codex 停下来等用户（审批 / 回答）。
WAITING_ITEM_STATUSES = frozenset({"waitingForApproval", "pendingApproval", "waitingForInput"})
_WAITING_TOOL_HINTS = ("request_user_input", "ask_user", "approval")

_THINKING_ITEMS = frozenset({"userMessage", "reasoning", "imageView"})
#: 命令动作类型 → 工具名（落到 AgentLinkManager.TOOL_LABELS 的既有文案）。
_COMMAND_ACTION_TOOLS = {"read": "read", "search": "grep", "listFiles": "grep"}
_ITEM_TOOLS = {"fileChange": "edit", "webSearch": "websearch", "subAgentActivity": "task"}

#: 回合结束态 → 桌宠状态。中断走 attention：它不是「干完了」，桌宠的收尾文案
#: 会是「那边停了，要主人自己看一眼」，而不是「干完活啦」。
_FINISHED_STATES = {"completed": "idle", "failed": "error", "interrupted": "attention"}


def default_db_path() -> Path:
    """``$CODEX_HOME/thread_history_1.sqlite``；未设置 ``CODEX_HOME`` 时取 ``~/.codex``。"""
    home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / DB_FILENAME


def _text(value) -> str:
    return str(value or "").strip()


def classify_item(item: dict | None) -> tuple[str, str]:
    """回合内最新一步 → ``(状态, 工具名)``；工具名为空表示不出过程气泡。

    ``item`` 是读库侧取出的白名单字段：``item_type`` / ``status`` / ``server`` /
    ``tool`` / ``action_type`` / ``completed_at_ms``。还没有任何条目（回合刚开始）
    按「思考中」算。
    """
    if not item:
        return ("thinking", "")
    item_type = _text(item.get("item_type"))
    status = _text(item.get("status"))
    server = _text(item.get("server")).lower()
    tool = _text(item.get("tool")).lower()
    if status in WAITING_ITEM_STATUSES:
        return ("attention", "")
    tool_path = f"{server}/{tool}"
    if any(hint in tool_path for hint in _WAITING_TOOL_HINTS) and not item.get("completed_at_ms"):
        return ("attention", "")
    if item_type in _THINKING_ITEMS:
        return ("thinking", "")
    if item_type == "commandExecution":
        action = _text(item.get("action_type"))
        return ("working", _COMMAND_ACTION_TOOLS.get(action, "shell"))
    if item_type in _ITEM_TOOLS:
        return ("working", _ITEM_TOOLS[item_type])
    if item_type == "mcpToolCall":
        return ("working", "browser" if "browser" in server else tool)
    # agentMessage（写回复）/ contextCompaction（整理上下文）/ 新类型：在忙，不报工具
    return ("working", "")


def _ms(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _last_activity_ms(turn: dict) -> int:
    item = turn.get("item") or {}
    return max(
        _ms(item.get("created_at_ms")),
        _ms(item.get("started_at_ms")),
        _ms(item.get("completed_at_ms")),
        _ms(turn.get("started_at")) * 1000,
    )


def _is_live(turn: dict, now_ms: int) -> bool:
    return _text(turn.get("status")) == "inProgress" and now_ms - _last_activity_ms(turn) < STALE_AFTER_MS


def has_live_turn(turns: list[dict], now_ms: int) -> bool:
    """此刻是否有真在跑的回合（超时的脏 ``inProgress`` 不算）。

    读库侧取基线时用它区分「正在忙」（要立刻报出来）与「历史结局」（不回放）。
    """
    return any(_is_live(turn, now_ms) for turn in turns)


def aggregate(turns: list[dict], now_ms: int) -> tuple[str, str]:
    """多个回合（可能分属多个会话）聚合成一个桌宠状态 ``(状态, 工具名)``。

    - 有进行中的回合：等用户的优先（attention），否则取最近有进展的那个回合；
    - 没有：取「最近结束」的那个回合的结局（completed → idle，failed → error，
      interrupted → attention）；超时没进展的 ``inProgress`` 也算一次结束
      （结局 attention），与真实结束的回合按时间先后比较；
    - 什么都没有 → idle。

    ``turns`` 每项：``status`` / ``started_at``（秒）/ ``completed_at``（秒）/
    ``item``（该回合最新一步的白名单字段，只有进行中的回合才有）。
    """
    busy: list[tuple[int, tuple[str, str]]] = []
    endings: list[tuple[int, str]] = []
    for turn in turns:
        status = _text(turn.get("status"))
        if status == "inProgress":
            last = _last_activity_ms(turn)
            if _is_live(turn, now_ms):
                busy.append((last, classify_item(turn.get("item"))))
            else:
                endings.append((last + STALE_AFTER_MS, "attention"))
        elif status in _FINISHED_STATES:
            endings.append((_ms(turn.get("completed_at")) * 1000, _FINISHED_STATES[status]))
    if busy:
        busy.sort(key=lambda entry: entry[0], reverse=True)
        for _last, snapshot in busy:
            if snapshot[0] == "attention":
                return snapshot
        return busy[0][1]
    if endings:
        endings.sort(key=lambda entry: entry[0])
        return (endings[-1][1], "")
    return ("idle", "")
