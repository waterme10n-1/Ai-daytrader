"""Bordet lærer af sine fejl.

Hver markering med indgang, stop og mål får et udfald: det rigtige resultat, hvis den blev købt, eller
skyggeresultatet (hvad der ville være sket), hvis den blev afvist. Udfaldene sorteres i mønstre. Har et mønster
tabt mindst LEARN_MIN_TRADES gange med højst en tredjedel vindere, bliver det en lært regel, og nye køb, der
passer på mønstret, får WAIT. Reglen slår selv fra igen, når mønstret begynder at vinde.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

LEARN_MIN_TRADES = 3
LEARN_MAX_WIN_RATE = 1 / 3
MAX_LESSONS = 30


def _flag_hour(flag: dict) -> int | None:
    try:
        return datetime.fromisoformat(str(flag.get("ts"))).hour
    except (TypeError, ValueError):
        return None


def _late(flag: dict) -> bool:
    """Under to timer til lukning. Ældre markeringer uden minuttal: efter kl. 14 (New York lukker kl. 16)."""
    left = flag.get("to_close_min")
    if left is not None:
        try:
            return int(left) <= 120
        except (TypeError, ValueError):
            return False
    return (_flag_hour(flag) or 0) >= 14


# (nøgle, tekst, test på flag/news/chart)
PATTERNS: tuple[tuple[str, str, Callable[[dict, dict, dict], bool]], ...] = (
    ("far_above_vwap", "Køb mere end 1,5 % over VWAP", lambda f, n, c: float(f.get("vs_vwap_pct") or 0) > 1.5),
    ("big_move", "Aktier, der allerede er steget 5 % eller mere på en time", lambda f, n, c: float(f.get("move_pct") or 0) >= 5),
    ("late_day", "Køb under to timer før lukning", lambda f, n, c: _late(f)),
    ("volume_only", "Markeringer kun på volumen, uden kursbevægelse", lambda f, n, c: not f.get("by_move")),
    ("headline_only", "Køb, hvor grunden kun var en overskrift (ikke Claude)", lambda f, n, c: n.get("source") == "rules"),
    ("wide_stop", "Køb med stop mere end 1,5 % væk", lambda f, n, c: float(c.get("stop_pct") or 0) > 1.5),
    ("gap_open", "Gaps købt ved åbningen", lambda f, n, c: bool(f.get("by_gap"))),
)


def outcome(row: dict) -> dict | None:
    """Rigtigt resultat for en lukket handel, ellers skyggeresultatet. None, hvis der ikke er noget at lære af."""
    if row.get("status") == "closed" and row.get("pnl") is not None and row.get("fill_price"):
        fill = float(row["fill_price"])
        exit_price = float(row.get("exit_price") or fill)
        pct = (exit_price - fill) / fill * 100.0 if fill else 0.0
        return {"win": float(row["pnl"]) > 0, "pct": pct, "real": True}
    shadow = row.get("shadow")
    if shadow and shadow.get("result") in ("win", "loss"):
        return {"win": shadow["result"] == "win", "pct": float(shadow.get("pct") or 0), "real": False}
    return None


def evaluate(rows: list[dict]) -> list[dict]:
    """Statistik pr. mønster og om reglen er slået til."""
    result = []
    for key, label, test in PATTERNS:
        hits = []
        for row in rows:
            done = outcome(row)
            if done is None:
                continue
            try:
                matched = test(row.get("flag") or {}, row.get("news") or {}, row.get("chart") or {})
            except (TypeError, ValueError):
                matched = False
            if matched:
                hits.append(done)
        wins = sum(1 for item in hits if item["win"])
        n = len(hits)
        win_rate = wins / n if n else None
        avg = sum(item["pct"] for item in hits) / n if n else None
        active = n >= LEARN_MIN_TRADES and win_rate is not None and win_rate <= LEARN_MAX_WIN_RATE + 1e-9 and (avg or 0) < 0
        result.append(
            {
                "key": key,
                "label": label,
                "n": n,
                "wins": wins,
                "real": sum(1 for item in hits if item["real"]),
                "win_rate": round(win_rate, 3) if win_rate is not None else None,
                "avg_pct": round(avg, 2) if avg is not None else None,
                "active": active,
            }
        )
    return result


def blocking(learned: list[dict], flag: dict, news: dict, chart: dict) -> list[dict]:
    """De lærte regler, et nyt køb rammer."""
    tests = {key: test for key, _label, test in PATTERNS}
    hits = []
    for rule in learned:
        if not rule["active"]:
            continue
        try:
            if tests[rule["key"]](flag or {}, news or {}, chart or {}):
                hits.append(rule)
        except (TypeError, ValueError, KeyError):
            continue
    return hits


def rule_text(rule: dict) -> str:
    return f"{rule['label']} har tabt {rule['n'] - rule['wins']} af {rule['n']} gange (snit {rule['avg_pct']:+.1f}%)."


def day_lessons(rows_today: list[dict], before: list[dict], after: list[dict]) -> list[str]:
    """Faste lektioner fra dagens handler og regler, der slog til eller fra."""
    lessons: list[str] = []
    was = {rule["key"] for rule in before if rule["active"]}
    now = {rule["key"] for rule in after if rule["active"]}
    for rule in after:
        if rule["key"] in now - was:
            lessons.append("Ny lært regel: " + rule_text(rule) + " Den slags køb får WAIT fremover.")
        if rule["key"] in was - now:
            lessons.append(f"Reglen \"{rule['label']}\" er slået fra igen, fordi mønstret nu vinder oftere.")
    for row in rows_today:
        done = outcome(row)
        if done is None or not done["real"] or done["win"]:
            continue
        flag = row.get("flag") or {}
        lessons.append(
            f"{row['symbol']} tabte {done['pct']:+.1f}% ({row.get('exit_reason') or 'lukket'}). Den var steget "
            f"{float(flag.get('move_pct') or 0):+.1f}% på en time og lå {float(flag.get('vs_vwap_pct') or 0):.1f}% over VWAP, da vi købte."
        )
    missed = [row for row in rows_today if (outcome(row) or {}).get("win") and not (outcome(row) or {}).get("real")]
    if missed:
        names = ", ".join(row["symbol"] for row in missed[:4])
        lessons.append(f"Afvist, men ville have vundet: {names}. Tjek om SKEPTIC er for streng på den slags.")
    return lessons


def add_lessons(existing: list[dict], day: str, texts: list[str]) -> list[dict]:
    fresh = [{"day": day, "text": text} for text in texts if text]
    return (fresh + list(existing or []))[:MAX_LESSONS]


def for_prompt(lessons: list[dict], learned: list[dict]) -> dict[str, Any]:
    return {
        "lessons": [item["text"] for item in (lessons or [])[:8]],
        "learned_rules": [rule_text(rule) for rule in learned if rule["active"]],
    }
