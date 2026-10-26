# -*- coding: utf-8 -*-
"""Codex 联动：纯映射（codex_state）+ 只读 sqlite 监视器（CodexMonitor）+ 装配。

数据源是 Codex 本地的 ``$CODEX_HOME/thread_history_1.sqlite``（``thread_turns`` /
``thread_items`` 两张表，Codex Desktop 写入）。监视器只读、不写任何外部文件，
测试里用临时目录自建同结构库（新旧两版表结构都覆盖）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

import pytest
from PySide6.QtWidgets import QApplication

from pet.agent_link import codex_state
from pet.agent_link.codex_state import aggregate, classify_item

NOW_S = 1_800_000_000
NOW_MS = NOW_S * 1000


# ---------------------------------------------------------------- 纯映射
class TestClassifyItem:
    @pytest.mark.parametrize(
        "item, expected",
        [
            (None, ("thinking", "")),
            ({"item_type": "userMessage"}, ("thinking", "")),
            ({"item_type": "reasoning"}, ("thinking", "")),
            ({"item_type": "imageView"}, ("thinking", "")),
            ({"item_type": "commandExecution"}, ("working", "shell")),
            ({"item_type": "commandExecution", "action_type": "unknown"}, ("working", "shell")),
            ({"item_type": "commandExecution", "action_type": "read"}, ("working", "read")),
            ({"item_type": "commandExecution", "action_type": "search"}, ("working", "grep")),
            ({"item_type": "commandExecution", "action_type": "listFiles"}, ("working", "grep")),
            ({"item_type": "fileChange"}, ("working", "edit")),
            ({"item_type": "webSearch"}, ("working", "websearch")),
            ({"item_type": "mcpToolCall", "server": "node_repl", "tool": "js"}, ("working", "js")),
            ({"item_type": "mcpToolCall", "server": "browser", "tool": "navigate"}, ("working", "browser")),
            ({"item_type": "mcpToolCall"}, ("working", "")),
            ({"item_type": "subAgentActivity"}, ("working", "task")),
            ({"item_type": "agentMessage"}, ("working", "")),
            ({"item_type": "contextCompaction"}, ("working", "")),
            ({"item_type": "somethingNew"}, ("working", "")),
        ],
    )
    def test_item_types(self, item, expected):
        assert classify_item(item) == expected

    @pytest.mark.parametrize("status", ["waitingForApproval", "pendingApproval", "waitingForInput"])
    def test_waiting_status_is_attention(self, status):
        item = {"item_type": "commandExecution", "status": status}
        assert classify_item(item) == ("attention", "")

    def test_unfinished_user_input_tool_is_attention(self):
        item = {"item_type": "mcpToolCall", "server": "codex", "tool": "request_user_input"}
        assert classify_item(item) == ("attention", "")
        done = dict(item, completed_at_ms=NOW_MS)
        assert classify_item(done)[0] == "working"

    def test_only_whitelisted_fields_reach_the_tool_name(self):
        """隐私口径：命令正文、参数、输出不进工具名（过程气泡只说"正在跑命令"）。"""
        item = {
            "item_type": "commandExecution",
            "command": "rm -rf C:/secret",
            "aggregated_output": "password=hunter2",
        }
        assert classify_item(item) == ("working", "shell")


def _turn(status, started_s, completed_s=None, item=None, turn_id="t1", thread_id="th1"):
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "status": status,
        "started_at": started_s,
        "completed_at": completed_s,
        "item": item,
    }


class TestAggregate:
    def test_no_turns_is_idle(self):
        assert aggregate([], NOW_MS) == ("idle", "")

    def test_in_progress_follows_latest_item(self):
        turns = [_turn("inProgress", NOW_S - 5,
                       item={"item_type": "commandExecution", "created_at_ms": NOW_MS - 1000})]
        assert aggregate(turns, NOW_MS) == ("working", "shell")

    def test_waiting_turn_has_priority_over_newer_busy_turn(self):
        turns = [
            _turn("inProgress", NOW_S - 30, turn_id="a",
                  item={"item_type": "commandExecution", "status": "waitingForApproval",
                        "created_at_ms": NOW_MS - 20_000}),
            _turn("inProgress", NOW_S - 5, turn_id="b", thread_id="th2",
                  item={"item_type": "reasoning", "created_at_ms": NOW_MS - 1000}),
        ]
        assert aggregate(turns, NOW_MS) == ("attention", "")

    def test_newest_activity_wins_between_busy_turns(self):
        turns = [
            _turn("inProgress", NOW_S - 30, turn_id="a",
                  item={"item_type": "reasoning", "created_at_ms": NOW_MS - 20_000}),
            _turn("inProgress", NOW_S - 60, turn_id="b", thread_id="th2",
                  item={"item_type": "fileChange", "created_at_ms": NOW_MS - 1000}),
        ]
        assert aggregate(turns, NOW_MS) == ("working", "edit")

    @pytest.mark.parametrize(
        "status, expected",
        [("completed", "idle"), ("failed", "error"), ("interrupted", "attention")],
    )
    def test_latest_finished_turn_decides_when_nothing_runs(self, status, expected):
        turns = [
            _turn("completed", NOW_S - 600, NOW_S - 500, turn_id="old"),
            _turn(status, NOW_S - 60, NOW_S - 10, turn_id="new"),
        ]
        assert aggregate(turns, NOW_MS) == (expected, "")

    def test_unknown_status_is_ignored(self):
        turns = [_turn("somethingElse", NOW_S - 5, NOW_S - 1)]
        assert aggregate(turns, NOW_MS) == ("idle", "")

    def test_stale_in_progress_turn_stops_counting_as_busy(self):
        """客户端崩溃会留下 inProgress 脏行：超时没进展就不再算忙，而是「停了，自己看一眼」。"""
        stale_ms = codex_state.STALE_AFTER_MS
        last = NOW_MS - stale_ms - 1000
        turns = [_turn("inProgress", last // 1000, item={"item_type": "commandExecution", "created_at_ms": last})]
        assert aggregate(turns, NOW_MS) == ("attention", "")
        fresh = [_turn("inProgress", last // 1000,
                       item={"item_type": "commandExecution", "created_at_ms": last,
                             "completed_at_ms": NOW_MS - 1000})]
        assert aggregate(fresh, NOW_MS) == ("working", "shell")

    def test_finished_turn_after_stale_turn_wins(self):
        stale_ms = codex_state.STALE_AFTER_MS
        last = NOW_MS - stale_ms * 3
        turns = [
            _turn("inProgress", last // 1000, turn_id="dead",
                  item={"item_type": "reasoning", "created_at_ms": last}),
            _turn("completed", NOW_S - 120, NOW_S - 60, turn_id="live", thread_id="th2"),
        ]
        assert aggregate(turns, NOW_MS) == ("idle", "")


def test_codex_state_is_pure_logic():
    src = codex_state.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "PySide6" not in text


# ---------------------------------------------------------------- sqlite 监视器
_TURNS_DDL = (
    "CREATE TABLE thread_turns (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
    "rollout_ordinal INTEGER NOT NULL, status TEXT NOT NULL, error_json TEXT, "
    "started_at INTEGER, completed_at INTEGER, duration_ms INTEGER, "
    "first_user_item_id TEXT, final_agent_item_id TEXT{extra}, "
    "PRIMARY KEY (thread_id, turn_id))"
)
_ITEMS_DDL = (
    "CREATE TABLE thread_items (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
    "item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, "
    "item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT ''{extra}, "
    "PRIMARY KEY (thread_id, turn_id, item_id))"
)


class _CodexDb:
    """同结构的临时 Codex 库；每次写操作单独连接、立刻关闭（与真实写者一样不占锁）。"""

    def __init__(self, path, *, new_schema=True):
        self.path = path
        self._ordinal = 0
        db = sqlite3.connect(path)
        if new_schema:
            db.execute(_TURNS_DDL.format(extra=", root_turn_id TEXT"))
            db.execute(_ITEMS_DDL.format(
                extra=", updated_at_ordinal INTEGER NOT NULL DEFAULT 0, "
                      "started_at_ms INTEGER, completed_at_ms INTEGER"))
        else:
            db.execute(_TURNS_DDL.format(extra=""))
            db.execute(_ITEMS_DDL.format(extra=""))
        db.commit()
        db.close()

    def _run(self, sql, args):
        db = sqlite3.connect(self.path)
        try:
            db.execute(sql, args)
            db.commit()
        finally:
            db.close()

    def turn(self, turn_id, status, started_s, completed_s=None, thread_id="th1"):
        self._ordinal += 1
        self._run(
            "INSERT OR REPLACE INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, "
            "started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, turn_id, self._ordinal, status, started_s, completed_s),
        )

    def item(self, turn_id, item_type, created_ms, thread_id="th1", **fields):
        import json

        self._ordinal += 1
        payload = {"type": item_type, **fields}
        self._run(
            "INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal, "
            "created_at_ms, item_json, item_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, turn_id, f"i{self._ordinal}", self._ordinal, created_ms,
             json.dumps(payload), item_type),
        )


class _Clock:
    def __init__(self, now_s=NOW_S):
        self.now = float(now_s)

    def __call__(self):
        return self.now


def _monitor(tmp_path, db_path, clock):
    from pet.agent_link import CodexMonitor

    QApplication.instance() or QApplication([])
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    mon = CodexMonitor(cfg_dir, db_path=db_path, clock=clock)
    states, tools = [], []
    mon.state_changed.connect(lambda _k, s: states.append(s))
    mon.activity.connect(lambda _k, t: tools.append(t))
    return mon, states, tools


class TestCodexMonitor:
    def test_baseline_does_not_replay_finished_history(self, tmp_path):
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        db.turn("old", "completed", NOW_S - 600, NOW_S - 500)
        db.turn("bad", "failed", NOW_S - 300, NOW_S - 200)
        db.turn("stop", "interrupted", NOW_S - 100, NOW_S - 90)
        db.turn("dead", "inProgress", NOW_S - 7200)
        mon, states, tools = _monitor(tmp_path, db.path, _Clock())
        mon._poll()
        mon._poll()
        assert states == [] and tools == []

    def test_turn_lifecycle(self, tmp_path):
        clock = _Clock()
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        mon, states, tools = _monitor(tmp_path, db.path, clock)
        mon._poll()  # 基线：空库
        db.turn("t1", "inProgress", NOW_S)
        db.item("t1", "userMessage", NOW_MS)
        clock.now += 2
        mon._poll()
        assert states == ["thinking"]
        db.item("t1", "commandExecution", NOW_MS + 1500, status="inProgress",
                commandActions=[{"type": "unknown"}])
        clock.now += 2
        mon._poll()
        assert states == ["thinking", "working"]
        assert tools == ["shell"]
        mon._poll()  # 没变化不重发
        assert states == ["thinking", "working"] and tools == ["shell"]
        db.item("t1", "fileChange", NOW_MS + 3000)
        mon._poll()
        assert states == ["thinking", "working"]  # 仍是 working，不重复切状态
        assert tools == ["shell", "edit"]
        db.turn("t1", "completed", NOW_S, NOW_S + 5)
        clock.now += 2
        mon._poll()
        assert states == ["thinking", "working", "idle"]

    def test_old_schema_without_item_timestamps_still_works(self, tmp_path):
        clock = _Clock()
        db = _CodexDb(tmp_path / "thread_history_1.sqlite", new_schema=False)
        mon, states, _tools = _monitor(tmp_path, db.path, clock)
        mon._poll()
        db.turn("t1", "inProgress", NOW_S)
        db.item("t1", "reasoning", NOW_MS)
        mon._poll()
        assert states == ["thinking"]

    def test_waiting_for_approval_then_failed(self, tmp_path):
        clock = _Clock()
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        mon, states, _tools = _monitor(tmp_path, db.path, clock)
        mon._poll()
        db.turn("t1", "inProgress", NOW_S)
        db.item("t1", "commandExecution", NOW_MS, status="waitingForApproval")
        mon._poll()
        assert states == ["attention"]
        db.turn("t1", "failed", NOW_S, NOW_S + 3)
        mon._poll()
        assert states == ["attention", "error"]

    def test_interrupted_turn_is_attention_not_done(self, tmp_path):
        clock = _Clock()
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        mon, states, _tools = _monitor(tmp_path, db.path, clock)
        mon._poll()
        db.turn("t1", "inProgress", NOW_S)
        db.item("t1", "reasoning", NOW_MS)
        mon._poll()
        db.turn("t1", "interrupted", NOW_S, NOW_S + 2)
        mon._poll()
        assert states == ["thinking", "attention"]

    def test_busy_at_baseline_is_reported(self, tmp_path):
        """任务进行中才开联动：基线不回放历史，但「正在忙」要立刻反映出来。"""
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        db.turn("t1", "inProgress", NOW_S - 3)
        db.item("t1", "commandExecution", NOW_MS - 1000, commandActions=[{"type": "read"}])
        mon, states, tools = _monitor(tmp_path, db.path, _Clock())
        mon._poll()
        assert states == ["working"]
        assert tools == ["read"]

    def test_stale_in_progress_turn_settles(self, tmp_path):
        clock = _Clock()
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        mon, states, _tools = _monitor(tmp_path, db.path, clock)
        mon._poll()
        db.turn("t1", "inProgress", NOW_S)
        db.item("t1", "reasoning", NOW_MS)
        mon._poll()
        clock.now += codex_state.STALE_AFTER_MS / 1000 + 60
        mon._poll()
        assert states == ["thinking", "attention"]

    def test_missing_database_is_silent(self, tmp_path):
        mon, states, _tools = _monitor(tmp_path, tmp_path / "nope.sqlite", _Clock())
        mon._poll()
        mon._poll()
        assert states == []

    def test_unknown_schema_degrades_once_without_raising(self, tmp_path, caplog):
        path = tmp_path / "thread_history_1.sqlite"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE thread_turns (thread_id TEXT, something_else TEXT)")
        db.commit()
        db.close()
        mon, states, _tools = _monitor(tmp_path, path, _Clock())
        with caplog.at_level(logging.INFO):
            for _ in range(5):
                mon._poll()
        assert states == []
        warnings = [r for r in caplog.records if "Codex" in r.getMessage() and "结构" in r.getMessage()]
        assert len(warnings) == 1

    def test_replaced_database_rebaselines(self, tmp_path):
        clock = _Clock()
        path = tmp_path / "thread_history_1.sqlite"
        db = _CodexDb(path)
        mon, states, _tools = _monitor(tmp_path, path, clock)
        mon._poll()
        replacement = _CodexDb(tmp_path / "new.sqlite")
        replacement.turn("x", "completed", NOW_S - 50, NOW_S - 40)
        os.replace(replacement.path, path)
        mon._poll()  # 新库：重新取基线，历史完成不回放
        assert states == []
        del db

    def test_never_writes_next_to_the_codex_database(self, tmp_path):
        codex_home = tmp_path / "codex_home"
        codex_home.mkdir()
        db = _CodexDb(codex_home / "thread_history_1.sqlite")
        db.turn("t1", "inProgress", NOW_S)
        before = sorted(p.name for p in codex_home.iterdir())
        mon, _states, _tools = _monitor(tmp_path, db.path, _Clock())
        mon._poll()
        mon._poll()
        assert sorted(p.name for p in codex_home.iterdir()) == before

    def test_default_path_honours_codex_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
        assert codex_state.default_db_path() == tmp_path / "custom" / "thread_history_1.sqlite"
        monkeypatch.delenv("CODEX_HOME")
        assert codex_state.default_db_path().name == "thread_history_1.sqlite"
        assert codex_state.default_db_path().parent.name == ".codex"

    def test_worker_thread_polls_and_stops(self, tmp_path):
        """真实 worker 线程轮询（不直调 _poll）：开始干活被感知，stop 后线程退出。"""
        import threading

        clock = _Clock(time.time())
        db = _CodexDb(tmp_path / "thread_history_1.sqlite")
        mon, states, _tools = _monitor(tmp_path, db.path, clock)
        seen = threading.Event()
        mon.state_changed.connect(lambda _k, s: seen.set())
        mon._POLL_INTERVAL_S = 0.05
        mon.start()
        try:
            deadline = time.monotonic() + 10
            while mon._baseline is None and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
            db.turn("t1", "inProgress", int(clock.now))
            db.item("t1", "reasoning", int(clock.now * 1000))
            while not seen.is_set() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
            assert "thinking" in states
        finally:
            mon.stop()
        assert mon._worker is None or not mon._worker.is_alive()


# ---------------------------------------------------------------- 装配
class TestCodexWiring:
    def test_config_default_and_cleaning(self, tmp_path):
        from pet.config import Config, _clean_agent_link_data

        cfg = Config(base=tmp_path)
        assert cfg.data["agent_link"]["codex"] is False
        assert _clean_agent_link_data({"codex": 1})["codex"] is True
        assert _clean_agent_link_data({"codex": 0})["codex"] is False

    def test_custom_agent_cannot_take_the_builtin_codex_key(self):
        from pet.config import _clean_custom_agents

        cleaned = _clean_custom_agents([
            {"key": "codex", "name": "My Codex", "path": "~/codex.jsonl"},
            {"key": "gemini", "name": "Gemini", "path": "~/g.jsonl"},
        ])
        assert [item["key"] for item in cleaned] == ["gemini"]

    def test_manager_builds_codex_monitor(self, tmp_path):
        from pet.agent_link import AgentLinkManager, CodexMonitor
        from pet.config import Config

        QApplication.instance() or QApplication([])
        cfg = Config(base=tmp_path)
        mgr = AgentLinkManager(None, cfg)
        try:
            assert isinstance(mgr.monitors["codex"], CodexMonitor)
            assert mgr.monitors["codex"].is_running() is False
            assert AgentLinkManager.AGENT_NAMES["codex"] == "Codex"
        finally:
            mgr.shutdown()

    def test_enabling_codex_needs_no_external_write(self, tmp_path, monkeypatch):
        """开启 Codex 联动不弹授权框、不写 Codex 的任何配置（只读数据源）。"""
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox

        from pet.agent_link import AgentLinkManager
        from pet.config import Config

        QApplication.instance() or QApplication([])
        fake_home = tmp_path / "home"
        (fake_home / ".codex").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.delenv("CODEX_HOME", raising=False)

        def _no_dialog(*_a, **_k):
            raise AssertionError("Codex 联动不应弹授权框")

        monkeypatch.setattr(QMessageBox, "question", _no_dialog)
        cfg = Config(base=tmp_path / "cfg")
        mgr = AgentLinkManager(None, cfg)
        try:
            assert mgr.set_enabled("codex", True) is True
            assert cfg.get("agent_link")["codex"] is True
            assert mgr.monitors["codex"].is_running() is True
            assert list((fake_home / ".codex").iterdir()) == []
            assert mgr.set_enabled("codex", False) is True
            assert mgr.monitors["codex"].is_running() is False
        finally:
            mgr.shutdown()

    def test_absent_codex_warns(self, tmp_path, monkeypatch):
        from pathlib import Path

        from pet.agent_link import AgentLinkManager
        from pet.config import Config

        QApplication.instance() or QApplication([])
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        bubbles = []

        class W:
            def show_bubble(self, text, **_k):
                bubbles.append(text)

        mgr = AgentLinkManager(W(), Config(base=tmp_path / "cfg"))
        try:
            mgr._warn_if_agent_absent("codex")
        finally:
            mgr.shutdown()
        assert any("Codex" in text for text in bubbles)

    def test_busy_codex_owns_its_process_for_proactive_dedup(self, tmp_path):
        from pet.agent_link import AgentLinkManager
        from pet.config import Config

        QApplication.instance() or QApplication([])
        cfg = Config(base=tmp_path)
        # 先建管理器再开开关：构造时 apply_config 不会真的起监视器线程去读本机 Codex 库
        mgr = AgentLinkManager(None, cfg)
        cfg.data["agent_link"]["codex"] = True
        try:
            mgr._last_raw["codex"] = "working"
            assert mgr.busy_agent_owns_process("Codex.exe") is True
            mgr._last_raw["codex"] = "idle"
            assert mgr.busy_agent_owns_process("Codex.exe") is False
        finally:
            cfg.data["agent_link"]["codex"] = False
            mgr.shutdown()

    def test_menu_lists_codex(self, tmp_path):
        from PySide6.QtWidgets import QMenu

        from pet.config import Config
        from pet.context_menus.shared import add_agent_link_menu

        QApplication.instance() or QApplication([])
        cfg = Config(base=tmp_path)
        toggles = []

        class DummyPet:
            def __init__(self):
                self.cfg = cfg

            def toggle_agent_link(self, key, on, action=None):
                toggles.append((key, on))

        menu = QMenu()
        try:
            add_agent_link_menu(menu, DummyPet())
            sub = menu.actions()[0].menu()
            codex = next(a for a in sub.actions() if a.text() == "Codex")
            codex.setChecked(True)
            assert toggles == [("codex", True)]
        finally:
            import shiboken6

            shiboken6.delete(menu)

    def test_codex_alone_wakes_the_agent_link_manager(self, monkeypatch):
        from pet import feature_activation
        from pet.window_optional_services import WindowFeatureGateMixin

        # 激活快照是进程级全局：钉成「未冻结」，不受其它用例残留的快照影响
        monkeypatch.setattr(feature_activation, "_snapshot", None)

        class Host(WindowFeatureGateMixin):
            def __init__(self, data):
                self.cfg = {"agent_link": data}

        assert Host({"codex": True})._agent_link_wanted() is True
        assert Host({"codex": False})._agent_link_wanted() is False

    def test_feature_spec_owns_the_codex_key(self):
        from pet.features import FEATURES

        spec = FEATURES["agent_link"]
        assert "agent_link.codex" in spec.config_keys
        assert "Codex" in spec.search_aliases
        assert "Codex" in spec.desc
