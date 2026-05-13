"""``AtConditionTrigger`` —— 当受限表达式判定为真时触发。

LAYOUT §2.E 明确禁止使用裸 ``eval``。优先依赖 :mod:`simpleeval`；该依赖
缺失时退化到一个仅支持比较 + 布尔运算 + 名称/属性/下标读取的 AST
sandbox，足够 MVP 演示使用。

TODO(deps): 在 ``pyproject.toml`` 加入 ``simpleeval>=0.9.13`` 后可移除
fallback 实现。
"""

from __future__ import annotations

import ast
import logging
import operator
from typing import Any, Callable, Dict, Mapping

from agent_world.script._registrars import TriggerBase

log = logging.getLogger(__name__)

try:  # pragma: no cover - import-time wiring
    from simpleeval import SimpleEval, DEFAULT_OPERATORS  # type: ignore

    _HAS_SIMPLEEVAL = True
except Exception:  # noqa: BLE001 - any import failure -> fallback
    SimpleEval = None  # type: ignore[assignment]
    DEFAULT_OPERATORS = None  # type: ignore[assignment]
    _HAS_SIMPLEEVAL = False


# ---------------------------------------------------------------------------
# Tiny AST-based safe eval fallback.
# Supports: literals, names lookup in a flat dict, attribute access (read-only),
# subscript access, boolean ops (and/or/not), comparison ops, unary +/- on
# numerics. Refuses calls, lambdas, comprehensions, attribute-write, etc.
# ---------------------------------------------------------------------------

_BIN_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_CMP_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def _safe_eval(expr: str, names: Mapping[str, Any]) -> Any:
    tree = ast.parse(expr, mode="eval")

    def _ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise NameError(f"name '{node.id}' is not defined in safe-eval scope")
            return names[node.id]
        if isinstance(node, ast.Attribute):
            # Block dunder access — without `Call`, attribute walking alone
            # can still leak `__class__.__init__.__globals__` etc.
            if node.attr.startswith("_"):
                raise ValueError(
                    f"attribute access to dunder/private name not allowed: {node.attr!r}"
                )
            base = _ev(node.value)
            return getattr(base, node.attr)
        if isinstance(node, ast.Subscript):
            base = _ev(node.value)
            key = _ev(node.slice)
            return base[key]
        if isinstance(node, ast.UnaryOp):
            v = _ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return +v
            if isinstance(node.op, ast.Not):
                return not v
            raise ValueError(f"unary op not allowed: {type(node.op).__name__}")
        if isinstance(node, ast.BinOp):
            op_fn = _BIN_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"binary op not allowed: {type(node.op).__name__}")
            return op_fn(_ev(node.left), _ev(node.right))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for v in node.values:
                    result = _ev(v)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for v in node.values:
                    result = _ev(v)
                    if result:
                        return result
                return result
            raise ValueError("unsupported BoolOp")
        if isinstance(node, ast.Compare):
            left = _ev(node.left)
            for op_node, right_node in zip(node.ops, node.comparators):
                cmp_fn = _CMP_OPS.get(type(op_node))
                if cmp_fn is None:
                    raise ValueError(f"compare op not allowed: {type(op_node).__name__}")
                right = _ev(right_node)
                if not cmp_fn(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Tuple):
            return tuple(_ev(e) for e in node.elts)
        if isinstance(node, ast.List):
            return [_ev(e) for e in node.elts]
        raise ValueError(f"AST node not allowed in safe-eval: {type(node).__name__}")

    return _ev(tree)


class AtConditionTrigger(TriggerBase):
    """Fire when a ``simpleeval`` expression evaluates truthy.

    Args:
        expr: simpleeval/AST expression. ``world`` and ``t`` are bound
            as names in the evaluation scope. The expression must be
            free of side effects (calls / assignments are rejected).
    """

    def __init__(self, *, expr: str) -> None:
        self.expr = expr

    def fires(self, world: Any, t: int) -> bool:
        names = {"world": world, "t": t}
        try:
            if _HAS_SIMPLEEVAL:
                evaluator = SimpleEval(names=names)  # type: ignore[operator]
                return bool(evaluator.eval(self.expr))
            return bool(_safe_eval(self.expr, names))
        except Exception as exc:  # noqa: BLE001
            log.warning("AtConditionTrigger eval failed: %s -> %s", self.expr, exc)
            return False
