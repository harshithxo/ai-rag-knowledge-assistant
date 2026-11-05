import ast
import operator as op
import re
from langchain_core.tools import tool

_ALLOWED_BINOPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod}
_ALLOWED_UNARY = {ast.UAdd: op.pos, ast.USub: op.neg}


def _safe_eval(expression: str) -> float:
    expression = expression.strip()
    if len(expression) > 200 or not re.fullmatch(r"[0-9+\-*/%.()\s]+", expression):
        raise ValueError("Unsupported calculator expression")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent is too large")
            return _ALLOWED_BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](visit(node.operand))
        raise ValueError("Unsupported calculator expression")

    value = visit(ast.parse(expression, mode="eval"))
    if abs(value) > 1e100:
        raise ValueError("Result is too large")
    return value


@tool
def calculator(expression: str) -> str:
    """Safely calculate a basic arithmetic expression."""
    try:
        return str(_safe_eval(expression))
    except Exception as exc:
        raise ValueError(f"Calculator error: {exc}") from exc
