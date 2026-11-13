"""Shared play survives context resets and interrupted memory acknowledgements."""
import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from companion_v2.conversation.flow import ConversationPolicy
from companion_v2.conversation.reset import ConversationWindow, reset_window
from companion_v2.execution.models import encode
from companion_v2.foundation.outcome import Outcome
from companion_v2.games.native import provider as games
from companion_v2.memory import codec
from companion_v2.product import PRODUCT_VERSION
from tests.test_game_memory_ingestion import GameMemoryTests
from tests.test_game_native_web import request
from tests.proactive_support import ContactFixture
from tests.test_role_tool_loop import answered


class SharedBoardTests(GameMemoryTests):
    def create_port(self):
        super().create_port()
        self.game_port.plugin.native_v2_tictactoe_enabled = True
        self.game_port.plugin.native_v2_gomoku_enabled = True

    async def test_both_web_finishes_save_one_sourced_experience_without_a_model(self):
        host = await self.game_host()
        app = host.application
        for kind, size, engine, replies in (
                ("tictactoe", 3, games.TicTacToeGame, [(1, 0), (1, 1)]),
                ("gomoku", 5, games.GomokuGame, [(14, 14), (12, 14), (10, 14), (8, 14)])):
            context, _, _, game = await self.operate(host, f"game.{kind}.create", {"difficulty": "normal"}, kind)
            ticket = self.game_port.issue_web_ticket(game["game_id"])
            with patch.object(engine, "choose_bot_move", side_effect=replies):
                for column in range(size):
                    result = await self.game_port.web_action(ticket, {"action": "move", "row": 0, "column": column})
            self.assertEqual((result["game"]["status"], result["game"]["outcome"]), ("finished", "player"))
            self.assertEqual(result["game"]["memory"]["state"], "saved")
            view = app.memory.experiences.read(context, result["game"]["memory"]["view_id"]).unwrap()
            self.assertEqual(datetime.fromisoformat(view["occurred_at"]), self.clock.now())
            self.assertIn(f"共落子{size * 2 - 1}步", view["statements"][0]["text"])
            original = app.memory.writer.access.source(context, codec.source(view["statements"][0]["evidence"][0]["source"]))
            source_game = json.loads(original.payload.split("\n", 1)[1])["game"]
            self.assertEqual(source_game["moves"], result["game"]["moves"])
            self.assertEqual(source_game["owner_context"], json.loads(encode(context)))
            app.memory.game.advance_native()
            app.memory.game.advance(context).unwrap()
        self.assertEqual(len(app.memory.experiences.list(context).unwrap()["items"]), 2)
        self.assertEqual(self.called, [])

    async def test_closed_game_retry_reload_reset_and_enrichment_keep_one_result(self):
        host = await self.game_host()
        app = host.application
        context, _, _, game = await self.operate(host, games.CREATE, {"difficulty": "easy"}, "这局让我认真想想")
        ticket = self.game_port.issue_web_ticket(game["game_id"])
        with patch.object(app.memory.experiences, "save", side_effect=sqlite3.OperationalError("injected database lock")):
            result = await self.game_port.web_action(ticket, {"action": "close"})
        self.assertEqual(result["game"]["memory"]["state"], "failed")
        self.assertEqual(result["game"]["status"], "closed")
        self.assertEqual(len(app.memory.experiences.list(context).unwrap()["items"]), 0)
        # Restart both the ledger reader and the application/database, with no new QQ turn.
        await app.close()
        await self.native_registry.aclose()
        self.create_port()
        restarted = await self.start(self.host(runtime=self.open_runtime(), tool_port=self.native_registry,
            policy=ConversationPolicy(0, 0, 180, 0)), task_generation=1, model_generation=1)
        app = restarted.application
        await self.until(lambda: len(app.memory.experiences.list(context).unwrap()["items"]) == 1)
        views = app.memory.experiences.list(context).unwrap()["items"]
        self.assertEqual(len(views), 1)
        self.assertIn("中途结束、未分胜负", views[0]["title"])
        # Simulate DB committed but the game-file acknowledgement never reached disk.
        with self.game_port.store.lock:
            draft = copy.deepcopy(self.game_port.store.data)
            room = next(room for rooms in draft["scopes"].values() for room in rooms.values())
            ended = room["ended_at"]
            room["memory"] = {"state": "pending"}
            self.game_port.store.commit(draft)
        self.advance(app, 31)
        await self.until(lambda: all(room.get("memory", {}).get("state") == "saved"
            for rooms in self.game_port.store.data["scopes"].values() for room in rooms.values()))
        self.advance(app, 1900)
        app.memory.game.advance_native(enrich=True)
        view = app.memory.experiences.read(context, views[0]["view_id"]).unwrap()
        self.assertGreater(len(view["statements"]), 1)
        self.assertIn("认真想想", view["statements"][1]["text"])
        reset_window(app.runtime, ConversationWindow.from_scope(context.scope), reset_id="game-reset", at=self.clock.now())
        self.assertEqual(app.memory.experiences.read(context, view["view_id"]).unwrap()["revision"], 2)
        app.memory.query.begin(context, "after-reset")
        recalled = app.memory.query.query(context, "after-reset", query_intent="井字棋", kind="experiences").unwrap()
        self.assertTrue(recalled["items"])
        self.assertEqual(len(app.memory.experiences.list(context).unwrap()["items"]), 1)
        persisted = next(room for rooms in self.game_port.store.data["scopes"].values() for room in rooms.values())
        self.assertEqual(persisted["ended_at"], ended)
        self.assertEqual(self.called, [])

    async def test_invitation_is_only_an_offer_decline_blocks_today_and_direct_start_works(self):
        host = await self.game_host()
        context, _, _, game = await self.operate(host, games.CREATE, {"difficulty": "normal"}, "initial")
        provider = self.game_port
        draft = copy.deepcopy(provider.store.data)
        legacy = next(room for rooms in draft["scopes"].values() for room in rooms.values())
        legacy.pop("ownership_version")
        legacy.pop("owner_context")
        legacy.pop("memory")
        provider.store.commit(draft)
        self.assertEqual(provider.invitation_status(json.loads(encode(context)), "gomoku")["reason"], "active_game")
        _, _, _, adopted = await self.operate(host, games.CREATE, {"difficulty": "normal"}, "resume-legacy")
        self.assertEqual(adopted["game_id"], game["game_id"])
        self.assertEqual(adopted["ownership_version"], 2)
        raw = json.loads(encode(context))
        self.assertEqual(provider.invitation_status(raw, "gomoku")["reason"], "active_game")
        await provider.web_action(provider.issue_web_ticket(game["game_id"]), {"action": "close"})

        async def invite(kind, decision, identifier):
            call = request(provider, f"game.{kind}.invitation", {"decision": decision}, identifier)
            call["context"] = raw
            return await provider.execute(call, before_dispatch=lambda: True)

        offer = await invite("gomoku", "offer", "offer")
        self.assertTrue(offer["result"]["invitation"]["allowed"])
        self.assertEqual(len(provider.list_web_games()), 0, "An invitation cannot create a board before acceptance")
        self.assertEqual(provider.invitation_status(raw, "tictactoe")["reason"], "cooldown")
        await invite("gomoku", "decline", "decline")
        self.assertEqual(provider.invitation_status(raw, "tictactoe")["reason"], "declined_today")
        _, _, _, accepted = await self.operate(host, games.GOMOKU_CREATE, {"difficulty": "hard"}, "direct-request")
        self.assertEqual(accepted["status"], "active")
        _, _, _, resumed = await self.operate(host, games.GOMOKU_CREATE, {"difficulty": "normal"}, "link-again")
        self.assertEqual(accepted["game_id"], resumed["game_id"])
        self.assertEqual(resumed["state"]["difficulty"], "hard")

    async def test_proactive_candidate_obeys_probability_authorization_and_cooldown(self):
        host = await self.game_host()
        app, provider = host.application, self.game_port
        context, _, _, game = await self.operate(host, games.CREATE, {"difficulty": "normal"}, "setup")
        await provider.web_action(provider.issue_web_ticket(game["game_id"]), {"action": "close"})
        provider.plugin.invite_probability = 100
        chosen = app.proactive._game_invitation(context, "play", "opportunity", self.clock.now())
        self.assertIn(chosen, {"gomoku", "tictactoe"})
        provider.plugin.invite_probability = 0
        self.assertIsNone(app.proactive._game_invitation(context, "play", "other", self.clock.now()))
        provider.plugin.invite_probability = 100
        provider.confirm_invitation(json.loads(encode(context)), chosen, "delivered-contact", self.clock.now().timestamp())
        self.assertIsNone(app.proactive._game_invitation(context, "invitation", "other", self.clock.now()))
        self.advance(app, 86401)
        self.assertIsNotNone(app.proactive._game_invitation(context, "play", "next-day", self.clock.now()))
        provider.invitation_authorized = lambda *_: False
        self.assertIsNone(app.proactive._game_invitation(context, "play", "not-authorized", self.clock.now()))


class SharedInvitationDeliveryTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    async def game_contacts(self):
        app = await self.enabled()
        self.settings(app, {"play.enabled": True, "invitation.enabled": True}).unwrap()
        provider = games.NativeGameProvider(SimpleNamespace(native_v2_tictactoe_enabled=True,
            native_v2_gomoku_enabled=True, invite_probability=100), self.root.parent / "invitations", version=PRODUCT_VERSION)
        provider.store.now = lambda: self.clock.now().timestamp()
        provider.invitation_authorized = lambda *_: True
        self.addAsyncCleanup(provider.close)
        app.proactive.game_provider = provider
        return app, provider

    async def test_delivered_invite_sets_cooldown_but_never_creates_game(self):
        app, provider = await self.game_contacts()
        async def invite(envelope):
            self.assertIn("必须等明确答应", envelope.original.instruction)
            return answered('{"decision":"send","text":"要不要一起下一局棋？"}')
        self.model_behavior = invite
        opportunity = self.opportunity("board-invitation")
        task = (await self.contact(app, opportunity, (self.intent(opportunity, purpose="play"),))).unwrap().task
        row = await self.delivery_state(app, task.task_id, "delivered")
        app.proactive.delivery_confirmed(row["delivery_id"])
        status = provider.invitation_status(json.loads(encode(self.contact_context())), "gomoku")
        self.assertEqual(status["reason"], "cooldown")
        self.assertEqual(provider.list_web_games(), [])
        self.assertEqual(len(self.sent), 1)

    async def test_quiet_hours_recheck_blocks_game_invite_without_consuming_cooldown(self):
        app, provider = await self.game_contacts()
        self.settings(app, {"quiet_enabled": True, "quiet_timezone": "UTC", "quiet_start": "00:01", "quiet_end": "00:02"}).unwrap()
        async def invite(_):
            self.clock.advance(timedelta(minutes=1))
            return answered('{"decision":"send","text":"要不要下棋？"}')
        self.model_behavior = invite
        opportunity = self.opportunity("quiet-board-invitation")
        result = await self.contact(app, opportunity, (self.intent(opportunity, purpose="play"),))
        self.assertNotEqual(result.unwrap().task.state, "succeeded")
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.store.data.get("invitations", {}), {})

    async def test_decline_during_generation_blocks_queued_game_invite(self):
        app, provider = await self.game_contacts()
        async def invite(_):
            call = request(provider, games.INVITE, {"decision": "decline"}, "decline-before-send")
            call["context"] = json.loads(encode(self.contact_context()))
            await provider.execute(call, before_dispatch=lambda: True)
            return answered('{"decision":"send","text":"要不要下棋？"}')
        self.model_behavior = invite
        opportunity = self.opportunity("declined-board-invitation")
        task = (await self.contact(app, opportunity, (self.intent(opportunity, purpose="play"),))).unwrap().task
        self.assertNotEqual(task.state, "succeeded", "A declined invitation is rejected before it enters the outbox")
        self.assertIsNone(app.proactive._delivery_row(task.task_id))
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.list_web_games(), [])
