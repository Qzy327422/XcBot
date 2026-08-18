# -*- coding: utf-8 -*-
"""Agent 定时任务。

AI 可以设定一次性提醒（「明天早上8点叫我」）或周期提醒（「每天中午提醒喝水」），
到点由后台调度循环唤醒，把提示词交给 LLM 生成一句自然的提醒语再发出去。

任务持久化在 data/agent_tasks.json，重启不丢。调度精度 30 秒，够用且不费 CPU。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

MAX_TASKS_PER_SESSION = 20
MAX_TASKS_TOTAL = 500
TICK_SECONDS = 30

_lock = threading.RLock()
_tasks: dict[str, dict] = {}
_path = ""
_notifier = None
# 调度器跑在独立线程 + 独立事件循环里，不依附于任何一条消息的 loop
_sched_thread: threading.Thread | None = None
_sched_loop = None
_stop_flag = threading.Event()


def bind(store_path: str, notifier) -> None:
    """notifier(task: dict) -> Awaitable，由 main.py 注入，负责真正发消息。"""
    global _path, _notifier
    _path = store_path
    _notifier = notifier
    _load()


def _load() -> None:
    global _tasks
    with _lock:
        try:
            if _path and os.path.exists(_path):
                with open(_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    _tasks = {k: v for k, v in data.items() if isinstance(v, dict)}
                    # 上次退出时正在发送的任务：run_at 已被清零，但通知没确认完成。
                    # 恢复成待发送，让它重新排队，而不是静默消失。
                    for task in _tasks.values():
                        if task.pop("in_flight", None):
                            task["run_at"] = float(task.pop("orig_run_at", 0) or 0) or time.time()
                            print(f"[AgentTask] 任务 {task.get('id')} 上次未发送完成，已恢复待发送")
        except Exception as e:
            print(f"[AgentTask] 加载任务失败: {e}")
            _tasks = {}


def _save() -> None:
    with _lock:
        if not _path:
            return
        try:
            os.makedirs(os.path.dirname(_path), exist_ok=True)
            tmp = f"{_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(_tasks, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, _path)
        except Exception as e:
            print(f"[AgentTask] 保存任务失败: {e}")


def _session_key(ctx) -> str:
    return f"group_{ctx.group_id}" if ctx.is_group and ctx.group_id else f"private_{ctx.user_id}"


def parse_when(text: str, repeat: str = "once") -> tuple[float, str]:
    """把模型给的时间描述解析成时间戳。

    只认标准格式，不做中文自然语言解析——正则永远追不上「下周三」「国庆节后」
    这类说法。模型自己知道当前时间（系统提示词里注入了），让它算出绝对时间
    比我们猜更可靠（cron 表达式 + ISO datetime）。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0.0, "error: when 不能为空"

    now = datetime.now()

    # 相对时间：+30m / +2h / +1d
    if raw.startswith("+"):
        match = raw[1:].strip().lower()
        unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if len(match) >= 2 and match[-1] in unit_map:
            try:
                amount = float(match[:-1])
            except ValueError:
                return 0.0, f"error: 无法解析相对时间「{raw}」，正确格式如 +30m、+2h、+1d"
            if amount <= 0:
                return 0.0, "error: 相对时间必须大于 0"
            return (now + timedelta(seconds=amount * unit_map[match[-1]])).timestamp(), ""
        return 0.0, f"error: 无法解析相对时间「{raw}」，正确格式如 +30m、+2h、+1d"

    # 绝对时间：YYYY-MM-DD HH:MM / HH:MM
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            parsed = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        elif fmt == "%m-%d %H:%M":
            parsed = parsed.replace(year=now.year)
        # 时间点已经过去时要顺延，否则「每天12点」在下午创建会立刻触发一次
        if parsed <= now:
            step = {"hourly": timedelta(hours=1), "daily": timedelta(days=1),
                    "weekly": timedelta(days=7)}.get(repeat)
            if step is not None:
                # 算术跳跃，不要 while 逐次累加：用户给「0001-01-01 00:00 每小时」
                # 时逐次累加要循环上千万次，会把消息处理线程卡住一两秒。
                gap = (now - parsed).total_seconds()
                stride = step.total_seconds()
                if stride > 0:
                    parsed += step * (math.floor(gap / stride) + 1)
            elif fmt == "%H:%M":
                parsed += timedelta(days=1)
            elif fmt == "%m-%d %H:%M":
                parsed = parsed.replace(year=now.year + 1)
            else:
                return 0.0, (
                    f"error: 指定的时间 {raw} 已经过去了。请先用 get_current_time 确认当前时间，"
                    "再给出一个未来的时间点。"
                )
        return parsed.timestamp(), ""

    return 0.0, (
        f"error: 无法解析时间「{raw}」。支持的格式："
        "绝对时间 2026-08-02 08:00 或 08:00；相对时间 +30m、+2h、+1d。"
        "如果用户说的是「明天」「下周」这类相对说法，请先用 get_current_time 查当前时间，自己算出绝对时间。"
    )


def _next_run(task: dict) -> float:
    """周期任务算下一次触发时间。

    用数学运算一步到位，不要 while base += step：脏数据或系统时间回拨时
    base 可能远小于 now（比如 base=0），每小时任务要循环近 50 万次，
    而调度器每 30 秒会对所有周期任务重算一遍。
    """
    repeat = task.get("repeat", "once")
    # 优先用 cycle_base：失败重试会把 run_at 改成"两分钟后"，
    # 拿它当下一周期起点会让固定周期随每次重试持续漂移。
    try:
        base = float(task.get("cycle_base") or task.get("run_at", 0) or 0)
    except (TypeError, ValueError):
        base = float(task.get("run_at", 0) or 0)
    step = {"daily": 86400, "hourly": 3600, "weekly": 604800}.get(repeat, 0)
    if step <= 0:
        return 0.0
    now = time.time()
    if base > now:
        return base
    # 向前推进到严格大于 now 的第一个时间点
    return base + (math.floor((now - base) / step) + 1) * step


def add_task(ctx, content: str, when: str, repeat: str = "once") -> str:
    repeat = str(repeat or "once").strip().lower()
    if repeat not in ("once", "hourly", "daily", "weekly"):
        return "error: repeat 只能是 once / hourly / daily / weekly"
    run_at, err = parse_when(when, repeat)
    if err:
        return err
    content = str(content or "").strip()
    if not content:
        return "error: content 不能为空"

    session = _session_key(ctx)
    with _lock:
        mine = [t for t in _tasks.values() if t.get("session") == session]
        if len(mine) >= MAX_TASKS_PER_SESSION:
            return f"error: 这个会话已经有 {len(mine)} 个定时任务了（上限 {MAX_TASKS_PER_SESSION}），请先删掉一些。"
        if len(_tasks) >= MAX_TASKS_TOTAL:
            return f"error: 全局定时任务已达上限 {MAX_TASKS_TOTAL}。"
        task_id = uuid.uuid4().hex[:8]
        _tasks[task_id] = {
            "id": task_id,
            "session": session,
            "user_id": str(ctx.user_id or ""),
            "group_id": str(ctx.group_id or ""),
            "is_group": bool(ctx.is_group),
            "content": content[:500],
            "repeat": repeat,
            "run_at": run_at,
            "created_at": time.time(),
            "fired": 0,
        }
        _save()
    when_text = datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M")
    repeat_text = {"once": "仅一次", "hourly": "每小时", "daily": "每天", "weekly": "每周"}[repeat]
    return (
        f"已创建定时任务（编号 {task_id}）：{when_text} {repeat_text}，内容「{content[:60]}」。"
        f"到时间我会主动发消息。请把编号和时间告诉用户。"
    )


def list_tasks(ctx) -> str:
    session = _session_key(ctx)
    with _lock:
        mine = sorted(
            (t for t in _tasks.values() if t.get("session") == session),
            key=lambda t: float(t.get("run_at", 0) or 0),
        )
    if not mine:
        return "当前会话没有定时任务。"
    repeat_text = {"once": "仅一次", "hourly": "每小时", "daily": "每天", "weekly": "每周"}
    lines = [f"当前会话有 {len(mine)} 个定时任务："]
    for task in mine:
        when = datetime.fromtimestamp(float(task.get("run_at", 0) or 0)).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"- 编号 {task['id']}：{when} {repeat_text.get(task.get('repeat'), '?')}"
            f"，内容「{task.get('content', '')[:60]}」"
        )
    return "\n".join(lines)


def cancel_task(ctx, task_id: str) -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        return "error: task_id 不能为空"
    session = _session_key(ctx)
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return f"error: 找不到编号 {task_id} 的任务。可以先用 action=list 查看现有任务。"
        # 只能删本会话的任务：否则 A 群能删掉 B 群的提醒
        if task.get("session") != session:
            return f"error: 任务 {task_id} 属于其他会话，不能在这里删除。"
        _tasks.pop(task_id, None)
        _save()
    return f"已取消定时任务 {task_id}（原内容「{task.get('content', '')[:60]}」）。"


MAX_DELIVERY_RETRIES = 5
RETRY_DELAY_SECONDS = 120


async def _run_one(task: dict, now: float) -> None:
    """跑一个到期任务并推进它的状态。异常只记录，不影响其他任务。

    notifier 返回 False 或抛异常表示没送达（QQ 断线、发送失败）。这时不能把
    一次性任务删掉——用户的提醒就永久消失了。改为过一会儿重试，
    连续失败到上限才放弃。
    """
    delivered = True
    try:
        if callable(_notifier):
            result = await _notifier(dict(task))
            # notifier 显式返回 False 才算失败；返回 None（老签名）视为成功
            delivered = result is not False
    except Exception as e:
        delivered = False
        print(f"[AgentTask] 任务 {task.get('id')} 执行失败: {e}")

    with _lock:
        live = _tasks.get(task["id"])
        if live is None:
            return
        if not delivered:
            live.pop("in_flight", None)
            attempts = int(live.get("delivery_failures", 0) or 0) + 1
            live["delivery_failures"] = attempts
            if attempts >= MAX_DELIVERY_RETRIES:
                print(f"[AgentTask] 任务 {task.get('id')} 连续 {attempts} 次未送达，放弃")
                if live.get("repeat", "once") == "once":
                    _tasks.pop(task["id"], None)
                else:
                    live["run_at"] = _next_run(live)
            else:
                # 稍后重试，不推进周期、不删除
                live["run_at"] = time.time() + RETRY_DELAY_SECONDS
                print(f"[AgentTask] 任务 {task.get('id')} 未送达（第 {attempts} 次），"
                      f"{RETRY_DELAY_SECONDS} 秒后重试")
            _save()
            return

        live["delivery_failures"] = 0
        live.pop("in_flight", None)
        live.pop("orig_run_at", None)
        live["fired"] = int(live.get("fired", 0) or 0) + 1
        live["last_fired_at"] = now
        if live.get("repeat", "once") == "once":
            _tasks.pop(task["id"], None)
        else:
            live["run_at"] = _next_run(live)
        _save()


def _due_at(task: dict, now: float) -> bool:
    """单个任务是否到期。解析失败只跳过这一个，不能连累同批其他任务。

    原来写成列表推导里的 float(...)，一条任务的 run_at 是脏数据（字符串、None
    以外的类型）就会抛异常，整轮 tick 直接失败，当批所有正常提醒都发不出去。
    """
    try:
        run_at = float(task.get("run_at", 0) or 0)
    except (TypeError, ValueError):
        print(f"[AgentTask] 任务 {task.get('id')} 的 run_at 非法（{task.get('run_at')!r}），已跳过")
        return False
    return 0 < run_at <= now


async def _tick() -> None:
    now = time.time()
    with _lock:
        due = [t for t in _tasks.values() if _due_at(t, now)]
        # 先占位推进 run_at，避免 notifier 还在跑（生成文案要几秒）时
        # 下一次 tick 又把同一个任务当成到期的，重复推送
        for t in due:
            # 一次性任务不能直接写 run_at=0：那等于"已完成"，通知还没发出去
            # 进程就退出的话，重启后调度器再也不会选中它，提醒永久丢失。
            # 改为标记 in_flight 并记下原定时间，_run_one 成功后才真正结束。
            if t.get("repeat", "once") == "once":
                t["in_flight"] = True
                t.setdefault("orig_run_at", t.get("run_at"))
                t["run_at"] = 0.0
            else:
                # 周期任务：基准时间单独存，避免失败重试时间被当成下次周期起点
                t.setdefault("cycle_base", t.get("run_at"))
                t["run_at"] = _next_run(t)
    if not due:
        return
    # 并发执行：每个提醒都要过一次 LLM（约数秒），串行的话 10 个同时到期
    # 就要几十秒才推完，最后一个人收到时早过了点
    await asyncio.gather(*[_run_one(t, now) for t in due], return_exceptions=True)


async def scheduler_loop() -> None:
    """后台调度循环。异常只记录不退出，否则一次抖动就永久停掉所有提醒。"""
    print(f"[AgentTask] 调度器已启动，当前 {len(_tasks)} 个任务")
    while not _stop_flag.is_set():
        try:
            await _tick()
        except Exception as e:
            print(f"[AgentTask] 调度循环异常（已忽略）: {e}")
        # 用可唤醒的等待代替固定 sleep：stop_scheduler 只是置个标记的话，
        # 线程要把这一轮 30 秒睡完才发现该退出，退出流程就得干等。
        try:
            await asyncio.wait_for(asyncio.to_thread(_stop_flag.wait, TICK_SECONDS),
                                   timeout=TICK_SECONDS + 5)
        except asyncio.TimeoutError:
            pass


def _scheduler_thread_main() -> None:
    """在独立线程里跑一个专属事件循环。

    不能把调度器 create_task 到消息 handler 的 loop 上：Hyper 的 OneBot 适配器
    对每条消息都新建线程 + asyncio.run（Hyper/Adapters/OneBot.py:193,265），
    asyncio.run 返回时会取消该 loop 上所有未完成的 task——调度器会随着那条消息
    处理结束一起被杀，而且因为「被取消的 task 算 done」，下条消息又会重启一次、
    再被杀一次，定时提醒永远不会触发。
    """
    global _sched_loop
    loop = asyncio.new_event_loop()
    _sched_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scheduler_loop())
    except Exception as e:
        print(f"[AgentTask] 调度线程退出: {e}")
    finally:
        try:
            loop.close()
        except Exception:
            pass


def ensure_scheduler_started() -> None:
    """启动调度线程；重复调用安全。"""
    global _sched_thread
    with _lock:
        if _sched_thread is not None and _sched_thread.is_alive():
            return
        _stop_flag.clear()
        _sched_thread = threading.Thread(
            target=_scheduler_thread_main, name="AgentTaskScheduler", daemon=True
        )
        _sched_thread.start()


def stop_scheduler(timeout: float = 3.0) -> None:
    """退出时停掉调度线程。

    daemon 线程本可直接随进程退出，但正在跑的 _tick 里可能刚推送完提醒、
    还没写回 fired 计数与下次触发时间；等它一下能少丢一次状态。
    """
    _stop_flag.set()
    thread = _sched_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    # 兜底：线程若卡在 notifier 里没能及时退出，至少把内存状态落盘
    try:
        _save()
    except Exception:
        pass
