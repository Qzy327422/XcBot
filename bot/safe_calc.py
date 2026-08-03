# -*- coding: utf-8 -*-
"""安全的算术表达式求值。

不用正则黑名单 + eval：Python 表达式语法太灵活，`9**(+999999999)`、`9**(9**9)`、
`(9)**(999999)` 都能绕过「数字 ** 数字」这种字符串匹配。正确做法是先用 ast 解析，
再按白名单校验每个节点，最后自己算——全程不碰 eval。
"""
from __future__ import annotations

import ast
import math
import operator

# 只允许这些节点。任何函数调用、名字引用、属性访问、下标、推导式一律拒绝。
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

MAX_EXPR_CHARS = 300
MAX_NODES = 200
MAX_DEPTH = 15
# 结果位数上限。10^4300 已经远超日常需求，而再大一点点就会开始明显吃 CPU 和内存
MAX_RESULT_DIGITS = 4300
MAX_ABS_OPERAND = 10 ** 100


class CalcError(Exception):
    """表达式不合法或超出可计算范围。message 直接可以给模型看。"""


def _digits_of(value) -> float:
    """估算数值的十进制位数。用 log10 估，不真的把数算出来。"""
    try:
        av = abs(float(value))
    except (TypeError, ValueError, OverflowError):
        return float("inf")
    if av in (0.0,):
        return 1.0
    if math.isinf(av) or math.isnan(av):
        return float("inf")
    return math.log10(av) + 1.0


def _check_pow(base, exp) -> None:
    """在真正做幂运算之前估算结果规模。

    先算后判断是没用的：9**(9**9) 在得到结果那一刻内存已经爆了。
    """
    try:
        e = float(exp)
    except (TypeError, ValueError, OverflowError):
        raise CalcError("幂运算的指数不是有效数值")
    if e != int(e) and abs(float(base)) > 1e6:
        raise CalcError("底数过大时不支持小数指数")
    if abs(e) > 10 ** 6:
        raise CalcError("幂运算的指数过大（绝对值需小于 1000000）")
    est = _digits_of(base) * abs(e)
    if est > MAX_RESULT_DIGITS:
        raise CalcError(
            f"计算结果太大（约 {int(min(est, 1e18))} 位数字），"
            "请把问题拆小、改用科学计数法近似，或换个思路"
        )


def _eval(node, depth: int = 0):
    if depth > MAX_DEPTH:
        raise CalcError("表达式嵌套层数过深")
    if not isinstance(node, _ALLOWED_NODES):
        raise CalcError(f"表达式含不支持的语法（{type(node).__name__}），只支持四则运算与幂运算")

    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("只支持数字字面量")
        if abs(node.value) > MAX_ABS_OPERAND:
            raise CalcError("表达式里的数值过大")
        return node.value

    if isinstance(node, ast.UnaryOp):
        # 必须显式判断运算符类型：只看「不是 USub 就当 +」的话，
        # ~1 会被算成 +1、not 0 会被算成 0，既不在白名单里结果也是错的
        if isinstance(node.op, ast.USub):
            return -_eval(node.operand, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return +_eval(node.operand, depth + 1)
        raise CalcError("只支持正负号，不支持按位取反或逻辑非")

    # BinOp
    left = _eval(node.left, depth + 1)
    right = _eval(node.right, depth + 1)
    op_type = type(node.op)

    if op_type is ast.Pow:
        _check_pow(left, right)
        try:
            return left ** right
        except (OverflowError, ValueError, ZeroDivisionError) as e:
            raise CalcError(f"幂运算失败：{e}")

    func = _BINOPS.get(op_type)
    if func is None:
        raise CalcError("不支持这个运算符")
    try:
        result = func(left, right)
    except ZeroDivisionError:
        raise CalcError("除数不能为 0")
    except (OverflowError, ValueError) as e:
        raise CalcError(f"计算失败：{e}")
    if _digits_of(result) > MAX_RESULT_DIGITS:
        raise CalcError("计算结果太大，请把问题拆小")
    return result


def safe_eval(expr: str):
    """求值一个算术表达式。任何问题都抛 CalcError，消息可直接给模型。"""
    text = str(expr or "").strip()
    if not text:
        raise CalcError("表达式不能为空")
    if len(text) > MAX_EXPR_CHARS:
        raise CalcError(f"表达式过长（超过 {MAX_EXPR_CHARS} 字符）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"表达式语法有误：{e.msg}")

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise CalcError("表达式过于复杂")
    return _eval(tree)
