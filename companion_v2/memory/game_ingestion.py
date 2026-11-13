"""Project confirmed Game terminal receipts into private, sourced experiences."""

import json
import copy
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from companion_v2.execution.models import TaskContext, TaskError, digest, encode, require, stamp
from companion_v2.execution.service import outcome
from companion_v2.foundation.refs import ScopeKind, SourceRef, VersionRef
from companion_v2.sources.models import SourceRecord
from companion_v2.foundation.outcome import Outcome
from companion_v2.conversation.native_tool_results import operation_rows

from .models import Evidence, owner_key
from . import codec

logger = logging.getLogger(__name__)


GAME_ACTIONS = {"game.tictactoe.move": ("tictactoe", "井字棋"),
                "game.gomoku.move": ("gomoku", "五子棋"),
                "game.pig_dice.roll": ("pig_dice", "掷骰竞分"),
                "game.pig_dice.hold": ("pig_dice", "掷骰竞分"),
                "game.blackjack.create": ("blackjack", "二十一点"),
                "game.blackjack.hit": ("blackjack", "二十一点"),
                "game.blackjack.stand": ("blackjack", "二十一点"),
                "game.turtle_soup.answer": ("turtle_soup", "海龟汤"),
                "game.turtle_soup.give_up": ("turtle_soup", "海龟汤"),
                "game.draw_guess.guess": ("draw_guess", "你画我猜"),
                "game.draw_guess.give_up": ("draw_guess", "你画我猜"),
                "game.xiangqi.create": ("xiangqi", "中国象棋"),
                "game.xiangqi.move": ("xiangqi", "中国象棋")}
CURSOR_PREFIX = "memory.game-ingestion.v1:"
TERMINAL_UNAVAILABLE = frozenset({"source_revoked", "source_expired", "source_dependency_unavailable",
    "source_dependency_expired", "memory_evidence_forgotten", "tool_owner_unavailable"})
OUTCOMES = {"player": "玩家获胜", "bot": "角色获胜", "draw": "平局"}
COOPERATIVE_OUTCOMES = {
    "turtle_soup": {"player": "玩家解开汤底", "bot": "玩家放弃"},
    "draw_guess": {"player": "合作猜中", "bot": "本轮未猜中"},
}
# The provider reports ``active | finished | closed``. Only a *finished* session carries
# a result, and only a result is material for an experience: a session that was started
# and later closed is remembered by the role's own continuity, not as a Game outcome, so
# this producer deliberately writes nothing for it. Completion, pausing and archiving are
# understood separately.
CLOSE_ACTIONS = frozenset({"game.tictactoe.close", "game.gomoku.close", "game.pig_dice.close",
                           "game.blackjack.close", "game.xiangqi.close", "game.turtle_soup.close",
                           "game.draw_guess.close"})


def terminal_result(game):
    """The finished result of one Game view, or None when it is not a finished session.

    ``active`` and ``closed`` both return None: neither has an outcome, and an abandoned
    session must not be recorded as if it had been played to an end.
    """
    if type(game) is not dict or game.get("status") != "finished":
        return None
    outcome = game.get("outcome")
    return outcome if type(outcome) is str and outcome in OUTCOMES else None


class MemoryGameIngestion:
    page_size = 16

    def __init__(self, memory, external, *, require_running=None):
        if external.tasks is not memory.tasks or external.messages is None:
            raise ValueError("game memory requires the attached original tool ledger")
        self.memory, self.external = memory, external
        self.tasks, self.runtime = memory.tasks, memory.runtime
        self.require_running = require_running
        self.closed = False
        self._last_advance = {}
        self._binding_offset = 0
        self.native_provider = None
        self._native_advanced_at = None

    def attach_native(self, provider):
        self.native_provider = provider
        provider.store.now = lambda: self.tasks.now().timestamp()
        provider.memory_sync = self.advance_native

    @outcome
    def record_board(self, room):
        """One immutable producer original and one experience per owner/game.

        This original is self-contained in the source repository, so rolling back
        the game extension never makes previously saved experiences unreadable.
        """
        self._active()
        context = TaskContext.restore(room["owner_context"])
        self.memory.writer.access.check(context, "memory.propose")
        identity = "board-experience-" + digest([owner_key(context), room["game_id"]])
        head = self.runtime.read_one("SELECT state FROM memory_views WHERE view_id = ?", (identity,))
        if head is not None:
            # A lost game-file acknowledgement must not replay an already committed
            # result, overwrite its enrichment, or resurrect an explicitly forgotten view.
            return {"state": "saved" if head["state"] == "active" else "forgotten", "view_id": identity}
        kind = room["game_type"]
        label = {"gomoku": "五子棋", "tictactoe": "井字棋"}[kind]
        ended = datetime.fromtimestamp(room["ended_at"], timezone.utc)
        local = ended.astimezone(timezone(timedelta(hours=8)))
        result = "中途结束、未分胜负" if room["status"] == "closed" else OUTCOMES[room["outcome"]]
        human = ("黑棋" if room.get("human_side", "black") == "black" else "白棋") if kind == "gomoku" else "X"
        bot = ({"黑棋": "白棋", "白棋": "黑棋"}[human]) if kind == "gomoku" else "O"
        difficulty = {"easy": "简单", "normal": "普通", "hard": "困难"}[room["difficulty"]]
        text = (f"{local:%Y年%m月%d日 %H:%M:%S}（UTC+8），用户与当前角色一起玩{label}，"
                f"用户执{human}、角色执{bot}，{difficulty}难度，共落子{len(room['moves'])}步。"
                f"结果：{result}。棋局编号：{room['game_id']}。")
        if room["moves"]:
            recent = room["moves"][-6:]
            steps = "；".join(f"第{len(room['moves']) - len(recent) + index + 1}步"
                f"{'用户' if stone == (1 if human in ('黑棋', 'X') else 2) else '角色'}落在({row+1},{column+1})"
                for index, (row, column, stone) in enumerate(recent))
            text += "最后的实际落子（行、列从1开始）：" + steps + "。"
        original = {key: room[key] for key in ("game_id", "game_type", "difficulty", "status", "outcome",
                    "moves", "created_at", "ended_at", "owner_context")}
        original["human_side"] = human
        payload = text + "\n" + encode({"schema": "memory.board-result.v2", "game": original})
        reference = SourceRef(identity, "memory.game_result", context.scope, VersionRef(identity, 1), ended)
        with self.runtime.transaction():
            require(self.memory.writer.sources.publish(SourceRecord(reference, context.audience,
                digest(payload), payload), observed_at=self.tasks.now()))
            evidence = Evidence(reference, digest(payload), 0, len(text), digest(text),
                context.role_binding.role.subject_id, "observation")
            return require(self.memory.experiences.save(context, view_id=identity, expected_revision=0,
                title=label + "：" + result,
                statements=({"text": text, "evidence": (evidence,), "atoms": ()},),
                entities=(label, "游戏:" + room["game_id"]), occurred_at=ended, operation_key=identity))

    @outcome
    def enrich_board(self, room):
        """Existing maintenance adds traceable QQ quotations after the core result.

        No model is needed to preserve originals. The existing dialogue reflection
        remains responsible for interpreting them; a failed summary cannot remove
        this result or turn guessed emotions into a shared experience.
        """
        from companion_v2.conversation.native_observations import rows_for_context, validate_observation
        self._active()
        context = TaskContext.restore(room["owner_context"])
        view = require(self.memory.experiences.read(context, room["memory"]["view_id"]))
        statements = tuple({"text": item["text"], "evidence": tuple(codec.evidence(e) for e in item["evidence"]),
                            "atoms": ()} for item in view["statements"])
        quotes = []
        start = stamp(datetime.fromtimestamp(room["created_at"] - 60, timezone.utc))
        end = stamp(datetime.fromtimestamp(room["ended_at"] + 120, timezone.utc))
        for row in reversed(rows_for_context(self.runtime, context, start=start, end=end, limit=8)):
            if TaskContext.restore(json.loads(row["context_json"])).scope != context.scope:
                continue
            reference = codec.source(json.loads(row["source_json"]))
            try:
                speaker, source_kind = validate_observation(self.runtime, row, context, reference)
                source = self.memory.writer.access.source(context, reference)
            except TaskError:
                continue  # Withdrawn/forgotten originals are never replaced by guesses.
            excerpt = source.payload[:240]
            if not excerpt.strip():
                continue
            who = "用户" if row["kind"] == "input" else "角色（记录到发送调用返回）"
            evidence = Evidence(reference, source.content_hash, 0, len(excerpt), digest(excerpt), speaker, source_kind)
            quotes.append({"text": f"对局时段的QQ原话，{who}：{excerpt}", "evidence": (evidence,), "atoms": ()})
        if not quotes or view["revision"] > 1:
            return {"state": "complete"}
        return require(self.memory.experiences.save(context, view_id=view["view_id"], expected_revision=1,
            title=view["title"], statements=statements + tuple(quotes), entities=tuple(view["entities"]),
            occurred_at=view["occurred_at"], operation_key=view["view_id"] + "-dialogue"))

    def advance_native(self, *, enrich=False):
        provider = self.native_provider
        report = {"saved": 0, "blocked": []}
        if provider is None or provider.closed:
            return report
        with provider.store.lock:
            draft = copy.deepcopy(provider.store.data)
            pending = [room for rooms in draft["scopes"].values() for room in rooms.values()
                       if room.get("ownership_version") == 2 and room["status"] != "active"
                       and (room["memory"]["state"] in {"pending", "failed"}
                            or enrich and room["memory"]["state"] == "saved" and not room["memory"].get("enriched"))]
            for room in pending[:self.page_size]:
                if room["memory"]["state"] in {"pending", "failed"}:
                    try:
                        result = self.record_board(room)
                    except (OSError, sqlite3.Error) as error:
                        result = Outcome.failed("game_memory_storage_error", type(error).__name__)
                    if result.is_success:
                        saved = result.unwrap()
                        room["memory"] = {"state": saved["state"], "view_id": saved["view_id"]}
                        report["saved"] += 1
                    else:
                        room["memory"] = {"state": "failed", "error_code": result.failure.code}
                        report["blocked"].append({"game_id": room["game_id"], "code": result.failure.code})
                        logger.warning("Board result memory pending retry: %s", result.failure.code)
                if (enrich and room["memory"]["state"] == "saved" and not room["memory"].get("enriched")
                        and self.tasks.now().timestamp() >= room["ended_at"] + 120):
                    try:
                        result = self.enrich_board(room)
                    except (OSError, sqlite3.Error) as error:
                        result = Outcome.failed("game_memory_storage_error", type(error).__name__)
                    if result.is_success:
                        room["memory"]["enriched"] = True
                    else:
                        report["blocked"].append({"game_id": room["game_id"], "code": result.failure.code})
                        logger.warning("Board dialogue enrichment pending retry: %s", result.failure.code)
            if draft != provider.store.data:
                provider.store.commit(draft)
        return report

    def _active(self):
        if self.closed:
            raise TaskError("memory_game_closed", "game memory producer is stopped")
        if self.require_running:
            self.require_running()
        require(self.tasks.check_generation())

    @staticmethod
    def _identity(context, invocation_id):
        return "game-experience-" + digest([owner_key(context), context.scope, invocation_id])

    @outcome
    def record(self, context, invocation_id):
        """Use only the settled child task and its governed native original."""
        with self.runtime.transaction():
            self._active()
            self.memory.writer.access.check(context, "memory.propose")
            row = self.external._row(invocation_id)
            if row["action"] not in GAME_ACTIONS or row["state"] != "succeeded":
                return {"state": "not_completed", "view_id": None}
            receipt, material = self.external.snapshot(row, context, self.tasks.now())
            if (receipt["state"] != "succeeded" or receipt["effect"] != "committed"
                    or receipt["operation_id"] is None or material is None or material.source is None):
                return {"state": "not_completed", "view_id": None}
            original = self.memory.writer.access.source(context, material.source, action="memory.propose")
            if original.payload != material.payload or material.source.owner != "game.native":
                raise TaskError("memory_game_source_changed", "game result differs from its original source")
            native = json.loads(original.payload).get("native")
            if (type(native) is not dict or native.get("operation_id") != receipt["operation_id"]
                    or native.get("receipt_ref") != receipt["receipt_ref"]
                    or type(native.get("result")) is not dict):
                raise TaskError("memory_game_receipt_changed", "game result differs from its confirmed receipt")
            result = native["result"]
            game = result.get("game") if set(result) == {"game"} else None
            if game and game.get("ownership_version") == 2:
                return {"state": "native_result_owner", "view_id": None}
            outcome_name = terminal_result(game)
            if outcome_name is None:
                return {"state": "not_completed", "view_id": None}
            game_id = game.get("game_id")
            game_type, game_label = GAME_ACTIONS[row["action"]]
            if (game.get("game_type") != game_type or type(game_id) is not str
                    or not 1 <= len(game_id) <= 128):
                raise TaskError("memory_game_receipt_changed", "game terminal result is invalid")
            label = COOPERATIVE_OUTCOMES.get(game_type, OUTCOMES).get(outcome_name)
            if label is None:
                raise TaskError("memory_game_receipt_changed", "game terminal result is invalid")
            statement = f"Game 执行端确认当前会话的{game_label}对局 {game_id} 已结束，结果为{label}。"
            evidence = Evidence(material.source, original.content_hash, 0, len(original.payload),
                digest(original.payload), context.role_binding.role.subject_id, "observation")
            identity = self._identity(context, invocation_id)
            return require(self.memory.experiences.save(context, view_id=identity, expected_revision=0,
                title=game_label + "终局：" + label,
                statements=({"text": statement, "evidence": (evidence,), "atoms": ()},),
                entities=(game_label, "游戏:" + game_id), occurred_at=material.source.issued_at,
                operation_key=identity))

    @outcome
    def advance(self, context):
        with self.runtime.transaction() as connection:
            self._active()
            self.memory.writer.access.check(context, "memory.propose")
            actions = tuple(GAME_ACTIONS)
            placeholders = ", ".join("?" for _ in actions)
            report = {"state": "ready", "examined": 0, "saved": 0, "skipped": 0,
                      "more": False, "blocked": []}
            for native in (False, True):
                key = CURSOR_PREFIX + ("native:" if native else "") + context.key
                saved = connection.execute("SELECT value FROM runtime_metadata WHERE key = ?", (key,)).fetchone()
                cursor = json.loads(saved["value"]) if saved else 0
                if type(cursor) is not int or cursor < 0:
                    raise TaskError("memory_game_cursor_invalid", "game receipt cursor is invalid")
                rows = operation_rows(self.runtime, context, actions, after=cursor, limit=self.page_size + 1) if native else (
                    connection.execute(f"""SELECT i.rowid AS scan_id, i.invocation_id, i.state
                        FROM extension_tool_invocations i JOIN conversation_batches b ON b.batch_id = i.batch_id
                        WHERE i.rowid > ? AND i.action IN ({placeholders}) AND b.origin_key = ?
                        ORDER BY i.rowid LIMIT ?""", (cursor, *actions, context.key, self.page_size + 1)).fetchall())
                report["more"] = report["more"] or len(rows) > self.page_size
                for row in rows[:self.page_size]:
                    report["examined"] += 1
                    if row["state"] in {"queued", "running"}:
                        report["state"], report["more"] = "blocked", True
                        report["blocked"].append({"invocation_id": row["invocation_id"], "code": "external_task_unresolved"})
                        break
                    if row["state"] == "succeeded":
                        result = self.record(context, row["invocation_id"])
                        if result.is_success:
                            report["saved" if result.unwrap()["view_id"] else "skipped"] += 1
                        elif result.failure.code in TERMINAL_UNAVAILABLE:
                            report["skipped"] += 1
                        else:
                            report["state"], report["more"] = "blocked", True
                            report["blocked"].append({"invocation_id": row["invocation_id"],
                                                      "code": result.failure.code})
                            break
                    else:
                        report["skipped"] += 1
                    cursor = row["scan_id"]
                connection.execute("""INSERT INTO runtime_metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (key, encode(cursor)))
            return report

    @outcome
    def advance_bound(self, bindings):
        self._active()
        windows = tuple(bindings)
        report = {"considered": 0, "saved": 0, "blocked": []}
        now = self.tasks.now()
        if self._native_advanced_at is None or (now - self._native_advanced_at).total_seconds() >= 30:
            self._native_advanced_at = now
            native = self.advance_native(enrich=True)
            report["saved"] += native["saved"]
            report["blocked"].extend(native["blocked"])
        if not windows:
            self._last_advance.clear()
            self._binding_offset = 0
            return report
        keys = {window[0].key for window in windows}
        self._last_advance = {key: at for key, at in self._last_advance.items() if key in keys}
        now = self.tasks.now()
        start, count = self._binding_offset % len(windows), min(16, len(windows))
        self._binding_offset = (start + count) % len(windows)
        for index in range(count):
            context = windows[(start + index) % len(windows)][0]
            report["considered"] += 1
            if context.scope.kind is not ScopeKind.PRIVATE:
                continue
            previous = self._last_advance.get(context.key)
            if previous is not None and 0 <= (now - previous).total_seconds() < 30:
                continue
            self._last_advance[context.key] = now
            result = self.advance(context)
            if result.is_success:
                report["saved"] += result.unwrap()["saved"]
                report["blocked"].extend(result.unwrap()["blocked"])
            else:
                self._active()
                report["blocked"].append({"context_key": context.key, "code": result.failure.code})
        return report

    def close(self):
        self.closed = True
        self._last_advance.clear()
        self._binding_offset = 0
