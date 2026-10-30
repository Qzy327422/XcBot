"""从 main AST 提取 normal 真实入口，全部使用 fake，不导入 main 或连接 QQ。"""
import ast
import asyncio
import copy
import inspect
import json
import pathlib
import random
import re
import sys
import threading
import time
import types
import unittest
import urllib.parse
from collections import OrderedDict
from unittest.mock import AsyncMock, Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.message_runtime import CURRENT_REQUEST, MessageIndex, ReplyDecision, ReplySuppressed, RequestState, bind_request

TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))


class Text:
    def __init__(self, text):
        self.text = text

    def to_json(self):
        return {"type": "text", "data": {"text": self.text}}


class Reply:
    def __init__(self, message_id):
        self.id = str(message_id)

    def to_json(self):
        return {"type": "reply", "data": {"id": self.id}}


class Message:
    def __init__(self, *contents):
        self.contents = list(contents)


class Event:
    def __init__(self, self_id=1, user_id=2, group_id=3, message_id=10):
        self.self_id, self.user_id = self_id, user_id
        self.group_id, self.message_id = group_id, message_id
        self.message, self.sender = [Text("你好")], {"nickname": "群友"}


class GroupMessageEvent(Event):
    pass


class PrivateMessageEvent(Event):
    def __init__(self, **kwargs):
        super().__init__(group_id=None, **kwargs)


class NotifyEvent(Event):
    notice_type = "notify"
    sub_type = "poke"
    target_id = 1


class PrivateNotifyEvent(NotifyEvent):
    def __init__(self):
        super().__init__(group_id=None)


class GroupMemberIncreaseEvent(Event):
    notice_type = "group_increase"


class HyperListenerStartNotify:
    self_id = 1


def extract(names, env):
    nodes = [copy.deepcopy(node) for node in TREE.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
    return env


def environment():
    switches = {}
    others = {"weak_blacklist_users": ["2"], "weak_blacklist_trigger_probability": 0.3}
    blacklist = set()
    env = {
        "asyncio": asyncio, "time": time, "random": random, "json": json, "re": re,
        "urllib": urllib, "OrderedDict": OrderedDict, "threading": threading,
        "CURRENT_REQUEST": CURRENT_REQUEST, "ReplyDecision": ReplyDecision,
        "ReplySuppressed": ReplySuppressed, "RequestState": RequestState, "bind_request": bind_request,
        "INDEX": MessageIndex(), "Events": types.SimpleNamespace(Event=Event, GroupMessageEvent=GroupMessageEvent,
            PrivateMessageEvent=PrivateMessageEvent, NotifyEvent=NotifyEvent,
            GroupMemberIncreaseEvent=GroupMemberIncreaseEvent, HyperListenerStartNotify=HyperListenerStartNotify),
        "Listener": types.SimpleNamespace(Actions=object),
        "Segments": types.SimpleNamespace(Text=Text, Reply=Reply), "Manager": types.SimpleNamespace(Message=Message),
        "get_runtime_others": lambda: others, "get_all_blacklist": lambda: blacklist,
        "is_feature_enabled": lambda key, default=True: switches.get(key, default),
        "log_console": Mock(), "_message_preview": lambda *args: "preview",
        "log_receive_group": Mock(), "log_receive_private": Mock(),
        "_qq_actions_lock": threading.RLock(), "_current_qq_actions": None,
        "_current_bot_self_id": None, "_agent_startup_tasks_sync": Mock(),
        "normalize_probability_config": lambda value, default=0.3: float(value) / 100 if float(value) > 1 else float(value),
        "_short_text": lambda value, limit: str(value)[:limit],
    }
    extract({"is_user_blacklisted", "_hard_blacklisted_author", "_incoming_message_blocked", "_memory_author_visible",
             "_receipt_succeeded", "_extract_send_message_id", "_message_facts", "_event_session",
             "_record_incoming_event", "_resolve_incoming_quote", "_ai_reply_allowed", "_normal_ai_state",
             "_chat_response_session", "_ensure_chat_response_allowed", "_maintain_event_runtime",
             "_build_normal_message", "_consume_social_reply", "LoggedActions", "_agent_actions", "send_error_detail", "handler"}, env)
    return env, switches, others, blacklist


def state_for(event=None, *, sample=0.1, random_fn=random.random, source="normal", actions=None):
    event = event or GroupMessageEvent()
    key = str(event.self_id), bool(event.group_id), str(event.group_id or event.user_id)
    decision = ReplyDecision(*key, actor_id=str(event.user_id), sample=sample,
                             random_fn=random_fn, source=source)
    return RequestState(*key, decision, event=event, actions=actions)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_gate_real_handler_blocks_all_routes_before_quote(self):
        from bot import proactive_reply
        for factory, kind in ((GroupMessageEvent, "group"), (PrivateMessageEvent, "private"),
                              (NotifyEvent, "group"), (PrivateNotifyEvent, "private"),
                              (GroupMemberIncreaseEvent, "group")):
            for text in ("/帮助", "/插件视角", "/model", "/reset", "你好"):
                with self.subTest(event=factory.__name__, text=text):
                    env, switches, _, _ = environment()
                    switches[kind + "_chat"] = False
                    extract({"_handler_inner"}, env)
                    quote, plugin, model = AsyncMock(), AsyncMock(), AsyncMock()
                    env.update(_resolve_incoming_quote=quote, execute_plugins=plugin,
                               handle_private_message=model)
                    event = factory()
                    event.message = [Reply(42), Text(text)]
                    ticket = object()
                    with patch.object(proactive_reply, "notify_user_message_received", return_value=ticket), patch.object(proactive_reply, "complete_normal_reply") as complete:
                        await env["handler"](event, object())
                    quote.assert_not_awaited()
                    plugin.assert_not_awaited()
                    model.assert_not_awaited()
                    env["_agent_startup_tasks_sync"].assert_called_once()
                    self.assertIsInstance(env["_current_qq_actions"], env["LoggedActions"])
                    self.assertEqual(env["_current_bot_self_id"], 1)
                    state = event._normal_request_state
                    self.assertTrue(state.handled)
                    if isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
                        complete.assert_called_once_with(state.is_group, state.chat_id,
                            normal_handled=True, message_id="10", ticket=ticket, reply_sent=False)
                        self.assertIsNotNone(env["INDEX"].resolve(*state.key, 10))
                        env["log_receive_group" if state.is_group else "log_receive_private"].assert_called_once()
                    self.assertIsNone(CURRENT_REQUEST.get())

    async def test_live_close_blocks_messages_and_every_forward_but_not_independent_send(self):
        for factory, kind in ((GroupMessageEvent, "group"), (PrivateMessageEvent, "private")):
            env, switches, _, _ = environment()
            raw = types.SimpleNamespace(**{name: AsyncMock(return_value={"status": "ok", "retcode": 0})
                for name in ("send", "send_group_forward_msg", "send_private_forward_msg", "send_forward_msg", "del_message")})
            actions = env["LoggedActions"](raw, self_id=1)
            state = state_for(factory(), actions=actions)
            with bind_request(state):
                await actions.send(message=Message(Text("first")), group_id=3)
                switches[kind + "_chat"] = False
                for name, kwargs in (("send", {"group_id": 3}), ("send", {"user_id": 999}),
                    ("send_group_forward_msg", {"group_id": 3}),
                    ("send_private_forward_msg", {"user_id": 2}), ("send_forward_msg", {})):
                    with self.assertRaises(ReplySuppressed):
                        await getattr(actions, name)(message=Message(Text("blocked")), **kwargs)
                await env["send_error_detail"](actions, state.event, ReplySuppressed(), state.is_group)
                await actions.del_message(42)
            self.assertEqual(raw.send.await_count, 1)
            for name in ("send_group_forward_msg", "send_private_forward_msg", "send_forward_msg"):
                getattr(raw, name).assert_not_awaited()
            # HTTP/定时任务独立上下文发送，即使目标与关闭的 QQ 会话相同也不拦。
            await actions.send(message=Message(Text("HTTP/timer")), group_id=3)
            await actions.send_group_forward_msg(group_id=3, message=Message())
            self.assertEqual(raw.send.await_count, 2)
            raw.del_message.assert_awaited_once()

    async def test_hot_close_during_quote_and_plugin_prevents_next_work(self):
        from bot import proactive_reply
        env, switches, _, _ = environment()
        inner = AsyncMock()
        async def resolve(*args):
            switches["group_chat"] = False
        env.update(_resolve_incoming_quote=resolve, _handler_inner=inner)
        with patch.object(proactive_reply, "notify_user_message_received"), patch.object(proactive_reply, "complete_normal_reply"):
            await env["handler"](GroupMessageEvent(), object())
        inner.assert_not_awaited()
        extract({"execute_plugins"}, env)
        for is_any in (True, False):
            switches["group_chat"] = False
            plugin = types.SimpleNamespace(on_message=AsyncMock())
            env.update(plugins=[plugin])
            with bind_request(state_for()), self.assertRaises(ReplySuppressed):
                await env["execute_plugins"](is_any, is_group=True, order="天气")
            plugin.on_message.assert_not_awaited()
        switches["group_chat"] = True
        async def close_after_await():
            switches["group_chat"] = False
            return False
        first = types.SimpleNamespace(PLUGIN_NAME="first", __name__="first", TRIGGHT_KEYWORD="Any", on_message=close_after_await)
        second = types.SimpleNamespace(PLUGIN_NAME="second", __name__="second", TRIGGHT_KEYWORD="Any", on_message=AsyncMock())
        env.update(plugins=[first, second], inspect=inspect, reminder="/",
            _make_plugin_logger=lambda name: lambda *args: None,
            get_plugin_config=lambda *args: None)
        with bind_request(state_for()):
            self.assertTrue(await env["execute_plugins"](True, is_group=True))
        second.on_message.assert_not_awaited()

    async def test_lifecycle_maintenance_and_recall_survive_disabled_chat(self):
        env, switches, _, _ = environment()
        switches.update(group_chat=False, private_chat=False)
        extract({"_handler_inner"}, env)
        for name in ("GroupAddInviteEvent",):
            setattr(env["Events"], name, type(name, (), {}))
        connected = Mock()
        env.update(settings_loaded=True, ROOT_User=[], build_plugin_base_context=lambda *args: {},
            execute_plugins=AsyncMock(return_value=False), HOT_SWITCH_IN_PROGRESS=threading.Event(),
            set_connection_status=connected, load_restart_state=lambda: None)
        await env["handler"](HyperListenerStartNotify(), object())
        connected.assert_called_once_with("connected", "已连接", "OneBot / Hyper 已建立连接")
        event = Event()
        event.notice_type = "group_recall"
        env["INDEX"].record("1", True, "3", 10, "2", text="history")
        await env["handler"](event, object())
        self.assertTrue(env["INDEX"].resolve("1", True, "3", 10)["recalled"])
        env["_agent_startup_tasks_sync"].assert_called_once()
        self.assertFalse(event._normal_request_state._qq_response_scoped)

    async def test_private_real_ai_paths_ignore_old_ai_flag_and_hot_close_generation(self):
        for text in ("/你好", "你好"):
            env, switches, _, _ = environment()
            switches.update(ai_chat=False, weak_blacklist=False)
            extract({"handle_private_message", "prefixed_command_text", "match_command", "match_command_prefix"}, env)
            context = types.SimpleNamespace(get_message_count=lambda: 0, _enforce_message_limit=lambda: None,
                agen_content=AsyncMock(return_value=("reply", 0, 0, 0)))
            env.update(ROOT_User=[], reminder="/", filter_sensitive_content=lambda value: value,
                build_plugin_base_context=lambda *args: {}, EMOJI_PLUS_ONE_ENABLED=False,
                has_emoji=lambda value: False, handle_reset_command=AsyncMock(),
                handle_agent_stop_command=AsyncMock(return_value=False),
                handle_compression_commands=AsyncMock(return_value=False),
                handle_config_command=AsyncMock(return_value=False), execute_plugins=AsyncMock(return_value=False),
                build_private_ai_text_message=lambda nickname, value: value,
                extract_image_urls_from_message=lambda message: [],
                cmc=types.SimpleNamespace(get_context=lambda *args: context),
                agent_session_id_of=lambda context: "private_2", _has_active_session=lambda sid: False,
                process_and_send=AsyncMock(), traceback=types.SimpleNamespace(print_exc=Mock()))
            event = PrivateMessageEvent()
            event.message = text
            raw = types.SimpleNamespace(get_stranger_info=AsyncMock(return_value=types.SimpleNamespace(
                data=types.SimpleNamespace(raw={"nickname": "fake"}))), send=AsyncMock())
            actions = env["LoggedActions"](raw, 1)
            with bind_request(state_for(event, actions=actions)):
                await env["handle_private_message"](event, actions)
            context.agen_content.assert_awaited_once()
            env["process_and_send"].assert_awaited_once()
            # 已开始的生成无法撤销外部请求，但完成后不得出站。
            async def generate(*args, **kwargs):
                switches["private_chat"] = False
                return "late reply", 0, 0, 0
            context.agen_content.side_effect = generate
            extract({"process_and_send"}, env)
            env.update(build_final_send_parts=lambda value: [value],
                find_llm_reply_failover_keyword=lambda value: "", is_split_reply_quote_enabled=lambda value: False)
            with bind_request(state_for(event, actions=actions)):
                await env["handle_private_message"](event, actions)
            raw.send.assert_not_awaited()
            env["traceback"].print_exc.assert_not_called()

    async def test_real_http_and_timer_entries_send_when_both_chat_types_closed(self):
        env, switches, _, _ = environment()
        switches.update(group_chat=False, private_chat=False, ai_chat=False)
        extract({"QQHttpSendError", "_qq_http_error", "send_qq_message_from_http", "_agent_task_notifier"}, env)
        raw = types.SimpleNamespace(send=AsyncMock(return_value={"status": "ok", "retcode": 0}))
        actions = env["LoggedActions"](raw, 1)
        context = types.SimpleNamespace(agen_content=AsyncMock(return_value=("timer", 0, 0, 0)))
        env.update(_current_qq_actions=actions, _qq_send_lock=threading.Lock(),
            _parse_qq_http_target=lambda payload: (payload["target_type"], payload["target_id"]),
            build_qq_http_segments=lambda payload: [Text("http")], build_http_send_parts=lambda text: [text],
            build_final_send_parts=lambda text: [text],
            cmc=types.SimpleNamespace(get_context=lambda *args: context))
        for kind in ("group", "private"):
            result = await asyncio.to_thread(env["send_qq_message_from_http"], {"target_type": kind, "target_id": 3})
            self.assertTrue(result["ok"])
            self.assertTrue(await env["_agent_task_notifier"]({"id": "fake", "is_group": kind == "group",
                "group_id": "3", "user_id": "", "content": "timer"} if kind == "group" else
                {"id": "fake", "is_group": False, "user_id": "2", "content": "timer"}))
        self.assertEqual(raw.send.await_count, 4)
        self.assertEqual(context.agen_content.await_count, 2)

    async def test_split_response_hot_close_stops_next_part_for_each_type(self):
        from bot import proactive_reply
        for factory, kind in ((GroupMessageEvent, "group"), (PrivateMessageEvent, "private")):
            env, switches, _, _ = environment()
            switches["weak_blacklist"] = False
            extract({"process_and_send"}, env)
            raw = types.SimpleNamespace(send=AsyncMock(return_value={"status": "ok", "retcode": 0}))
            actions = env["LoggedActions"](raw, 1)
            event = factory()
            async def sleep(_):
                switches[kind + "_chat"] = False
            env.update(build_final_send_parts=lambda value: value.split("<split>"),
                find_llm_reply_failover_keyword=lambda value: "", is_split_reply_quote_enabled=lambda value: False,
                attach_trace_send=Mock(return_value=True), asyncio=types.SimpleNamespace(sleep=sleep))
            with bind_request(state_for(event, actions=actions)), patch.object(proactive_reply, "notify_bot_message_sent"), self.assertRaises(ReplySuppressed):
                await env["process_and_send"](actions, event, "first<split>blocked", kind == "group")
            self.assertEqual(raw.send.await_count, 1)

    async def test_one_sample_across_generation_tools_and_send_and_live_policy(self):
        env, switches, others, _ = environment()
        random_fn = Mock(return_value=0.2)
        actions = object()
        state = state_for(sample=None, random_fn=random_fn, actions=actions)
        with bind_request(state):
            self.assertIs(env["_normal_ai_state"](), state)
            self.assertIs(env["_agent_actions"](types.SimpleNamespace()), actions)
            self.assertIs(env["_normal_ai_state"](), state)
            others["weak_blacklist_trigger_probability"] = 0.1
            with self.assertRaises(ReplySuppressed):
                env["_normal_ai_state"]()
            switches["weak_blacklist"] = False
            self.assertFalse(env["_ai_reply_allowed"](state))
        self.assertTrue(state.handled)
        random_fn.assert_called_once_with()
        self.assertIsNone(CURRENT_REQUEST.get())

    async def test_weak_applies_private_and_poke_but_not_unlisted_users(self):
        env, _, others, blacklist = environment()
        others["weak_blacklist_trigger_probability"] = 0
        for event in (PrivateMessageEvent(), NotifyEvent()):
            state = state_for(event)
            with bind_request(state), self.assertRaises(ReplySuppressed):
                env["_normal_ai_state"]()
            self.assertTrue(state.handled)
        state = state_for(GroupMessageEvent(user_id=8))
        self.assertTrue(env["_ai_reply_allowed"](state))
        blacklist.add("3")
        self.assertFalse(env["_ai_reply_allowed"](state))

    async def test_compatibility_percentage_and_live_strong_switches(self):
        env, switches, others, blacklist = environment()
        state = state_for(sample=0.2)
        others["weak_blacklist_trigger_probability"] = 30
        self.assertTrue(env["_ai_reply_allowed"](state))
        switches["ai_chat"] = False
        self.assertTrue(env["_ai_reply_allowed"](state))
        switches["group_chat"] = False
        self.assertFalse(env["_ai_reply_allowed"](state))
        switches["group_chat"] = True
        blacklist.add("2")
        self.assertFalse(env["_ai_reply_allowed"](state))

    async def test_proactive_decision_is_not_claimed_as_normal(self):
        env, _, _, _ = environment()
        state = state_for(source="proactive")
        with bind_request(state):
            self.assertIsNone(env["_normal_ai_state"]())
        self.assertFalse(state.handled)

    async def test_agent_scope_is_current_session_and_proactive_does_not_borrow_actions(self):
        env, _, _, _ = environment()
        state = state_for(actions=object())
        with bind_request(state):
            for ctx in (types.SimpleNamespace(is_group=True, group_id="4"),
                        types.SimpleNamespace(is_group=False, user_id="2")):
                with self.assertRaises(ReplySuppressed):
                    env["_agent_actions"](ctx)
        state = state_for(actions=object(), source="proactive")
        with bind_request(state):
            self.assertIsNone(env["_agent_actions"](types.SimpleNamespace(actions=None)))

    async def test_suppression_never_sends_error_or_uses_other_self_actions(self):
        env, _, others, _ = environment()
        actions = types.SimpleNamespace(send=AsyncMock())
        state = state_for(actions=actions)
        env["_current_qq_actions"] = object()
        env["_qq_actions_lock"] = threading.Lock()
        with bind_request(state):
            self.assertIs(env["_agent_actions"](types.SimpleNamespace(actions=None)), actions)
            others["weak_blacklist_trigger_probability"] = 0
            await env["send_error_detail"](actions, state.event, ReplySuppressed(), True)
            await env["send_error_detail"](actions, state.event, RuntimeError("普通失败"), True, error_type="ai")
            with self.assertRaises(ReplySuppressed):
                env["_agent_actions"](types.SimpleNamespace(actions=object()))
        actions.send.assert_not_awaited()

    async def test_handler_pre_registers_before_quote_and_finalizes_silent_denial(self):
        from bot import proactive_reply
        env, _, others, _ = environment()
        others["weak_blacklist_trigger_probability"] = 0
        order, seen = [], []
        event = GroupMessageEvent()

        def received(*args, **kwargs):
            order.append("received")
            self.assertEqual(kwargs["message_id"], "10")

        async def resolve(ev, actions, state):
            order.append("quote")
            self.assertIs(CURRENT_REQUEST.get(), state)

        async def inner(ev, actions):
            order.append("inner")
            seen.append(CURRENT_REQUEST.get())
            env["_normal_ai_state"]()

        def note(*args, **kwargs):
            order.append("note")
            self.assertTrue(kwargs["normal_handled"])
            self.assertEqual(kwargs["message_id"], "10")

        env.update(_resolve_incoming_quote=resolve, _handler_inner=inner)
        with patch.object(proactive_reply, "notify_user_message_received", received), patch.object(proactive_reply, "complete_normal_reply", note, create=True):
            await env["handler"](event, object())
        self.assertEqual(order, ["received", "quote", "inner", "note"])
        self.assertEqual(seen[0].key, ("1", True, "3"))
        self.assertIsNone(CURRENT_REQUEST.get())
        self.assertIsNotNone(env["INDEX"].resolve("1", True, "3", 10))

    async def test_hard_blacklist_handler_returns_before_all_side_effects(self):
        from bot import proactive_reply
        for event in (GroupMessageEvent(), PrivateMessageEvent()):
            for entry in ("2", "2,备注"):
                with self.subTest(event=type(event).__name__, entry=entry):
                    env, _, _, blacklist = environment()
                    blacklist.add(entry)
                    env.update(_record_incoming_event=Mock(), _resolve_incoming_quote=AsyncMock(),
                               _handler_inner=AsyncMock(), print=Mock())
                    with patch.object(proactive_reply, "notify_user_message_received") as notify:
                        await env["handler"](event, object())
                    env["_record_incoming_event"].assert_not_called()
                    env["_resolve_incoming_quote"].assert_not_awaited()
                    env["_handler_inner"].assert_not_awaited()
                    env["print"].assert_not_called()
                    notify.assert_not_called()
                    self.assertFalse(hasattr(event, "_normal_request_state"))

    async def test_handler_dynamic_blacklist_group_rule_and_self_echo(self):
        from bot import proactive_reply
        env, _, _, blacklist = environment()
        env.update(_record_incoming_event=Mock(), _resolve_incoming_quote=AsyncMock(),
                   _handler_inner=AsyncMock())
        with patch.object(proactive_reply, "notify_user_message_received"), patch.object(proactive_reply, "complete_normal_reply"):
            event = GroupMessageEvent()
            await env["handler"](event, object())
            self.assertEqual(env["_handler_inner"].await_count, 1)
            blacklist.add("2,动态")
            await env["handler"](event, object())
            self.assertEqual(env["_handler_inner"].await_count, 1)
            blacklist.clear()
            blacklist.add("3,群")
            await env["handler"](event, object())
            self.assertEqual(env["_handler_inner"].await_count, 1)
            blacklist.clear()
            blacklist.add("1,自身")
            await env["handler"](GroupMessageEvent(user_id=1), object())
            self.assertEqual(env["_handler_inner"].await_count, 2)
        self.assertEqual(env["_record_incoming_event"].call_count, 2)

    def test_social_consume_rechecks_index_author_hard_blacklist(self):
        env, _, _, blacklist = environment()
        state = state_for()
        state.social = types.SimpleNamespace(consume_reply=Mock(return_value="42"))
        env["INDEX"].record(*state.key, 42, 8, text="历史")
        self.assertEqual(env["_consume_social_reply"](state), "42")
        blacklist.add("8,旧作者")
        self.assertIsNone(env["_consume_social_reply"](state))

    def test_structured_history_read_filters_without_mutating_or_guessing(self):
        env, _, _, blacklist = environment()
        blacklist.add("8,备注")
        cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "LimitedDeepSeekContext")
        method = next(copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_build_messages")
        exec(compile(ast.Module(body=[method], type_ignores=[]), "history-offline", "exec"), env)
        env.update(build_llm_system_prompt=lambda value: value, fix_messages=lambda value: value)
        history = [{"role": "user", "author_id": "8", "content": "屏蔽"},
                   {"role": "user", "user_id": "8", "content": "旧结构"},
                   {"role": "assistant", "content": "无作者摘要保留 8"},
                   {"role": "user", "author_id": "9", "content": "正常文本 8"}]
        before = copy.deepcopy(history)
        result = env["_build_messages"](types.SimpleNamespace(system_prompt="sys", history=history))
        self.assertEqual([m["content"] for m in result], ["sys", "无作者摘要保留 8", "正常文本 8"])
        self.assertEqual(history, before)

    async def test_reentering_same_native_event_reuses_one_decision(self):
        from bot import proactive_reply
        env, _, _, _ = environment()
        event = GroupMessageEvent()
        decisions = []

        async def inner(ev, actions):
            decisions.append(CURRENT_REQUEST.get().decision)

        env["_handler_inner"] = inner
        with patch.object(proactive_reply, "notify_user_message_received"), patch.object(proactive_reply, "complete_normal_reply", create=True):
            await env["handler"](event, object())
            await env["handler"](event, object())
        self.assertIs(decisions[0], decisions[1])
        self.assertIsNone(CURRENT_REQUEST.get())

    async def test_disconnect_cancels_observations_and_drops_current_pending(self):
        from bot import proactive_reply
        env, _, _, _ = environment()
        extract({"clear_current_qq_actions"}, env)
        env.update(_qq_actions_lock=threading.Lock(), _current_qq_actions=object(), _current_bot_self_id="1")
        state = state_for()
        env["INDEX"].record(*state.key, 42, 8, text="断线前")
        state.social = object()
        state.pending_reply = "42"
        with bind_request(state), patch.object(proactive_reply, "cancel_observations") as cancel:
            env["clear_current_qq_actions"]()
        cancel.assert_called_once_with("disconnect")
        self.assertIsNone(state.social)
        self.assertIsNone(state.pending_reply)
        self.assertIsNone(env["_current_qq_actions"])
        self.assertIsNone(env["INDEX"].resolve(*state.key, 42))

    async def test_concurrent_handler_decisions_and_accounts_are_isolated(self):
        from bot import proactive_reply
        env, _, _, _ = environment()
        seen = []

        async def inner(event, actions):
            state = CURRENT_REQUEST.get()
            await asyncio.sleep(0)
            self.assertIs(CURRENT_REQUEST.get(), state)
            self.assertEqual(actions.self_id, str(event.self_id))
            seen.append(state)

        env["_handler_inner"] = inner
        with patch.object(proactive_reply, "notify_user_message_received"), patch.object(proactive_reply, "complete_normal_reply", create=True):
            await asyncio.gather(env["handler"](GroupMessageEvent(self_id=1), object()),
                                 env["handler"](GroupMessageEvent(self_id=9), object()))
        self.assertEqual({state.self_id for state in seen}, {"1", "9"})
        self.assertIsNot(seen[0].decision, seen[1].decision)
        self.assertIsNone(CURRENT_REQUEST.get())

    async def test_logged_send_records_only_real_receipts_and_never_weak_blocks_commands(self):
        env, _, others, _ = environment()
        others["weak_blacklist_trigger_probability"] = 0
        receipt = {"status": "ok", "retcode": 0, "data": {"message_id": -42}}
        underlying = types.SimpleNamespace(send=AsyncMock(return_value=receipt))
        actions = env["LoggedActions"](underlying, self_id=1)
        state = state_for(actions=actions, sample=None)
        with bind_request(state), patch.object(time, "time", return_value=123):
            self.assertIs(await actions.send(group_id=3, message=Message(Text("命令回复"))), receipt)
        record = env["INDEX"].resolve("1", True, "3", -42)
        self.assertEqual((record["text"], record["sent_at"], record["known"]), ("命令回复", 123, True))
        self.assertTrue(state.handled)
        self.assertIsNone(state.decision.sample)
        for ret in ({"status": "failed", "retcode": 100, "data": {"message_id": 88}},
                    types.SimpleNamespace(status="failed", ret_code=100, data=types.SimpleNamespace(raw={"message_id": 88})),
                    None):
            underlying.send.return_value = ret
            self.assertIs(await actions.send(Message(Text("失败")), 3), ret)
        self.assertIsNone(env["INDEX"].resolve("1", True, "3", 88))
        underlying.send.return_value = types.SimpleNamespace(status="ok", ret_code=0, data=types.SimpleNamespace(raw={"message_id": 89}))
        await actions.send(Message(Text("SDK")), 3)
        self.assertEqual(env["INDEX"].resolve("1", True, "3", 89)["text"], "SDK")

    async def test_del_receipt_tombstones_only_bound_session(self):
        env, _, _, _ = environment()
        index = env["INDEX"]
        for key in (("1", True, "3"), ("1", True, "4"), ("9", True, "3")):
            index.record(*key, 99, key[0], text="消息", is_bot=True, sent_at=10)
        underlying = types.SimpleNamespace(del_message=AsyncMock(return_value=None))
        actions = env["LoggedActions"](underlying, self_id=1)
        with bind_request(state_for(actions=actions)):
            await actions.del_message(99)
            self.assertFalse(index.resolve("1", True, "3", 99)["recalled"])
            underlying.del_message.return_value = {"status": "ok", "retcode": 0}
            await actions.del_message(99)
        self.assertTrue(index.resolve("1", True, "3", 99)["recalled"])
        self.assertFalse(index.resolve("1", True, "4", 99)["recalled"])
        self.assertFalse(index.resolve("9", True, "3", 99)["recalled"])

    async def test_non_ai_any_and_command_plugins_are_not_weak_blocked(self):
        env, _, others, _ = environment()
        others["weak_blacklist_trigger_probability"] = 0
        extract({"execute_plugins"}, env)
        plugin = types.SimpleNamespace(PLUGIN_NAME="普通插件", TRIGGHT_KEYWORD="Any", __name__="plain_plugin",
                                       on_message=AsyncMock(return_value=True))
        env.update(plugins=[plugin], reminder="/", inspect=inspect,
                   _make_plugin_logger=lambda name: lambda *args: None,
                   get_plugin_config=lambda *args: None, traceback=types.SimpleNamespace(print_exc=Mock()))
        for is_any, keyword, command in ((True, "Any", ""), (False, "天气", "天气 北京")):
            plugin.TRIGGHT_KEYWORD = keyword
            state = state_for(sample=None)
            with bind_request(state):
                self.assertTrue(await env["execute_plugins"](is_any, is_group=True, order=command))
            self.assertTrue(state.handled)
            self.assertIsNone(state.decision.sample)
            self.assertFalse(state.decision.denied)
        self.assertEqual(plugin.on_message.await_count, 2)

    async def test_plugin_suppression_is_handled_without_traceback(self):
        env, _, _, _ = environment()
        extract({"execute_plugins"}, env)
        plugin = types.SimpleNamespace(PLUGIN_NAME="AI插件", TRIGGHT_KEYWORD="Any", __name__="ai_plugin",
                                       on_message=AsyncMock(side_effect=ReplySuppressed()))
        trace = Mock()
        env.update(plugins=[plugin], reminder="/", inspect=inspect,
                   _make_plugin_logger=lambda name: lambda *args: None,
                   get_plugin_config=lambda *args: None, traceback=types.SimpleNamespace(print_exc=trace))
        with bind_request(state_for()):
            self.assertTrue(await env["execute_plugins"](True, is_group=True))
        trace.assert_not_called()

    async def test_poke_denial_does_not_lookup_user_or_emit_fallback(self):
        env, _, others, _ = environment()
        others["weak_blacklist_trigger_probability"] = 0
        extract({"handle_private_poke_event", "handle_group_poke_event"}, env)
        actions = types.SimpleNamespace(get_stranger_info=AsyncMock(), get_group_member_info=AsyncMock())
        env.update(POKE_REPLY_ENABLED=True, process_and_send=AsyncMock(),
                   traceback=types.SimpleNamespace(print_exc=Mock()))
        for method, event in (("handle_private_poke_event", PrivateMessageEvent()),
                              ("handle_group_poke_event", NotifyEvent())):
            with bind_request(state_for(event, actions=actions)):
                await env[method](event, actions)
        actions.get_stranger_info.assert_not_awaited()
        actions.get_group_member_info.assert_not_awaited()
        env["process_and_send"].assert_not_awaited()
        env["traceback"].print_exc.assert_not_called()

    async def test_legacy_uses_request_actions_explicit_meta_and_silent_deny(self):
        env, _, others, _ = environment()
        extract({"LegacyPluginAIAdapter"}, env)
        context = types.SimpleNamespace(agen_content=AsyncMock(return_value=("答复", 1, 1, 0)), last_trace_id="")
        env.update(filter_sensitive_content=lambda value: value, build_llm_user_message=lambda value: value,
                   process_and_send=AsyncMock(), traceback=types.SimpleNamespace(print_exc=Mock()))
        manager = types.SimpleNamespace(get_context=lambda *args: context)
        event = GroupMessageEvent()
        actions = object()
        state = state_for(event, actions=actions)
        with bind_request(state):
            result = await env["LegacyPluginAIAdapter"].generate_response(False, manager, "", {}, event)
        self.assertEqual(result, (None, None, True))
        self.assertIs(env["process_and_send"].await_args.args[0], actions)
        self.assertEqual(context.agen_content.await_args.kwargs["agent_meta"],
                         {"actions": actions, "event": event, "user_id": 2})
        others["weak_blacklist_trigger_probability"] = 0
        with bind_request(state_for(event, actions=actions)):
            self.assertEqual(await env["LegacyPluginAIAdapter"].generate_response(False, manager, "", {}, event),
                             (None, None, False))
        self.assertEqual(context.agen_content.await_count, 1)
        env["traceback"].print_exc.assert_not_called()

    async def test_social_selection_consumed_once_by_final_then_settings_unchanged(self):
        from bot.social_tools import SocialContext
        from bot import proactive_reply
        env, _, _, _ = environment()
        extract({"process_and_send"}, env)
        receipt = {"status": "ok", "retcode": 0, "data": {"message_id": 90}}
        actions = types.SimpleNamespace(send=AsyncMock(return_value=receipt))
        event = GroupMessageEvent()
        state = state_for(event, actions=actions)
        env["INDEX"].record(*state.key, 42, 8, text="可选引用")
        state.social = SocialContext(state.key, env["INDEX"].snapshot(*state.key), enabled=lambda: True)
        await state.social.select_reply("42")
        env.update(build_final_send_parts=lambda value: value.split("<split>"),
                   find_llm_reply_failover_keyword=lambda value: "", is_split_reply_quote_enabled=lambda value: False,
                   attach_trace_send=Mock(return_value=True), asyncio=types.SimpleNamespace(sleep=AsyncMock()))
        with bind_request(state), patch.object(proactive_reply, "notify_bot_message_sent"):
            await env["process_and_send"](actions, event, "第一段<split>第二段", True, reply_to_first=True)
            await env["process_and_send"](actions, event, "后续", True, reply_to_first=True)
        sent = [call.kwargs["message"].contents for call in actions.send.await_args_list]
        self.assertEqual(sent[0][0].id, "42")
        self.assertIsInstance(sent[1][0], Text)
        self.assertEqual(sent[2][0].id, "10")
        self.assertIsNone(state.social.quote_id)

    async def test_send_message_validates_explicit_candidate_then_consumes_for_final(self):
        from bot.social_tools import SocialContext
        from bot import proactive_reply
        env, _, _, _ = environment()
        extract({"_agent_send_message", "process_and_send"}, env)
        actions = types.SimpleNamespace(send=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 90}}))
        event = GroupMessageEvent()
        state = state_for(event, actions=actions)
        env["INDEX"].record(*state.key, 42, 8, text="可选引用")
        state.social = SocialContext(state.key, env["INDEX"].snapshot(*state.key), enabled=lambda: True)
        ctx = types.SimpleNamespace(is_group=True, group_id="3", user_id="2", actions=actions)

        async def serialized(factory, what):
            await factory()
            return True, ""

        env.update(build_final_send_parts=lambda value: value.split("<split>"),
                   find_llm_reply_failover_keyword=lambda value: "", is_split_reply_quote_enabled=lambda value: False,
                   attach_trace_send=Mock(return_value=True), _agent_send_serialized=serialized,
                   asyncio=types.SimpleNamespace(sleep=AsyncMock()))
        with bind_request(state), patch.object(proactive_reply, "notify_bot_message_sent"):
            rejected = await env["_agent_send_message"]({"content": "拒绝", "reply_to_id": "999"}, ctx)
            self.assertTrue(rejected.startswith("error:"))
            actions.send.assert_not_awaited()
            self.assertFalse((await env["_agent_send_message"]({"content": "工具第一段<split>工具第二段", "reply_to_id": "42"}, ctx)).startswith("error:"))
            await env["process_and_send"](actions, event, "最终答复", True, reply_to_first=False)
        sent = [call.kwargs["message"].contents for call in actions.send.await_args_list]
        self.assertEqual(sent[0][0].id, "42")
        self.assertIsInstance(sent[1][0], Text)
        self.assertIsInstance(sent[2][0], Text)
        self.assertIsNone(state.social.quote_id)

    async def test_split_send_rechecks_live_weak_switch_and_never_sends_second_part(self):
        from bot import proactive_reply
        env, _, others, _ = environment()
        extract({"process_and_send"}, env)
        actions = types.SimpleNamespace(send=AsyncMock(return_value={"status": "ok", "retcode": 0}))
        event = GroupMessageEvent()
        state = state_for(event, actions=actions)

        async def policy_changed(_):
            others["weak_blacklist_trigger_probability"] = 0

        env.update(build_final_send_parts=lambda value: value.split("<split>"),
                   find_llm_reply_failover_keyword=lambda value: "", is_split_reply_quote_enabled=lambda value: False,
                   attach_trace_send=Mock(return_value=True), asyncio=types.SimpleNamespace(sleep=policy_changed))
        with bind_request(state), patch.object(proactive_reply, "notify_bot_message_sent"), self.assertRaises(ReplySuppressed):
            await env["process_and_send"](actions, event, "第一段<split>不能发送", True)
        self.assertEqual(actions.send.await_count, 1)

    async def test_real_handler_normal_observation_send_matrix_and_races(self):
        import tempfile
        from bot import proactive_reply as proactive
        cases = ("success", "default", "group", "empty", "filtered", "first_reject", "partial_reject",
                 "send_error", "bookkeeping_false", "bookkeeping_error", "index_error", "index_capacity", "trace_error", "trace_reject", "trace_success", "no_id", "invalid_id",
                 "between_parts", "last_receipt", "generation", "cancel", "completion_error")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                env, switches, _, _ = environment()
                switches["weak_blacklist"] = False
                extract({"process_and_send", "attach_trace_send"}, env)
                event = GroupMessageEvent() if case == "group" else PrivateMessageEvent()
                logs, sends = [], []
                async def planner(cid, cfg):
                    return False
                scheduler = proactive.ProactiveReplyScheduler(lambda: {"private": {"enabled": True,
                    "observation": {"trigger_probability": 1}}, "group": {"enabled": True,
                    "observation": {"trigger_probability": 1}}}, planner, planner, logs.append)
                async def send(**kwargs):
                    sends.append(kwargs)
                    if case == "send_error":
                        raise RuntimeError("fake transport")
                    if case == "cancel":
                        raise asyncio.CancelledError()
                    if case == "last_receipt" and len(sends) == 2:
                        scheduler.notify_user_message_received(False, "2", message_id="new")
                    if case == "first_reject" or case == "partial_reject" and len(sends) == 2:
                        return {"status": "failed", "retcode": 100}
                    receipt = {"status": "ok", "retcode": 0}
                    if case != "no_id":
                        receipt["data"] = {"message_id": "not-real-id" if case == "invalid_id" else 90 + len(sends)}
                    return receipt
                async def sleep(_):
                    if case == "between_parts":
                        scheduler.notify_user_message_received(False, "2", message_id="new")
                env.update(build_final_send_parts=lambda value: [part for part in value.split("<split>") if part.strip()],
                    find_llm_reply_failover_keyword=lambda value: "blocked" if case == "filtered" else "",
                    is_split_reply_quote_enabled=lambda value: False,
                    trace_store=types.SimpleNamespace(attach_send=Mock(return_value=True)), print=logs.append,
                    asyncio=types.SimpleNamespace(sleep=sleep), log_console=lambda *args: logs.append(str(args)))
                if case == "trace_error":
                    # 执行真实 TraceStore.attach_send；只注入详情读取故障，不导入其磁盘单例。
                    trace_tree = ast.parse((ROOT / "bot" / "trace_store.py").read_text(encoding="utf-8"))
                    cls = next(node for node in trace_tree.body if isinstance(node, ast.ClassDef) and node.name == "TraceStore")
                    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "attach_send")
                    trace_env = {"Optional": __import__("typing").Optional, "copy": copy, "time": time}
                    exec(compile(ast.Module(body=[method], type_ignores=[]), "trace-store-offline", "exec"), trace_env)
                    store = types.SimpleNamespace(_lock=threading.RLock(), records=[{"id": "fake-trace"}],
                        _pending_details={}, _read_detail_db=Mock(side_effect=OSError("PRIVATE_RAW secret")))
                    env["trace_store"] = store
                    store.attach_send = Mock(wraps=types.MethodType(trace_env["attach_send"], store))
                elif case == "trace_reject":
                    env["trace_store"].attach_send.return_value = False
                async def inner(ev, actions):
                    if case == "generation":
                        scheduler.notify_user_message_received(False, "2", message_id="new")
                    if case == "index_error":
                        env["INDEX"].record = Mock(side_effect=OSError("PRIVATE_RAW secret"))
                    elif case == "index_capacity":
                        env["INDEX"] = MessageIndex(max_bytes=0)
                    try:
                        await env["process_and_send"](actions, ev, "" if case == "empty" else "one<split>two",
                            case == "group", observe_normal_reply=case != "default",
                            trace_id="fake-trace" if case in ("trace_error", "trace_reject", "trace_success") else "")
                    except RuntimeError:
                        pass
                env["_handler_inner"] = inner
                with patch.object(proactive, "_STATE_DIR", directory), patch.object(proactive, "_scheduler", scheduler):
                    original = scheduler.notify_bot_message_sent
                    def recorded(*args, **kwargs):
                        if case == "bookkeeping_error":
                            raise OSError("PRIVATE_RAW secret")
                        return False if case == "bookkeeping_false" else original(*args, **kwargs)
                    with patch.object(scheduler, "notify_bot_message_sent", side_effect=recorded):
                        if case == "completion_error":
                            with patch.object(proactive, "complete_normal_reply", side_effect=OSError("PRIVATE_RAW secret")):
                                await env["handler"](event, types.SimpleNamespace(send=send))
                        elif case == "cancel":
                            with self.assertRaises(asyncio.CancelledError):
                                await env["handler"](event, types.SimpleNamespace(send=send))
                        else:
                            await env["handler"](event, types.SimpleNamespace(send=send))
                    state = scheduler.get_session_state(case == "group", "3" if case == "group" else "2")
                    expected_success = case in ("success", "group", "trace_success", "no_id", "invalid_id")
                    self.assertEqual(state["observation"] is not None, expected_success)
                    if case in ("trace_error", "trace_reject", "index_capacity", "index_error"):
                        self.assertFalse(event._normal_request_state.normal_reply_sent)
                        self.assertEqual(len(sends), 2)
                    if case in ("trace_error", "trace_reject", "trace_success"):
                        env["trace_store"].attach_send.assert_called_once()
                    elif case == "success":
                        env["trace_store"].attach_send.assert_not_called()
                    self.assertEqual(state["total_proactive_sent"], 0)
                    self.assertEqual(state["last_proactive_at"], 0)
                    self.assertNotIn("PRIVATE_RAW", " ".join(logs))
                    self.assertNotIn("secret", " ".join(logs))
                    if case in ("success", "bookkeeping_error", "index_error", "trace_error", "completion_error"):
                        self.assertEqual(len(sends), 2)
                    scheduler.stop()

    def test_only_normal_private_and_group_final_calls_opt_in_observation(self):
        calls = [(node, call) for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                 for call in ast.walk(node) if isinstance(call, ast.Call)
                 and isinstance(call.func, ast.Name) and call.func.id == "process_and_send"
                 and any(keyword.arg == "observe_normal_reply" for keyword in call.keywords)]
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum(node.name == "handle_private_message" for node, _ in calls), 2)
        self.assertEqual(sum(node.name == "_handler_inner" for node, _ in calls), 1)
        for node, call in calls:
            self.assertTrue(next(kw.value.value for kw in call.keywords if kw.arg == "observe_normal_reply"))
            self.assertEqual(next(kw.value.value for kw in call.keywords if kw.arg == "is_group"),
                             node.name == "_handler_inner")

    def test_recalled_or_other_scope_social_candidate_is_not_used(self):
        env, _, _, _ = environment()
        state = state_for()
        state.social = types.SimpleNamespace(consume_reply=Mock(return_value="42"))
        env["INDEX"].record("9", True, "3", 42, 8, text="另账号")
        self.assertIsNone(env["_consume_social_reply"](state))
        env["INDEX"].record(*state.key, 42, 8, text="被撤回")
        env["INDEX"].tombstone(*state.key, 42)
        self.assertIsNone(env["_consume_social_reply"](state))

    def test_actual_follow_up_paths_gate_and_build_quote_before_enqueue(self):
        for name in ("handle_private_message", "_handler_inner"):
            node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            source = ast.unparse(node)
            positions = [match.start() for match in re.finditer(r"_follow_up_session\(", source)]
            self.assertTrue(positions)
            for position in positions:
                prefix = source[:position]
                self.assertIn("_normal_ai_state()", prefix)
                self.assertIn("_build_normal_message(", prefix)
        enhanced = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "EnhancedLimitedDeepSeekContext")
        method = next(node for node in enhanced.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "agen_content")
        source = ast.unparse(method)
        self.assertLess(source.index("_normal_ai_state()"), source.index("acquire_history_lock("))
        self.assertIn("agent_ctx.extra['social'] = normal_state.social", source)
        base = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "LimitedDeepSeekContext")
        generation = next(node for node in base.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "agen_content")
        self.assertIn("_normal_ai_state()", ast.unparse(generation))
        state = state_for(source="proactive")
        env, _, _, _ = environment()
        with bind_request(state):
            self.assertIsNone(env["_normal_ai_state"]())
        self.assertFalse(state.handled)

    async def test_enhanced_denial_happens_before_lock_or_model(self):
        env, _, others, _ = environment()
        others["weak_blacklist_trigger_probability"] = 0
        node = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "EnhancedLimitedDeepSeekContext")
        method = next(copy.deepcopy(node) for node in node.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "agen_content")
        module = ast.Module(body=[method], type_ignores=[])
        exec(compile(module, str(ROOT / "main.py"), "exec"), env)
        env["acquire_history_lock"] = AsyncMock(side_effect=AssertionError("不能获取锁"))
        with bind_request(state_for()), self.assertRaises(ReplySuppressed):
            await env["agen_content"](object(), "问题")
        env["acquire_history_lock"].assert_not_awaited()


class GroupContextBlacklistTests(unittest.TestCase):
    def setUp(self):
        from bot.group_chat_context import GroupChatContextManager
        self.manager = GroupChatContextManager()
        self.env, _, _, self.blacklist = environment()
        extract({"reserve_group_chat_context_suffix", "record_group_chat_context",
                 "build_group_context_record_text"}, self.env)
        from bot.group_chat_context import format_group_message, format_history_block
        self.env.update(group_chat_context=self.manager, is_group_chat_context_enabled=lambda: True,
                        get_group_chat_context_max_messages=lambda: 30,
                        format_group_message=format_group_message, format_history_block=format_history_block)

    def test_dynamic_tail_and_reserve_rollback_preserve_author_and_string_contract(self):
        self.manager.record("3", "旧作者正文", author_id="8")
        self.manager.record("3", "正常文本含 8", author_id="9")
        self.manager.record("3", "无作者旧摘要 8")
        visible = self.env["_memory_author_visible"]
        self.assertEqual(len(self.manager.tail("3", 30, visible=visible)), 3)
        self.blacklist.add("8,备注")
        self.assertEqual(self.manager.tail("3", 30, visible=visible), ["正常文本含 8", "无作者旧摘要 8"])
        self.assertEqual(self.manager.size("3"), 3)  # tail 不删旧历史
        trigger = self.manager.record("3", "触发", author_id="2")
        reservation, suffix = self.env["reserve_group_chat_context_suffix"]("3", trigger)
        self.assertNotIn("旧作者正文", suffix)
        self.assertIn("正常文本含 8", suffix)
        self.assertTrue(all(isinstance(row, str) for row in reservation.records))
        self.assertEqual(reservation.records[0].author_id, "9")
        original = reservation.records[0]
        self.assertTrue(self.manager.rollback(reservation))
        self.assertIs(self.manager.tail("3", 30)[0], original)
        self.blacklist.add("9,后来拉黑")
        self.assertEqual(self.manager.tail("3", 30, visible=visible), ["无作者旧摘要 8"])
        trigger = self.manager.record("3", "再触发", author_id="2")
        reservation, suffix = self.env["reserve_group_chat_context_suffix"]("3", trigger)
        self.assertEqual(reservation.records, ("无作者旧摘要 8",))
        self.assertTrue(self.manager.commit(reservation))
        self.assertEqual(self.manager.size("3"), 0)

    def test_rollback_then_blacklist_cannot_resurrect_visible_author(self):
        self.manager.record("3", "稍后拉黑", author_id="8")
        trigger = self.manager.record("3", "触发")
        reservation = self.manager.reserve_for_inject("3", trigger)
        self.blacklist.add("8")
        self.manager.rollback(reservation)
        self.assertEqual(self.manager.tail("3", 30)[0].author_id, "8")
        trigger = self.manager.record("3", "再触发")
        _, suffix = self.env["reserve_group_chat_context_suffix"]("3", trigger)
        self.assertEqual(suffix, "")

    def test_native_reply_payload_not_used_as_unverified_group_preview(self):
        self.env["Segments"] = types.SimpleNamespace(At=type("At", (), {}), Text=Text,
                                                    Image=type("Image", (), {}), Reply=Reply)
        event = GroupMessageEvent()
        reply = Reply("42")
        reply.text = reply.content = reply.message = "不可泄漏的未校验引用"
        event.message = [reply, Text("正常正文")]
        text = self.env["build_group_context_record_text"](event, "昵称", "fallback")
        self.assertIn("正常正文", text)
        self.assertNotIn("不可泄漏", text)
        self.env["record_group_chat_context"](event, "昵称", "fallback")
        self.assertEqual(self.manager.tail("3", 1)[0].author_id, "2")
        event.message = [reply]
        text = self.env["build_group_context_record_text"](event, "昵称", "不可泄漏的未校验引用")
        self.assertNotIn("不可泄漏", text)


class CommandPrefixTests(unittest.IsolatedAsyncioTestCase):
    def prefix_env(self):
        env = {"reminder": "/", "plugins": [], "plugins_help": ""}
        extract({"prefixed_command_text", "match_command", "handle_agent_stop_command",
                 "render_plugin_help", "build_plugins_help_section", "_agent_plugin_entries"}, env)
        return env

    async def test_stop_uses_live_complete_prefix_in_both_sessions(self):
        env = self.prefix_env()
        request_stop = Mock(return_value=True)
        env.update(AGENT_ABORTS=types.SimpleNamespace(request_stop=request_stop),
                   Manager=types.SimpleNamespace(Message=lambda text: text),
                   Segments=types.SimpleNamespace(Text=lambda text: text))
        actions = types.SimpleNamespace(send=AsyncMock())
        event = types.SimpleNamespace(user_id=2, group_id=3)
        for prefix in ("/", "#", "!!", ".*", "[+]"):
            env["reminder"] = prefix
            for is_group in (False, True):
                for name in ("停止", "stop"):
                    self.assertTrue(await env["handle_agent_stop_command"](event, actions, f" {prefix}{name} ", is_group))
                    self.assertEqual(request_stop.call_args.args[0], "group_3" if is_group else "private_2")
                for bad in ("stop", prefix, prefix + "stopx", prefix + "stop 参数", prefix + prefix + "stop"):
                    self.assertFalse(await env["handle_agent_stop_command"](event, actions, bad, is_group), (prefix, bad))
                if prefix != "/":
                    self.assertFalse(await env["handle_agent_stop_command"](event, actions, "/stop", is_group))

    def test_reset_actual_early_conditions_and_at_preserve_prefix_characters(self):
        env = self.prefix_env()
        reset_checks = [n for n in ast.walk(TREE) if isinstance(n, ast.If)
                        and "handle_reset_command" in " ".join(ast.unparse(x) for x in n.body)
                        and "prefixed_command_text" in ast.unparse(n.test)]
        self.assertEqual(len(reset_checks), 2)
        for prefix in ("/", "#", "!!", ".*", "[+]"):
            env["reminder"] = prefix
            for check in reset_checks:
                code = compile(ast.Expression(body=check.test), "reset-condition", "eval")
                for text, expected in ((prefix + "reset", True), ("重置", True), (prefix + "resetting", False), (prefix + prefix + "reset", False), ("reset", False)):
                    self.assertEqual(eval(code, env, {"user_message": text}), expected, (prefix, text))
                if prefix != "/":
                    self.assertFalse(eval(code, env, {"user_message": "/reset"}))
            self.assertIsNone(env["prefixed_command_text"](prefix[:-1] + "stop"))
            self.assertEqual(env["prefixed_command_text"](prefix + "天气 https://a/b !!x"), "天气 https://a/b !!x")
        normalize = next(n for n in ast.walk(TREE) if isinstance(n, ast.If)
                         and ast.unparse(n.test) == "has_command_at_bot and at_command_text.strip()")
        code = compile(ast.Module(body=normalize.body, type_ignores=[]), "at-normalize", "exec")
        env["reminder"] = "!!"
        for text, expected in (("!!reset", "!!reset"), ("reset", "!!reset"), ("!reset", "!!!reset"), ("!!!!reset", "!!!!reset")):
            env["at_command_text"] = text
            exec(code, env)
            self.assertEqual(env["user_message"], expected)

    async def test_command_and_any_plugins_keep_existing_contract_with_custom_prefix(self):
        env, _, _, _ = environment()
        extract({"execute_plugins"}, env)
        calls = []
        async def command(order, reminder):
            calls.append((order, reminder))
            return True
        plugin = types.SimpleNamespace(PLUGIN_NAME="weather", TRIGGHT_KEYWORD="天气", __name__="weather", on_message=command)
        env.update(plugins=[plugin], inspect=inspect, _make_plugin_logger=lambda name: lambda *args: None,
                   get_plugin_config=lambda *args: None, traceback=types.SimpleNamespace(print_exc=Mock()))
        for prefix in ("/", "#", "!!", ".*", "[+]"):
            env["reminder"] = prefix
            self.assertTrue(await env["execute_plugins"](False, order="天气 北京", reminder=prefix))
            self.assertEqual(calls[-1], ("天气 北京", prefix))
        plugin.TRIGGHT_KEYWORD = "Any"
        self.assertTrue(await env["execute_plugins"](True, order="赞我", reminder="!!"))
        self.assertEqual(calls[-1], ("赞我", "!!"))

    def test_plugin_help_templates_legacy_and_urls_follow_hot_prefix(self):
        env = self.prefix_env()
        plugin = types.SimpleNamespace(PLUGIN_NAME="weather", TRIGGHT_KEYWORD="天气", __name__="weather",
                                       HELP_MESSAGE="/天气 [城市] —— URL https://a/b /tmp/file\n{reminder}天气")
        env.update(plugins=[plugin], plugins_help="\n       " + plugin.HELP_MESSAGE)
        for prefix in ("/", "#", "!!", ".*", "[+]"):
            env["reminder"] = prefix
            expected = f"{prefix}天气 [城市] —— URL https://a/b /tmp/file\n{prefix}天气"
            self.assertEqual(env["_agent_plugin_entries"]()[0]["help"], expected)
            self.assertIn("       " + expected, env["build_plugins_help_section"]())
            self.assertEqual(env["render_plugin_help"]("https://a/b\n/tmp/file\n赞我", "Any"), "https://a/b\n/tmp/file\n赞我")
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("lstrip(reminder)", source)
        self.assertNotIn("/reset 或 重置", source)
        self.assertNotIn("/停止 或 /stop", source)
        self.assertEqual(source.count("{reminder}reset 或 重置"), 2)
        self.assertEqual(source.count("{reminder}停止 或 {reminder}stop"), 2)


if __name__ == "__main__":
    unittest.main()
