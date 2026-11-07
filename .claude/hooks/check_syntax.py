#!/usr/bin/env python3
"""PostToolUse-hook: tjekker syntaksen i den fil, Claude lige har ændret.

- .py-filer kompileres (uden at skrive .pyc).
- trader/static/*.html: hver inline <script> tjekkes med `node --check`, så en
  syntaksfejl, der ville efterlade siden hængende på "indlæser", fanges med det samme.
  Mangler node, springes JavaScript-tjekket over.

Exit 2 sender fejlen tilbage til Claude. Alt andet er stille.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def check_python(path: Path) -> str:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:
        return f"Syntaksfejl i {path}:{exc.lineno}:{exc.offset or 0}: {exc.msg}\n{(exc.text or '').rstrip()}"
    return ""


def check_html(path: Path) -> str:
    node = shutil.which("node")
    if not node:
        return ""
    html = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for match in SCRIPT_RE.finditer(html):
        body = match.group(1)
        if not body.strip():
            continue
        start = match.start(1)
        line = html.count("\n", 0, start)
        column = start - (html.rfind("\n", 0, start) + 1)
        # Polstring, så node melder samme linje og kolonne som i HTML-filen.
        padded = "\n" * line + " " * column + body
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(padded)
            tmp_path = tmp.name
        try:
            result = subprocess.run([node, "--check", tmp_path], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            continue
        finally:
            os.unlink(tmp_path)
        if result.returncode != 0:
            lines = (result.stderr or result.stdout).replace(tmp_path, str(path)).strip().splitlines()
            message = "\n".join(line for line in lines if not line.startswith("    at ") and not line.startswith("Node.js v")).strip()
            problems.append(f"JavaScript-syntaksfejl i {path}:\n{message}")
    return "\n\n".join(problems)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    tool_input = data.get("tool_input") or {}
    raw = tool_input.get("file_path") or (data.get("tool_response") or {}).get("filePath") or ""
    if not raw:
        return 0
    path = Path(raw)
    if not path.is_file():
        return 0
    if path.suffix == ".py":
        problem = check_python(path)
    elif path.suffix == ".html" and path.parent.name == "static" and path.parent.parent.name == "trader":
        problem = check_html(path)
    else:
        return 0
    if problem:
        print(problem, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
