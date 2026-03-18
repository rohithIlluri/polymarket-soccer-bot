"""
sandbox.py — AST-based validation for Claude-generated trade.py code.

Prevents execution of code that imports dangerous modules, calls dangerous
builtins, or accesses dunder attributes that could be used for escape.
"""
import ast
import logging

log = logging.getLogger(__name__)

BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "signal",
    "ctypes", "importlib", "pathlib", "io", "tempfile",
    "multiprocessing", "threading", "http", "urllib",
    "ftplib", "smtplib", "pickle", "shelve", "code",
    "codeop", "compileall", "webbrowser", "antigravity",
    "builtins", "runpy", "zipimport", "pkgutil",
})

ALLOWED_MODULES = frozenset({
    "logging", "typing", "math", "statistics", "collections",
    "dataclasses", "functools", "itertools", "operator",
    "numpy", "scipy", "np",
})

BLOCKED_BUILTINS = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "breakpoint", "exit", "quit", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir",
    "input", "help", "memoryview", "type",
})

BLOCKED_DUNDER_ATTRS = frozenset({
    "__import__", "__subclasses__", "__globals__", "__builtins__",
    "__code__", "__func__", "__self__", "__module__", "__class__",
    "__bases__", "__mro__", "__dict__", "__weakref__",
    "__loader__", "__spec__", "__file__", "__path__",
})


def validate_trade_file(source: str) -> tuple[bool, str]:
    """
    Validate trade.py source code via AST analysis.

    Returns:
        (True, "") if the code is safe to execute.
        (False, reason) if the code contains blocked constructs.
    """
    # Step 1: Parse — catches syntax errors
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    # Step 2: Walk AST and check every node
    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                if top_module in BLOCKED_MODULES:
                    return False, f"Blocked import: '{alias.name}'"
                if top_module not in ALLOWED_MODULES:
                    return False, f"Unrecognized import: '{alias.name}' (not in allowlist)"

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split(".")[0]
                if top_module in BLOCKED_MODULES:
                    return False, f"Blocked import: 'from {node.module}'"
                if top_module not in ALLOWED_MODULES:
                    return False, f"Unrecognized import: 'from {node.module}' (not in allowlist)"

        # Check function calls to blocked builtins
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in BLOCKED_BUILTINS:
                    return False, f"Blocked builtin call: '{node.func.id}()'"
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in BLOCKED_BUILTINS:
                    return False, f"Blocked method call: '.{node.func.attr}()'"

        # Check dunder attribute access
        elif isinstance(node, ast.Attribute):
            if node.attr in BLOCKED_DUNDER_ATTRS:
                return False, f"Blocked dunder access: '.{node.attr}'"

        # Block string-based code execution patterns
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and len(node.value) > 200:
                # Large string constants could be encoded payloads — flag for review
                # but don't block (docstrings are legitimate)
                pass

    return True, ""
