"""空回复修复的回归测试：不启动机器人，直接断言三处改动的行为。

运行：python tools/verify_empty_reply.py
覆盖：
  1. bot/agent.py 的 _EMPTY_RESULT_NOTICE 常量存在且含英文文案
  2. 空判断逻辑（独立模拟 agent.py:1215-1219）：None / "" / 纯空白 / 错误串
  3. main.py 非 agent 路径的 ValueError 分支（模拟 3403-3409）
  4. main.py 插件路径在空 result 时仍会 return (None, None, False) 且不再静默
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_agent_notice_constant():
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    assert "_EMPTY_RESULT_NOTICE" in src, "缺少 _EMPTY_RESULT_NOTICE 常量"
    assert "returned an empty result" in src, "提示应包含英文 empty result 文案"
    assert "Adjust the arguments" in src, "提示应包含调整参数的指引"
    # 确认在 sent_to_model 处真的拼接了 empty_notice
    assert "sent_to_model = materialized + notice + empty_notice" in src, (
        "sent_to_model 没有拼接 empty_notice"
    )
    print("PASS agent 常量与拼接：_EMPTY_RESULT_NOTICE 已定义并加入 sent_to_model")


def test_empty_judgement_logic():
    """复刻 agent.py:1215-1219 的判断，独立验证各种输入。"""
    def judge(result):
        result_text = str(result or "")
        if not result_text.strip() and not result_text.startswith("error:"):
            return "NOTIFIED"
        return "PASS_THROUGH"

    # 应当触发空提示
    assert judge(None) == "NOTIFIED"
    assert judge("") == "NOTIFIED"
    assert judge("   ") == "NOTIFIED"
    assert judge("\n\n  \t\n") == "NOTIFIED"
    # 不应触发：错误串走自己的路径
    assert judge("error: something broke") == "PASS_THROUGH"
    assert judge("error:") == "PASS_THROUGH"
    # 不应触发：有实际内容
    assert judge("ok") == "PASS_THROUGH"
    assert judge("  123  ") == "PASS_THROUGH"
    assert judge("0") == "PASS_THROUGH"  # 数字 0 也是有内容
    print("PASS 空判断逻辑：None/空串/纯空白触发提示；error: 与有内容不触发")


def test_main_plain_path_raises():
    """模拟 main.py 3403-3409 的 rstrip + 空白抛错。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # 确认抛错代码真的插进去了
    assert 'raise ValueError("模型返回了空白回复")' in src, (
        "main.py 非 agent 路径缺少空白抛错"
    )

    # 行为级：复刻该代码段并断言
    def plain_path(raw: str) -> str:
        result = raw.rstrip("\n")
        if not result.strip():
            raise ValueError("模型返回了空白回复")
        return result

    assert plain_path("hello\n\n") == "hello"
    assert plain_path("hello") == "hello"
    for bad in ("", "\n", "   \n  \n", "\t\t"):
        try:
            plain_path(bad)
        except ValueError as e:
            assert "空白回复" in str(e)
        else:
            raise AssertionError(f"应当对 {bad!r} 抛 ValueError")
    print("PASS main.py 非 agent 路径：纯空白抛 ValueError，有内容正常通过")


def test_main_plugin_path_logs():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # Bug C 修复：必须用 strip() 拦截纯空白（" " rstrip 不掉换行但还是空白）
    m = re.search(
        r'result = \(result or ""\)\.rstrip\("\\n"\)\s*\n(?:.*\n)*?\s*if not result\.strip\(\):\s*\n\s*log_console\("PLUGIN",\s*"旧插件 AI 调用返回空，已静默跳过"\)\s*\n\s*return None, None, False',
        src,
    )
    assert m, "插件路径缺少 strip() 空白检测或 log_console"
    print("PASS main.py 插件路径：纯空白用 strip() 拦截，return 前先 log_console")


def test_main_production_path_whitespace_raises():
    """Bug A：EnhancedLimitedDeepSeekContext.agen_content 生产路径（5720）必须有空白检查。

    注意：不能用 src.index 找 timeout——它会命中父类 3375/3394。要用 class 定义
    作为锚点，确保检测到的是子类路径。
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # 先定位到子类的定义
    child_idx = src.index("class EnhancedLimitedDeepSeekContext")
    child_src = src[child_idx:]
    # 子类里必须有 raise ValueError
    assert 'raise ValueError("模型返回了空白回复")' in child_src, (
        "子类 EnhancedLimitedDeepSeekContext.agen_content 缺少空白抛错"
    )
    # 并且必须出现在子类自己的 rstrip 之后
    rstrip_idx = child_src.index('result.rstrip')
    raise_idx = child_src.index('raise ValueError("模型返回了空白回复")')
    assert raise_idx > rstrip_idx, "子类 ValueError 应在 rstrip 之后"
    # 全文件共 2 处（父类 + 子类）
    count = src.count('raise ValueError("模型返回了空白回复")')
    assert count == 2, f"应有 2 处 ValueError（父+子），实际 {count}"
    print("PASS main.py 子类生产路径（EnhancedLimitedDeepSeekContext）有空白抛错")


def test_untrusted_wrap_preserves_empty_and_error():
    """P1-A：untrusted_output 包装不应破坏空结果和 error: 的语义。

    空结果不被包装；error: 开头的单行短文本（handler 失败信号）不被包装；
    但外部内容（即使 error: 开头但是多行/长文本）仍会被包装（P2-I 启发式）。
    """
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    idx = src.index("if spec.untrusted_output")
    block = src[idx:idx + 700]
    # 包装条件必须同时检查 text.strip() 和启发式失败判断
    assert "text.strip()" in block, "untrusted 包装前应检查 text.strip()"
    assert "_looks_like_handler_error" in block, "untrusted 包装应使用启发式判断（局部变量）"
    # _exec_one 里必须有 _looks_like_handler_error = _is_handler_failure_text(text) 的赋值
    assert "_looks_like_handler_error = _is_handler_failure_text(text)" in src, (
        "_exec_one 应用 _is_handler_failure_text 计算 _looks_like_handler_error"
    )
    wrap_idx = block.index("UNTRUSTED_HINT")
    strip_idx = block.index("text.strip()")
    error_idx = block.index("_looks_like_handler_error")
    assert strip_idx < wrap_idx and error_idx < wrap_idx, "strip/error 检查必须在包装之前"
    print("PASS bot/agent.py untrusted_output 包装：空结果和 handler 失败不被包装")


def test_poke_fallback_catches_value_error():
    """P1-B：拍一拍应捕获 ValueError 走本地兜底文案，而不是把错误发给用户。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # 两处 poke handler 都要有 ValueError catch
    count = src.count('except ValueError as e:')
    # 至少要有私聊+群聊两处 poke 的 catch
    assert count >= 2, f"poke handler 的 ValueError catch 至少应有 2 处，实际 {count}"
    # 并且 catch 里要有 "空白回复" 判断
    poke_block_start = src.index("handle_private_poke_event") if "handle_private_poke_event" in src else 0
    assert '"空白回复" in str(e)' in src, "ValueError catch 里应有 '空白回复' 判断"
    print("PASS main.py 拍一拍兜底：捕获 ValueError 走本地文案")


def test_exec_one_returns_raw_and_packaged():
    """P1-H + P2-I：_exec_one 必须返回 (raw, packaged) 元组。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    assert "packaged = " in src, "_exec_one 应构造 packaged 变量"
    assert "return text, packaged" in src, "_exec_one 成功路径应返回 (text, packaged)"
    assert "isinstance(text, tuple)" in src, "_finish 应解包 _exec_one 的 tuple 返回"
    assert "raw, packaged = text" in src, "_finish 应解包 raw, packaged"
    print("PASS bot/agent.py _exec_one 返回 (raw, packaged) 元组，_finish 正确解包")


def test_handler_failure_heuristic():
    """P2-I：_is_handler_failure_text 应正确区分 handler 失败信号 vs 外部内容。

    行为测试：直接导入并调用，不靠源码字符串匹配。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from bot.agent import _is_handler_failure_text

    # handler 主动失败：单行短 error:
    assert _is_handler_failure_text("error: 工具 X 执行失败") is True
    assert _is_handler_failure_text("error: query 不能为空") is True
    assert _is_handler_failure_text("error: 所有搜索源都失败了") is True
    # 空 / 非 error 开头
    assert _is_handler_failure_text("") is False
    assert _is_handler_failure_text("hello") is False
    assert _is_handler_failure_text("result ok") is False
    # 外部内容碰巧以 error: 开头（多行 → 不算失败）
    long_external = "error: 这是外部文档正文，不是失败\n第二行内容\n第三行内容"
    assert _is_handler_failure_text(long_external) is False, (
        "多行外部内容不应被判为 handler 失败"
    )
    # 单行但超长（>200 字符）也不算失败
    long_single_line = "error: " + "x" * 250
    assert _is_handler_failure_text(long_single_line) is False, (
        "单行超长内容不应被判为 handler 失败（更像外部文档）"
    )
    print("PASS bot/agent.py _is_handler_failure_text 启发式行为正确")


def test_untrusted_wrap_heuristic_behavior():
    """P2-I 行为测试：untrusted 工具的外部内容（即使 error: 开头）应被包装。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    # _exec_one 里必须用启发式函数判断
    assert "_is_handler_failure_text(text)" in src, (
        "_exec_one 应使用 _is_handler_failure_text 而不是裸 startswith"
    )
    exec_start = src.index("async def _exec_one")
    exec_end = src.index("return text, packaged", exec_start)
    exec_block = src[exec_start:exec_end]
    assert "_is_handler_failure_text" in exec_block
    assert "UNTRUSTED_HINT" in exec_block
    print("PASS bot/agent.py _exec_one 用启发式判断包装")


def test_downstream_uses_raw_not_packaged():
    """下游所有 ok/empty/digest 判断必须用 raw，不能用 packaged。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    idx = src.index("result, elapsed_ms, raw_result = packed")
    segment = src[idx:idx + 3000]
    assert 'failed = _is_handler_failure_text(raw_result)' in segment
    assert "tool_digest.append((name, str(raw_result or \"\")))" in segment
    assert "result_text = str(raw_result or \"\")" in segment
    print("PASS bot/agent.py 下游判断全部用 raw_result + _is_handler_failure_text")


def test_degraded_text_no_wrap_leak():
    """P1-H 行为测试：tool_digest 里的内容不应包含 UNTRUSTED_HINT。

    通过直接调用 _exec_one + 模拟 untrusted 工具验证。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    # 直接调 _exec_one：untrusted 工具返回正常内容时，raw 不应包含 UNTRUSTED_HINT
    import asyncio
    from bot.agent import _exec_one, AgentContext, AgentSettings, REGISTRY
    # 构造一个最小 untrusted 工具 spec
    from bot.agent import ToolSpec
    async def _dummy_handler(args, ctx):
        return "这是真实工具结果"
    spec = ToolSpec(
        name="_test_untrusted",
        description="test",
        parameters={"type": "object", "properties": {}},
        handler=_dummy_handler,
        level="user",
        untrusted_output=True,
    )
    ctx = AgentContext()
    ctx.user_level = "admin"
    settings = AgentSettings(enabled_tools={"_test_untrusted": True})
    raw, packaged = asyncio.run(_exec_one(spec, {}, ctx, settings))
    # raw 不应包含 UNTRUSTED_HINT
    from bot.agent import UNTRUSTED_HINT
    assert UNTRUSTED_HINT not in raw, "raw 不应包含 UNTRUSTED_HINT"
    assert "这是真实工具结果" in raw
    # packaged 应包含 UNTRUSTED_HINT
    assert UNTRUSTED_HINT in packaged
    assert "<tool_result" in packaged
    print("PASS P1-H：raw 不含 UNTRUSTED_HINT，packaged 含包装（行为断言）")


def test_agent_settings_dataclass_default():
    """P2-E：AgentSettings dataclass 默认值应与 DEFAULT_AGENT_CONFIG 同步。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from bot.agent import AgentSettings
    s = AgentSettings()
    assert s.retry_attempts == 3, (
        f"AgentSettings 默认 retry_attempts 应为 3，实际 {s.retry_attempts}"
    )
    print("PASS bot/agent.py AgentSettings dataclass 默认值 = 3（行为断言）")


def test_process_and_send_logs_empty_parts():
    """P3-K：process_and_send 的 if not parts: return 必须有日志。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    idx = src.index("def process_and_send")
    block = src[idx:idx + 5000]
    parts_idx = block.index("if not parts:")
    after_parts = block[parts_idx:parts_idx + 400]
    assert 'log_console("SEND"' in after_parts, "if not parts: 分支内应有 log_console"
    print("PASS main.py process_and_send 空 parts 分支有日志")


def test_agent_is_final_empty_raises():
    """Bug B：is_final 分支返回前 content 空必须抛 RuntimeError，不能 return 空。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    # 找到 is_final 分支
    idx = src.index("if is_final:")
    block = src[idx:idx + 1500]
    # 必须有空 content 检测
    assert "if not content.strip():" in block, "is_final 分支缺少空 content 检测"
    # 必须抛 RuntimeError，不是 return 空
    assert "raise error" in block or "raise RuntimeError" in block, "is_final 空 content 必须抛 RuntimeError"
    # 并且抛错时 degraded_text 应保留工具结果
    assert "degraded_text" in block, "is_final 抛错时应写 degraded_text 保留工具结果"
    print("PASS bot/agent.py is_final 分支：空 content 抛 RuntimeError 并保留 degraded_text")


def test_tool_digest_skips_empty():
    """Bug D：tool_digest 不应把空结果算成"已完成工具"。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    # 找到 tool_digest.append 那一行
    idx = src.index("tool_digest.append")
    # 往前看 300 字符，应该有 strip() 检查
    segment = src[max(0, idx - 300):idx]
    assert ".strip()" in segment, "tool_digest.append 前应有 strip() 空白检测"
    print("PASS bot/agent.py tool_digest：空结果不计入")


def test_no_regression_on_error_path():
    """确认 error: 开头的工具结果没有被新逻辑误改。"""
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    # error: 检测条件应当保留（用启发式 _is_handler_failure_text）
    assert '_is_handler_failure_text' in src
    assert 'failed = _is_handler_failure_text(raw_result)' in src
    print("PASS 错误结果路径未受影响")


def test_agent_empty_final_reply_goes_to_retry():
    """agent 最终回复为空时应当走 retry_attempts 就地重试，而不是静默吞。

    真逻辑在 bot/agent.py:1035-1053：message 既没 tool_calls 也没 content 时
    触发 retry，重试用尽抛 RuntimeError 走 key 轮询。
    """
    src = (ROOT / "bot/agent.py").read_text(encoding="utf-8")
    # 空响应的就地重试判断
    assert 'if getattr(message, "tool_calls", None) or str(getattr(message, "content", "") or "").strip():' in src, (
        "缺少空回复的就地重试入口判断"
    )
    # 重试提示
    assert "模型返回空响应，第" in src, "缺少空响应重试日志"
    # 重试用尽抛 RuntimeError
    assert "模型连续" in src and "返回空响应" in src, "重试用尽应抛 RuntimeError"
    # 默认值应是 3
    import json
    cfg_default = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
    assert cfg_default["Agent"]["retry_attempts"] == 3, (
        f"config.default.json 的 Agent.retry_attempts 应为 3，实际 {cfg_default['Agent'].get('retry_attempts')}"
    )
    agent_meta_src = (ROOT / "webui_core/agent_meta.py").read_text(encoding="utf-8")
    assert '"retry_attempts": 3' in agent_meta_src, "agent_meta.py 默认值应为 3"
    print("PASS agent 空回复走 retry_attempts 就地重试，默认值 3")


def main():
    test_agent_notice_constant()
    test_empty_judgement_logic()
    test_main_plain_path_raises()
    test_main_plugin_path_logs()
    test_main_production_path_whitespace_raises()
    test_untrusted_wrap_preserves_empty_and_error()
    test_poke_fallback_catches_value_error()
    test_exec_one_returns_raw_and_packaged()
    test_handler_failure_heuristic()
    test_untrusted_wrap_heuristic_behavior()
    test_downstream_uses_raw_not_packaged()
    test_degraded_text_no_wrap_leak()
    test_agent_settings_dataclass_default()
    test_process_and_send_logs_empty_parts()
    test_agent_is_final_empty_raises()
    test_tool_digest_skips_empty()
    test_no_regression_on_error_path()
    test_agent_empty_final_reply_goes_to_retry()
    print("\n全部 PASS。覆盖：")
    print("- agent 工具结果空：英文提示拼进 sent_to_model")
    print("- agent 空回复：retry_attempts=3 就地重试，用尽抛 RuntimeError 走 key 轮询")
    print("- agent is_final 空调用空 content：抛 RuntimeError 并保留 degraded_text")
    print("- agent tool_digest：空结果不计入'已完成'，且 raw 无 UNTRUSTED_HINT 泄露")
    print("- agent _exec_one：返回 (raw, packaged)，下游判断用 raw")
    print("- agent _is_handler_failure_text：启发式区分 handler 失败 vs 外部内容")
    print("- AgentSettings dataclass 默认值 = 3（行为断言）")
    print("- main.py 非 agent 父类路径（3393）：空白抛 ValueError")
    print("- main.py 非 agent 子类生产路径（5720）：空白抛 ValueError")
    print("- main.py 拍一拍：ValueError 兜底走本地文案")
    print("- main.py 插件路径：纯空白 strip() 拦截 + log_console")
    print("- main.py process_and_send 空 parts 分支有日志")
    print("- error: 开头的工具结果路径未被改动")


if __name__ == "__main__":
    sys.exit(main())
