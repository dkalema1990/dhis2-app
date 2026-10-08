"""
formulas.py
------------
A deliberately restricted formula evaluator for calculated fields (e.g.
"[DPT3 Immunization] / [ANC 4th visit] * 100"). Reference an existing data
element by wrapping its exact name in square brackets.

This does NOT use eval()/exec() on raw user input. Instead it walks a
parsed Python AST and only permits a small, explicit whitelist of nodes:
numbers, the four arithmetic operators plus power/modulo, unary +/-, and
a tiny set of safe functions (round, abs, min, max). Anything else —
attribute access, subscripting, comprehensions, function definitions,
imports, etc. — is rejected before it's ever evaluated. This matters
because formulas here can be entered by any `submitter`, not just `admin`.
"""

import ast
import operator
import re

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
}
_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max}

_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")


class FormulaError(Exception):
    pass


def extract_referenced_elements(formula: str) -> list[str]:
    """The data element names referenced in [brackets], in order of first use."""
    seen, out = set(), []
    for m in _BRACKET_RE.finditer(formula):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _tokenize(formula: str, available_elements: set) -> tuple[str, dict]:
    mapping = {}

    def repl(m):
        name = m.group(1).strip()
        if name not in available_elements:
            raise FormulaError(f"Unknown data element referenced: \"{name}\"")
        key = f"_v{len(mapping)}"
        mapping[key] = name
        return key

    rewritten = _BRACKET_RE.sub(repl, formula)
    if not rewritten.strip():
        raise FormulaError("Formula is empty.")
    return rewritten, mapping


def _eval_node(node, variables: dict):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise FormulaError("Only numbers are allowed as literal values.")
        if isinstance(node.value, int):
            return node.value  # keep as int (e.g. so round(x, 1) gets a valid ndigits)
        if isinstance(node.value, float):
            return node.value
        raise FormulaError("Only numbers are allowed as literal values.")
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaError(f"Unrecognized reference in formula: {node.id}")
        val = variables[node.id]
        return float("nan") if val is None else float(val)
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise FormulaError("That operator isn't allowed — only + - * / ** % are supported.")
        left, right = _eval_node(node.left, variables), _eval_node(node.right, variables)
        try:
            return op(left, right)
        except ZeroDivisionError:
            return float("nan")
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise FormulaError("That unary operator isn't allowed.")
        return op(_eval_node(node.operand, variables))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise FormulaError("Only round(), abs(), min(), max() may be used as functions.")
        if node.keywords:
            raise FormulaError("Keyword arguments aren't supported.")
        args = [_eval_node(a, variables) for a in node.args]
        return float(_FUNCS[node.func.id](*args))
    raise FormulaError(f"Expression type not allowed: {type(node).__name__}")


def validate_formula(formula: str, available_elements: set) -> None:
    """Raises FormulaError if the formula is structurally invalid or references
    an unknown data element. Doesn't require actual values — just checks syntax
    and that every [bracketed] reference exists."""
    try:
        rewritten, mapping = _tokenize(formula, available_elements)
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Invalid formula syntax: {e}")
    # Evaluate with dummy values just to confirm every node type is allowed
    dummy = {k: 1.0 for k in mapping}
    _eval_node(tree, dummy)


def evaluate_formula(formula: str, available_elements: set, facility_values: dict) -> float:
    """facility_values: {data_element_name: actual_value} for one facility."""
    rewritten, mapping = _tokenize(formula, available_elements)
    tree = ast.parse(rewritten, mode="eval")
    variables = {k: facility_values.get(v) for k, v in mapping.items()}
    try:
        return _eval_node(tree, variables)
    except FormulaError:
        raise
    except Exception as e:
        raise FormulaError(str(e))
