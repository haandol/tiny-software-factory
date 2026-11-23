"""코드 지표: 파일 길이, 함수 길이, 분기 복잡도. 코드가 늘어날수록 유지보수가 어려워지는
긴장 관계를 재는 정적 검증이다."""

from __future__ import annotations

import ast
from pathlib import Path

EXCLUDED = {".factory", ".git", ".venv", "vendor", "static", "data", "node_modules", "__pycache__", "var", "product"}
BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.comprehension,
            ast.match_case, ast.Assert)
LONG_FUNCTION = 60


def _code_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def _complexity(fn: ast.AST) -> int:
    n = 1
    for node in ast.walk(fn):
        if isinstance(node, BRANCHES):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
    return n


def measure_code(root: Path) -> dict:
    """root 아래 Python 파일의 코드 지표. 작업 공간 자료, 의존성, 데이터 폴더는 뺀다."""
    files = [p for p in sorted(root.rglob("*.py"))
             if not any(part in EXCLUDED for part in p.relative_to(root).parts)]
    m = {"code.files": len(files), "code.total_lines": 0, "code.max_file_lines": 0, "code.max_file": None,
         "code.max_function_lines": 0, "code.max_function": None, "code.long_functions": 0,
         "code.max_complexity": 0, "code.max_complexity_function": None}
    for f in files:
        text = f.read_text(errors="replace")
        rel = str(f.relative_to(root))
        lines = _code_lines(text)
        m["code.total_lines"] += lines
        if lines > m["code.max_file_lines"]:
            m["code.max_file_lines"], m["code.max_file"] = lines, rel
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = node.end_lineno - node.lineno + 1
            name = f"{rel}:{node.name}"
            m["code.long_functions"] += length > LONG_FUNCTION
            if length > m["code.max_function_lines"]:
                m["code.max_function_lines"], m["code.max_function"] = length, name
            c = _complexity(node)
            if c > m["code.max_complexity"]:
                m["code.max_complexity"], m["code.max_complexity_function"] = c, name
    return m
