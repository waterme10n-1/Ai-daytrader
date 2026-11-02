"""Claude-research pr. batch, med regelbaseret fallback når nøglen mangler eller kaldet fejler."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from trader.config import RULES_CONFIDENCE, Settings
from trader.signals import Candidate

logger = logging.getLogger("trader.research")

SETUP_DA = {"orb": "åbningsudbrud", "close": "lukkemomentum"}


class VerdictModel(BaseModel):
    symbol: str
    decision: Literal["take", "skip"]
    confidence: int = Field(ge=0, le=100)
    tighter_stop: float | None = None
    reasoning: str


class ResearchOutput(BaseModel):
    market_summary: str
    regime: Literal["trending_up", "trending_down", "choppy", "event_risk"]
    key_events_today: list[str]
    verdicts: list[VerdictModel]


@dataclass
class Verdict:
    symbol: str
    decision: str
    confidence: int
    tighter_stop: float | None
    reasoning: str


@dataclass
class ResearchBatch:
    market_summary: str
    regime: str
    key_events_today: list[str]
    verdicts: list[Verdict]
    source: str
    fallback: bool

    def for_symbol(self, symbol: str) -> Verdict:
        for verdict in self.verdicts:
            if verdict.symbol == symbol:
                return verdict
        return Verdict(
            symbol=symbol,
            decision="skip",
            confidence=0,
            tighter_stop=None,
            reasoning="Ingen vurdering af symbolet. Det springes over.",
        )


SYSTEM_PROMPT = """Du er research-assistent for en mekanisk daytrader i likvide amerikanske aktier og ETF'er.
Du handler ikke selv og du må ikke løsne et stop, hæve en positionsstørrelse eller tilsidesætte risiko.

Opgave:
- Brug web-søgning på dagens amerikanske økonomiske kalender: CPI, PPI, beskæftigelse, FOMC, Fed-talere, BNP og detailsalg.
- Søg også efter regnskaber og væsentlige nyheder for kandidaternes symboler.
- Vurder regimet: trending_up, trending_down, choppy eller event_risk.
- For hver kandidat: decision "take" eller "skip", confidence 0-100, valgfrit tighter_stop, og reasoning på dansk i 2-4 sætninger.

Skip er det normale svar. Skip ved høj-impact begivenhed inden for den korte holdetid, regnskab i dag, nyheder der modsiger setuppet, eller et hakkende marked.
tighter_stop må kun være et strammere stop på den rigtige side af indgangen. Udelad feltet hvis du ikke strammer.
market_summary skal være på dansk. Opfind ikke kurser.
"""


def _regime(bias: int) -> str:
    if bias > 0:
        return "trending_up"
    if bias < 0:
        return "trending_down"
    return "choppy"


def _payload(moment: datetime, window: str, candidates: list[Candidate], spy: dict, news: list[dict]) -> dict:
    return {
        "time": moment.isoformat(timespec="minutes"),
        "window": window,
        "spy": spy,
        "candidates": [
            {
                "symbol": item.symbol,
                "side": item.side,
                "setup": item.setup,
                "entry": item.entry,
                "stop": item.stop,
                "target": item.target,
                "gap_pct": round(item.gap_pct, 5),
                "change_from_open_pct": round(item.change_from_open_pct, 5),
                "vs_vwap_pct": round(item.vs_vwap_pct, 5),
                "trend_20d_pct": round(item.trend_20d_pct, 5),
                "adr_pct": round(item.adr_pct, 5),
                "or_high": item.or_high,
                "or_low": item.or_low,
                "or_mid": item.or_mid,
                "vwap": round(item.vwap, 4),
            }
            for item in candidates
        ],
        "news_last_18h": news,
    }


def rules_only(moment: datetime, window: str, candidates: list[Candidate], spy: dict) -> ResearchBatch:
    bias = int(spy.get("bias") or 0)
    regime = _regime(bias)
    summary = (
        "Regelbaseret fallback uden Claude. Beslutningerne følger kun de mekaniske "
        f"åbnings- og lukkeregler med fast sikkerhed på {RULES_CONFIDENCE}. "
        "Der er ikke søgt i nyheder eller kalender, og resultatet er ikke en modelvurdering."
    )
    verdicts: list[Verdict] = []
    for item in candidates:
        name = SETUP_DA.get(item.setup, item.setup)
        side = "lang" if item.side == "long" else "kort"
        verdicts.append(
            Verdict(
                symbol=item.symbol,
                decision="take",
                confidence=RULES_CONFIDENCE,
                tighter_stop=None,
                reasoning=(
                    f"Reglerne godkender det {side}e {name} i {item.symbol}. "
                    "Signalet opfylder de mekaniske krav til kurs, VWAP og markedsbias. "
                    f"Sikkerheden er fastsat til {RULES_CONFIDENCE}, fordi der ikke er en Claude-vurdering."
                ),
            )
        )
    return ResearchBatch(
        market_summary=summary,
        regime=regime,
        key_events_today=["Ingen kalenderkontrol i regelbaseret tilstand."],
        verdicts=verdicts,
        source="rules",
        fallback=True,
    )


def _content_param(response) -> list:
    blocks = []
    for block in getattr(response, "content", []) or []:
        if hasattr(block, "model_dump"):
            blocks.append(block.model_dump(mode="json", exclude_none=True))
        else:
            blocks.append(block)
    return blocks


def _container_id(response) -> str | None:
    container = getattr(response, "container", None)
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get("id")
    return getattr(container, "id", None)


def _refusal(response) -> bool:
    if getattr(response, "stop_reason", None) == "refusal":
        return True
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "refusal":
            return True
    return False


def _from_output(output: ResearchOutput, candidates: list[Candidate], *, fallback: bool) -> ResearchBatch:
    by_symbol = {item.symbol: item for item in output.verdicts}
    verdicts: list[Verdict] = []
    for candidate in candidates:
        item = by_symbol.get(candidate.symbol)
        if item is None:
            verdicts.append(
                Verdict(
                    symbol=candidate.symbol,
                    decision="skip",
                    confidence=0,
                    tighter_stop=None,
                    reasoning="Claude afgav ingen vurdering af symbolet. Det springes over.",
                )
            )
            continue
        verdicts.append(
            Verdict(
                symbol=candidate.symbol,
                decision=item.decision,
                confidence=int(item.confidence),
                tighter_stop=item.tighter_stop,
                reasoning=item.reasoning.strip(),
            )
        )
    return ResearchBatch(
        market_summary=output.market_summary.strip(),
        regime=output.regime,
        key_events_today=list(output.key_events_today),
        verdicts=verdicts,
        source="claude",
        fallback=fallback,
    )


def _skipped_refusal(candidates: list[Candidate]) -> ResearchBatch:
    return ResearchBatch(
        market_summary="Claude afviste forespørgslen. Alle kandidater springes over.",
        regime="event_risk",
        key_events_today=[],
        verdicts=[
            Verdict(
                symbol=item.symbol,
                decision="skip",
                confidence=0,
                tighter_stop=None,
                reasoning="Modellen afviste at vurdere handlen. Risikomotoren får derfor ingen godkendelse.",
            )
            for item in candidates
        ],
        source="claude",
        fallback=False,
    )


def _call_claude(settings: Settings, payload: dict, candidates: list[Candidate]) -> ResearchBatch:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    tool = {
        "type": settings.anthropic_web_search_tool,
        "name": "web_search",
        "max_uses": settings.anthropic_max_search_uses,
    }
    messages: list[dict] = [
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    container: str | None = None
    response = None
    # pause_turn: send assistentens indhold uændret tilbage, med samme værktøjer, indtil turen er færdig.
    for _attempt in range(6):
        kwargs: dict = {
            "model": settings.anthropic_model,
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": [tool],
            "output_format": ResearchOutput,
            "output_config": {"effort": settings.anthropic_effort},
            "fallbacks": settings.anthropic_fallbacks,
            "betas": ["server-side-fallback-2026-07-01"],
            "timeout": 120.0,
        }
        if container:
            kwargs["container"] = container
        response = client.beta.messages.parse(**kwargs)
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages.append({"role": "assistant", "content": _content_param(response)})
        container = _container_id(response) or container
    if response is None:
        raise RuntimeError("Intet svar fra Claude.")
    if getattr(response, "stop_reason", None) == "pause_turn":
        raise RuntimeError("Claude blev ved med at pause efter flere fortsættelser.")
    if _refusal(response):
        return _skipped_refusal(candidates)
    parsed = getattr(response, "parsed_output", None)
    if not isinstance(parsed, ResearchOutput):
        raise RuntimeError("Claude returnerede ikke et gyldigt research-svar.")
    return _from_output(parsed, candidates, fallback=False)


def analyze(
    settings: Settings,
    moment: datetime,
    window: str,
    candidates: list[Candidate],
    spy: dict,
    news: list[dict],
    *,
    rules_only_mode: bool,
) -> ResearchBatch:
    if rules_only_mode or not settings.anthropic_api_key:
        return rules_only(moment, window, candidates, spy)
    payload = _payload(moment, window, candidates, spy, news)
    try:
        return _call_claude(settings, payload, candidates)
    except Exception as exc:
        logger.warning("Claude-kald fejlede, bruger regler: %s", exc)
        batch = rules_only(moment, window, candidates, spy)
        batch.market_summary = (
            f"Claude fejlede ({exc.__class__.__name__}), så analysen faldt tilbage til regler. "
            + batch.market_summary
        )
        batch.fallback = True
        batch.source = "rules"
        return batch
