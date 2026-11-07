#!/usr/bin/env python3
"""Stop-hook: kør testene, før Claude afslutter, hvis Python-koden er ændret.

Ændret betyder: forskel i en .py-fil mellem arbejdsmappen og det sted, grenen
startede fra main (inkl. commits, der ikke er merget endnu, og nye filer).
Er testene allerede bestået for præcis den kode, springes de over, så et
spørgsmål uden kodeændringer ikke koster 15 sekunder.

Fejler testene, sendes de sidste linjer tilbage til Claude (exit 2), så den
retter dem i stedet for at stoppe. Det sker højst én gang i træk
(stop_hook_active), så en hook aldrig kan holde Claude fanget.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
TIMEOUT_SECONDS = 240


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def base_commit() -> str:
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = git("merge-base", "HEAD", ref).strip()
        if base:
            return base
    return "HEAD"


def changed_python(base: str) -> list[str]:
    files = set(git("diff", "--name-only", base).split())
    files.update(git("ls-files", "--others", "--exclude-standard").split())
    return sorted(name for name in files if name.endswith(".py"))


def fingerprint(base: str, files: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(git("rev-parse", "HEAD").encode())
    digest.update(git("diff", base, "--", "*.py").encode())
    for name in files:
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_file() -> Path:
    key = hashlib.sha1(str(ROOT).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"ai-daytrader-tests-{key}.txt"


def python() -> str:
    for candidate in (ROOT / ".venv" / "bin" / "python", ROOT / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}
    if data.get("stop_hook_active"):
        return 0
    base = base_commit()
    files = changed_python(base)
    if not files:
        return 0
    stamp = fingerprint(base, files)
    cache = cache_file()
    if cache.is_file() and cache.read_text().strip() == stamp:
        return 0
    py = python()
    if subprocess.run([py, "-c", "import pytest"], capture_output=True).returncode != 0:
        print("Python-filer er ændret, men pytest er ikke installeret. Kør `pip install -r requirements.txt` og derefter `pytest`.", file=sys.stderr)
        return 2
    try:
        result = subprocess.run(
            [py, "-m", "pytest", "-q", "-x", "--no-header"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"Testene tog mere end {TIMEOUT_SECONDS} sekunder. Kør `pytest` og se, hvad der hænger.", file=sys.stderr)
        return 2
    if result.returncode == 0:
        cache.write_text(stamp)
        return 0
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-60:])
    print(f"Testene fejler efter ændringer i {', '.join(files[:8])}. Ret dem, før du stopper:\n\n{tail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
