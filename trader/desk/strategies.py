"""Strategi-biblioteket: kendte daytrading-metoder omsat til regler, der kan testes på rigtige kurser.

Vigtigt om hvad det her ER og IKKE er:
- Hver strategi er bottens egen, forenklede fortolkning af en offentligt beskrevet metode. Det er ikke personens egen,
  nøjagtige metode, og appen påstår ikke, at personen handler sådan i dag.
- Appen indeholder ingen verificerede resultater for personerne. De eneste tal, den viser, er dem den selv måler
  (backtest og papirturnering), og de står adskilt fra hinanden.
- Detektorerne bruger kun data til og med den seneste afsluttede bar (ingen look-ahead).

Alt her er rene funktioner uden netværk, så det kan testes og køres på historik.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from trader.desk.alpaca import Bar

MIN_BARS = 16  # tidligste bar, en detektor må kigge på
MIN_PRICE = 1.0  # under 1 $ er tick-størrelsen for grov til procentstop
DEFAULT_RR = 2.0
STOP_CAP_PCT = 3.0
MIN_STOP_PCT = 0.25


# --------------------------------------------------------------------------- bibliotek


@dataclass(frozen=True)
class StrategyDef:
    id: str
    name: str
    origin: str  # hvem metoden er inspireret af
    source: str  # hvad der er dokumenteret, og hvor
    description: str
    how: str
    markets: str
    timeframes: str
    entry: str
    exit: str
    stop: str
    target: str
    rr: str
    side: str  # long | short
    families: tuple[str, ...]  # hvilke underliggende signaler strategien bygger på (til uafhængige bekræftelser)
    suits: tuple[str, ...]  # markedsforhold, den passer til (tom = alle)
    avoid: tuple[str, ...]  # markedsforhold, den ikke bruges i
    weakness: str
    executable: bool = True  # kan bordet udføre den? Bordet køber kun (ingen short), så short-strategier kun måles
    combo: tuple[str, ...] = ()  # for kombinerede strategier: de metoder den bygger på

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["families"] = list(self.families)
        data["suits"] = list(self.suits)
        data["avoid"] = list(self.avoid)
        data["combo"] = list(self.combo)
        return data


_SOURCE_NOTE = "Resultater for personen er ikke verificeret af appen."

LIBRARY: dict[str, StrategyDef] = {s.id: s for s in (
    StrategyDef(
        "gap_and_go", "Gap and Go", "Ross Cameron (Warrior Trading)",
        "Offentligt undervisningsmateriale om momentum i aktier med gap, høj relativ volumen og nyheder. " + _SOURCE_NOTE,
        "Køber en aktie, der åbner med et stærkt gap, handler på høj relativ volumen og bryder over åbningens første højde.",
        "Gappet og volumen viser, at mange vil have aktien. Brud over åbningsrangen viser, at køberne vinder.",
        "Aktier, især små og mellemstore med nyhed", "1 minut, første time",
        "Gap på mindst 4 %, relativ volumen mindst 3, kursen over VWAP og et nyt brud over de første 5 minutters højeste kurs.",
        "Stop eller mål. Alt lukkes før børsen lukker.", "Under laveste kurs de sidste 5 minutter (højst 3 % væk).", "2 gange risikoen.",
        "Mindst 2:1", "long", ("volume_momentum", "range_structure", "vwap"), ("strong_momentum", "bullish"), ("bearish", "low_liquidity"),
        "Gaps falder ofte tilbage efter åbningen. Float og nyheder kan appen ikke se, så filteret er grovere end metoden.",
    ),
    StrategyDef(
        "orb", "Opening Range Breakout", "Andrew Aziz (Bear Bull Traders)",
        "Beskrevet i bogen 'How to Day Trade for a Living' og i Bear Bull Traders' undervisning. " + _SOURCE_NOTE,
        "Handler et brud ud af de første 15 minutters handelsrange i retning af trenden.",
        "Åbningsrangen er dagens første kamp mellem købere og sælgere. Et brud med volumen viser, hvem der vandt.",
        "Likvide aktier", "1 minut, efter de første 15 minutter",
        "Lukkekurs over åbningsrangens højeste kurs (første brud) med relativ volumen mindst 1,5.",
        "Stop eller mål. Alt lukkes før børsen lukker.", "Under åbningsrangens laveste kurs (højst 3 % væk).", "2 gange risikoen.",
        "Mindst 2:1", "long", ("range_structure", "volume_momentum"), ("strong_momentum", "high_vol", "bullish"), ("low_liquidity",),
        "Falske brud i en sidelæns dag. Virker dårligt, når åbningsrangen er meget bred.",
    ),
    StrategyDef(
        "vwap_bounce", "VWAP Bounce", "Andrew Aziz (Bear Bull Traders)",
        "VWAP som støtte i en optrend er en gennemgående del af Bear Bull Traders' metode. " + _SOURCE_NOTE,
        "Køber, når en aktie i en optrend falder tilbage til VWAP og vender op igen.",
        "VWAP er institutionernes gennemsnitspris. Holder den, er køberne stadig i kontrol.",
        "Likvide aktier i trend", "1 minut",
        "Mest af de seneste 20 lukkekurser over VWAP, kursen rørte VWAP de sidste 4 minutter og lukker grøn over VWAP og 9-EMA.",
        "Stop eller mål.", "Under laveste kurs de sidste 4 minutter.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("vwap", "price_action"), ("bullish", "strong_momentum"), ("bearish",),
        "I en sidelæns dag er VWAP kun et støjniveau. Et brud under VWAP gør setuppet ugyldigt.",
    ),
    StrategyDef(
        "vwap_reclaim", "VWAP Reclaim", "Andrew Aziz (Bear Bull Traders)",
        "Gen-erobring af VWAP efter en periode under den er et kendt setup i Bear Bull Traders' materiale. " + _SOURCE_NOTE,
        "Køber, når kursen efter en tid under VWAP lukker over den med øget volumen.",
        "Skiftet fra under til over VWAP viser, at kontrollen skifter til køberne.",
        "Likvide aktier", "1 minut",
        "Mindst 8 af de 12 forrige lukkekurser under VWAP, nu en lukkekurs over VWAP og volumen over 1,3 gange snittet.",
        "Stop eller mål.", "Under laveste kurs de sidste 8 minutter (højst 3 % væk).", "2 gange risikoen.", "Mindst 2:1", "long",
        ("vwap", "volume_momentum"), (), ("bearish",),
        "Mange gen-erobringer fejler. Kræver at markedet som helhed ikke falder.",
    ),
    StrategyDef(
        "hod_breakout", "High of Day Breakout", "Ross Cameron (Warrior Trading)",
        "Brud over dagens højeste kurs efter en konsolidering er en kendt momentum-indgang i Warrior Tradings materiale. " + _SOURCE_NOTE,
        "Køber et brud over dagens højeste kurs, når aktien har holdt sig stram lige under den.",
        "En stram konsolidering under toppen viser, at sælgerne er løbet tør.",
        "Aktier i dagens bevægelse", "1 minut",
        "Lukkekurs over dagens højeste kurs, en stram range de sidste 8 minutter (højst 1,5 %), relativ volumen mindst 1,5 og aktien op mindst 2 % på dagen.",
        "Stop eller mål.", "Under laveste kurs i konsolideringen.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("range_structure", "volume_momentum"), ("strong_momentum", "bullish"), ("bearish", "low_liquidity"),
        "Brud over toppen er ofte et sent køb. Mange topper bliver til falske brud.",
    ),
    StrategyDef(
        "bull_flag", "Bull Flag", "Ross Cameron (Warrior Trading)",
        "Bull flag (flagstang og flag) er et grundmønster i momentum-undervisning, bl.a. hos Warrior Trading. " + _SOURCE_NOTE,
        "Køber et brud over et lille flag, der følger efter en kraftig stigning.",
        "Stangen viser købertryk. Et flag med faldende volumen viser, at ingen sælger aggressivt.",
        "Aktier i momentum", "1 minut",
        "Stang på mindst 3 % på højst 12 minutter, derefter 3-10 minutters flag, der giver højst halvdelen tilbage på lavere volumen, og et brud over flagets top.",
        "Stop eller mål.", "Under flagets laveste kurs.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("price_action", "volume_momentum"), ("strong_momentum", "bullish"), ("bearish",),
        "Mønstre defineres lidt forskelligt af hver trader, så reglen her er bottens egen.",
    ),
    StrategyDef(
        "bear_flag", "Bear Flag (short)", "Mønster-spejling af bull flag",
        "Spejlbillede af bull flag-mønstret, kendt fra almindelig teknisk analyse. " + _SOURCE_NOTE,
        "Shorter et brud under et lille flag, der følger efter et kraftigt fald.",
        "Et svagt opspring efter et hårdt fald viser, at sælgerne stadig har kontrollen.",
        "Aktier i fald", "1 minut", "Fald på mindst 3 %, et flag der giver højst halvdelen tilbage, og et brud under flagets bund.",
        "Stop eller mål.", "Over flagets højeste kurs.", "2 gange risikoen.", "Mindst 2:1", "short",
        ("price_action", "volume_momentum"), ("bearish", "strong_momentum"), ("bullish",),
        "Bordet kan ikke short-sælge. Strategien måles kun i backtest og turnering. Short koster lånerente og kan have uendeligt tab.",
        executable=False,
    ),
    StrategyDef(
        "momentum_breakout", "Momentum Breakout", "Tim Grittani / Ross Cameron (momentum i små aktier)",
        "Momentumhandel i aktier med høj relativ volumen er genstand for begges offentlige undervisning. Detaljer er ikke verificeret af appen.",
        "Køber et brud over de seneste 20 minutters højeste kurs med høj relativ volumen og en stigende trend.",
        "Højere toppe på voksende volumen er selve definitionen på momentum.",
        "Aktier med volumen", "1 minut",
        "Lukkekurs over de seneste 20 minutters top, relativ volumen mindst 2, 9-EMA over 20-EMA, kursen over VWAP og RSI under 85.",
        "Stop eller mål.", "Under laveste kurs de sidste 5 minutter (højst 3 % væk).", "2 gange risikoen.", "Mindst 2:1", "long",
        ("volume_momentum", "ma_trend"), ("strong_momentum", "bullish"), ("bearish", "low_liquidity"),
        "Sent køb efter en stor stigning. Virker kun, når bevægelsen fortsætter.",
    ),
    StrategyDef(
        "parabolic_short", "Parabolic Short", "Steven Dux / Tim Grittani (parabolske short-setups)",
        "Short af overstrakte, parabolske bevægelser er et kendt tema i begges offentlige undervisning. Detaljer er ikke verificeret af appen.",
        "Shorter den første tydelige vending efter en overstrakt stigning langt over VWAP.",
        "Efter en parabolsk bevægelse mangler nye købere, og den første vending afslører det.",
        "Overstrakte små aktier", "1 minut",
        "Kursen mindst 8 % over VWAP, op mindst 20 % på dagen, RSI mindst 80 og en rød bar, der lukker under forrige bars laveste.",
        "Stop eller mål.", "Over dagens højeste kurs (højst 8 % væk).", "Tilbage til VWAP, mindst 1,5 gange risikoen.", "Mindst 1,5:1", "short",
        ("mean_reversion", "price_action"), ("high_vol",), ("strong_momentum",),
        "Short mod en stærk bevægelse er den farligste type handel: en aktie kan stige meget mere. Bordet kan ikke short-sælge, så den måles kun.",
        executable=False,
    ),
    StrategyDef(
        "failed_breakout", "Failed Breakout (short)", "Teknisk analyse (Al Brooks beskriver fejlede brud som reverseringer)",
        "Fejlede brud som reverserings-setup beskrives i Al Brooks' 'Trading Price Action'-serie. " + _SOURCE_NOTE,
        "Shorter, når et brud over et modstandsniveau mislykkes og kursen lukker tilbage under det.",
        "Købere, der blev fanget i bruddet, tvinges ud og skubber kursen ned.",
        "Likvide aktier", "1 minut",
        "Et brud over de forrige 30 minutters top de sidste 5 minutter, og nu en rød bar, der lukker tilbage under niveauet.",
        "Stop eller mål.", "Over bruddets højeste kurs.", "2 gange risikoen.", "Mindst 2:1", "short",
        ("range_structure", "price_action"), ("bearish", "high_vol"), ("strong_momentum",),
        "Bordet kan ikke short-sælge. Måles kun. Fejlede brud er kun fejlede, når de først er fejlet.",
        executable=False,
    ),
    StrategyDef(
        "reversal", "Reversal (udmattelse)", "Al Brooks (Price Action) / Linda Raschke",
        "Reverseringer ved udmattelse beskrives i Brooks' 'Trading Price Action Reversals' og i Raschke og Connors' 'Street Smarts'. " + _SOURCE_NOTE,
        "Køber en hammer-bar med stor volumen ved dagens bund efter et hårdt fald.",
        "Lang hale og stor volumen viser, at sælgerne er udmattede.",
        "Likvide aktier efter et fald", "1 minut",
        "Kursen ved dagens bund, en nedre hale på mindst 60 % af baren, volumen mindst 2 gange snittet, RSI højst 30 og en grøn lukkekurs.",
        "Stop eller mål.", "Under baren (højst 3 % væk).", "2 gange risikoen.", "Mindst 2:1", "long",
        ("price_action", "mean_reversion"), ("high_vol", "sideways"), ("strong_momentum",),
        "At fange en kniv, der falder. Mange reverseringer er kun pauser.",
    ),
    StrategyDef(
        "ma_breakout", "Moving Average Breakout", "Linda Raschke / Mark Minervini (trend med glidende gennemsnit)",
        "Glidende gennemsnit som trendfilter indgår i begges publicerede materiale ('Street Smarts', 'Trade Like a Stock Market Wizard'). " + _SOURCE_NOTE,
        "Køber, når kursen lukker op gennem det 50-minutters glidende gennemsnit, mens 20-EMA stiger.",
        "Brud op gennem et langsomt gennemsnit viser, at trenden skifter eller genoptages.",
        "Likvide aktier", "1 minut",
        "Lukkekurs over 50-EMA efter en lukkekurs under, 20-EMA stigende og relativ volumen mindst 1,2.",
        "Stop eller mål.", "Under 50-EMA minus en halv ATR.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("ma_trend",), ("bullish", "strong_momentum"), ("sideways", "bearish"),
        "I en sidelæns dag giver gennemsnittene mange falske signaler.",
    ),
    StrategyDef(
        "mean_reversion", "Mean Reversion til VWAP", "Linda Raschke (tilbagevending til gennemsnit)",
        "Kortsigtet tilbagevending fra overstrakte niveauer er et gennemgående tema i 'Street Smarts'. " + _SOURCE_NOTE,
        "Køber en kraftig overreaktion nedad og sigter mod VWAP.",
        "Priser, der strækker sig langt fra gennemsnittet, vender ofte tilbage, især i en sidelæns dag.",
        "Likvide aktier i range", "1 minut",
        "Kursen mindst 2 ATR under VWAP, RSI højst 25 og en grøn bar. Målet er VWAP.",
        "Mål ved VWAP eller stop.", "Under baren minus en halv ATR.", "VWAP (mindst 1,2 gange risikoen).", "Mindst 1,2:1", "long",
        ("mean_reversion",), ("sideways", "low_vol"), ("strong_momentum", "bearish"),
        "Mislykkes i en trend-dag: kursen kan blive ved at falde. Kræver et marked uden retning.",
    ),
    StrategyDef(
        "vcp", "Volatility Contraction Breakout", "Mark Minervini (VCP)",
        "Volatility Contraction Pattern er beskrevet i Minervinis bog 'Trade Like a Stock Market Wizard'. Han bruger det på daglige kurser; bottens udgave er en forenklet intradag-version. " + _SOURCE_NOTE,
        "Køber et brud, når udsvingene er blevet mindre og mindre.",
        "Faldende udsving viser, at udbuddet tørrer ud, før kursen bryder ud.",
        "Aktier i trend", "1 minut (forenklet)",
        "Tre 8-minutters vinduer med faldende range, lukkekurs over det sidste vindues top, relativ volumen mindst 1,3 og kursen over 20-EMA.",
        "Stop eller mål.", "Under det sidste vindues bund.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("range_structure", "ma_trend"), ("bullish", "low_vol"), ("bearish",),
        "Minervinis metode er dagligt. Intradag-versionen er bottens egen og ikke testet af ham.",
    ),
    StrategyDef(
        "price_action", "Price Action Trend Pullback", "Al Brooks (Trading Price Action)",
        "Trendbarer, pullbacks og signalbarer er kernen i Brooks' 'Trading Price Action'-serie. " + _SOURCE_NOTE,
        "Køber et brud over en signalbar efter en pullback i en trend med stærke grønne trendbarer.",
        "Mindst tre af de fem seneste barer er stærke trendbarer, og pullbacken er lille.",
        "Likvide aktier i trend", "1 minut",
        "Mindst 3 af de 5 seneste barer er grønne trendbarer (krop mindst 60 % af baren), højere bundkurser, en kort pullback-bar og en lukkekurs over den, over 20-EMA.",
        "Stop eller mål.", "Under pullback-barens bund.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("price_action",), ("bullish", "strong_momentum"), ("sideways", "bearish"),
        "Price action er en vurdering. Reglen her er en streng forenkling og fanger kun de tydeligste tilfælde.",
    ),
    StrategyDef(
        "ema_pullback", "20-EMA Pullback", "Linda Raschke ('Holy Grail'-setuppet i 'Street Smarts')",
        "Pullback til 20-EMA i en stærk trend er beskrevet i Raschke og Connors' 'Street Smarts'. Bottens udgave er forenklet. " + _SOURCE_NOTE,
        "Køber, når kursen i en stærk optrend rører 20-EMA og vender op.",
        "En stærk trend finder støtte ved gennemsnittet, så pullbacken er et billigere køb.",
        "Likvide aktier i trend", "1 minut",
        "20-EMA stigende over de seneste 20 minutter, kursen rørte 20-EMA de sidste 3 minutter, en grøn lukkekurs over 20-EMA og over VWAP.",
        "Stop eller mål.", "Under pullbackens laveste kurs.", "2 gange risikoen.", "Mindst 2:1", "long",
        ("ma_trend", "price_action"), ("bullish", "strong_momentum"), ("sideways", "bearish"),
        "Når trenden vender, er det første tegn en tabt pullback.",
    ),
    StrategyDef(
        "combo_trend_momentum", "Kombineret: momentum + VWAP + price action + trend", "Bottens egen kombination (Cameron, Aziz, Brooks og Minervini)",
        "Kombinationen er bottens egen konstruktion af fire offentligt beskrevne principper. Den er ikke noget, nogen af personerne anbefaler.",
        "Kræver, at fire forskellige slags beviser peger samme vej, før den køber.",
        "Momentum (Cameron) siger, at nogen vil have aktien. VWAP (Aziz) siger, at køberne har kontrollen. Price action (Brooks) siger, at barerne er rene. "
        "Dagstrenden (Minervini) siger, at aktien ikke kæmper mod en nedtrend. De fire bruger forskellige data, så de er uafhængige beviser, i modsætning til en blanding af indikatorer, der alle bygger på samme kurs.",
        "Likvide aktier med volumen", "1 minut + daglige kurser",
        "Alle fire: (1) brud over de seneste 20 minutters top med relativ volumen mindst 2, (2) kursen over VWAP, (3) mindst 3 af 5 barer er grønne trendbarer, "
        "(4) daglig lukkekurs over 20-dages gennemsnittet, der stiger.",
        "Stop eller mål.", "Under laveste kurs de sidste 5 minutter (højst 3 % væk).", "2 gange risikoen.", "Mindst 2:1", "long",
        ("volume_momentum", "vwap", "price_action", "daily_trend"), ("strong_momentum", "bullish"), ("bearish", "low_liquidity"),
        "Strengt filter giver få signaler. Den fanger ikke de tidlige dele af en bevægelse, og dagstrenden kræver mindst 20 daglige kurser.",
        combo=("gap_and_go", "vwap_bounce", "price_action", "vcp"),
    ),
)}

# --------------------------------------------------------------------------- indikatorer pr. dag


def _ema(values: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out: list[float] = []
    for value in values:
        out.append(value if not out else value * k + out[-1] * (1 - k))
    return out


class DayData:
    """Én dags kurser med alle indikatorer regnet på forhånd, så en detektor kan kigge på bar nr. i uden at regne alt igen."""

    def __init__(self, bars: list[Bar], daily: list[Bar] | None = None, session_minutes: int = 390) -> None:
        self.bars = bars
        self.daily = daily or []
        self.o = [b.open for b in bars]
        self.h = [b.high for b in bars]
        self.l = [b.low for b in bars]
        self.c = [b.close for b in bars]
        self.v = [b.volume for b in bars]
        n = len(bars)
        pv = vv = 0.0
        self.vwap: list[float] = []
        for b in bars:
            pv += (b.high + b.low + b.close) / 3.0 * b.volume
            vv += b.volume
            self.vwap.append(pv / vv if vv > 0 else b.close)
        self.ema9, self.ema20, self.ema50 = _ema(self.c, 9), _ema(self.c, 20), _ema(self.c, 50)
        tr = [self.h[0] - self.l[0]] + [max(self.h[i] - self.l[i], abs(self.h[i] - self.c[i - 1]), abs(self.l[i] - self.c[i - 1])) for i in range(1, n)]
        self.atr: list[float] = []
        for i, value in enumerate(tr):
            self.atr.append(value if i == 0 else (self.atr[-1] * 13 + value) / 14.0)
        self.rsi: list[float] = []
        gain = loss = 0.0
        for i in range(n):
            if i == 0:
                self.rsi.append(50.0)
                continue
            change = self.c[i] - self.c[i - 1]
            gain = (gain * 13 + max(change, 0.0)) / 14.0
            loss = (loss * 13 + max(-change, 0.0)) / 14.0
            self.rsi.append(100.0 if loss == 0 and gain > 0 else 50.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss))
        self.hod = [max(self.h[: i + 1]) for i in range(n)] if n < 200 else self._running(self.h, max)
        self.lod = [min(self.l[: i + 1]) for i in range(n)] if n < 200 else self._running(self.l, min)
        self.prev_close = self.daily[-1].close if self.daily and self.daily[-1].close > 0 else None
        self.gap_pct = (self.o[0] - self.prev_close) / self.prev_close * 100.0 if self.prev_close and n else None
        volumes = [b.volume for b in self.daily if b.volume > 0]
        self.norm_pm = (sum(volumes) / len(volumes)) / float(session_minutes) if volumes else 0.0
        closes = [b.close for b in self.daily]
        self.daily_trend_up: bool | None = None
        if len(closes) >= 20:
            sma_now = sum(closes[-20:]) / 20.0
            earlier = closes[-25:-5] if len(closes) >= 25 else closes[:20]
            sma_before = sum(earlier[-20:]) / len(earlier[-20:])
            self.daily_trend_up = closes[-1] > sma_now and sma_now >= sma_before

    @staticmethod
    def _running(values: list[float], fn: Callable) -> list[float]:
        out: list[float] = []
        for value in values:
            out.append(value if not out else fn(out[-1], value))
        return out

    def rvol(self, i: int, window: int = 15) -> float:
        if self.norm_pm <= 0 or i < 0:
            return 0.0
        part = self.v[max(0, i - window + 1): i + 1]
        return (sum(part) / len(part)) / self.norm_pm if part else 0.0

    def vol_avg(self, i: int, window: int = 20) -> float:
        part = self.v[max(0, i - window): i]
        return sum(part) / len(part) if part else 0.0


# --------------------------------------------------------------------------- signal


@dataclass
class Signal:
    strategy: str
    symbol: str
    side: str
    i: int
    ts: str
    price: float
    entry: float
    stop: float
    target: float
    rr: float
    quality: int
    reasons: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    rvol: float = 0.0
    gap_pct: float | None = None
    vs_vwap_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def _finish(strategy: str, d: DayData, i: int, side: str, raw_stop: float, required: list[str], bonus: list[tuple[str, bool]], *,
            target: float | None = None, rr: float = DEFAULT_RR, cap_pct: float = STOP_CAP_PCT, min_rr: float = 1.0, symbol: str = "") -> Signal | None:
    entry = d.c[i]
    if entry < MIN_PRICE:
        return None
    if side == "long":
        stop = max(raw_stop, entry * (1 - cap_pct / 100.0))
        stop = min(stop, entry * (1 - MIN_STOP_PCT / 100.0))
        risk = entry - stop
        tgt = target if target is not None else entry + rr * risk
        if risk <= 0 or tgt <= entry:
            return None
        reward = tgt - entry
    else:
        stop = min(raw_stop, entry * (1 + cap_pct / 100.0))
        stop = max(stop, entry * (1 + MIN_STOP_PCT / 100.0))
        risk = stop - entry
        tgt = target if target is not None else entry - rr * risk
        if risk <= 0 or tgt >= entry:
            return None
        reward = entry - tgt
    if reward / risk < min_rr - 1e-9:
        return None
    total = len(required) + len(bonus)
    passed = len(required) + sum(1 for _, ok in bonus if ok)
    checks = [{"label": text, "ok": True} for text in required] + [{"label": text, "ok": bool(ok)} for text, ok in bonus]
    level = d.vwap[i]
    return Signal(
        strategy=strategy, symbol=symbol, side=side, i=i, ts=d.bars[i].ts.isoformat(timespec="minutes"), price=_round(entry),
        entry=_round(entry), stop=_round(stop), target=_round(tgt), rr=round(reward / risk, 2),
        quality=int(round(100.0 * passed / total)) if total else 0, reasons=[c["label"] for c in checks if c["ok"]], checks=checks,
        rvol=round(d.rvol(i), 1), gap_pct=None if d.gap_pct is None else round(d.gap_pct, 2),
        vs_vwap_pct=round((entry - level) / level * 100.0, 2) if level else 0.0,
    )


def _body_bull(d: DayData, k: int) -> bool:
    rng = d.h[k] - d.l[k]
    return rng > 0 and d.c[k] > d.o[k] and (d.c[k] - d.o[k]) / rng >= 0.6 and (d.h[k] - d.c[k]) / rng <= 0.25


# --------------------------------------------------------------------------- detektorer (alle bruger kun bar 0..i)


def gap_and_go(d: DayData, i: int) -> Signal | None:
    if i < 6 or i > 75 or d.gap_pct is None or d.gap_pct < 4.0:
        return None
    top = max(d.h[:5])
    if not (d.c[i] > top and max(d.c[5:i]) <= top if i > 5 else False):
        return None
    if d.rvol(i) < 3.0 or d.c[i] <= d.vwap[i]:
        return None
    return _finish("gap_and_go", d, i, "long", min(d.l[i - 4: i + 1]) - 0.01,
                   [f"Gap +{d.gap_pct:.1f} % siden i går", f"Relativ volumen {d.rvol(i):.1f}", "Over VWAP", "Nyt brud over de første 5 minutters top"],
                   [("Gap over 8 %", d.gap_pct >= 8.0), ("Relativ volumen over 5", d.rvol(i) >= 5.0), ("9-EMA over 20-EMA", d.ema9[i] > d.ema20[i])])


def orb(d: DayData, i: int) -> Signal | None:
    if i < 16 or i > 120:
        return None
    high, low = max(d.h[:15]), min(d.l[:15])
    if not (d.c[i] > high and max(d.c[15:i], default=0.0) <= high):
        return None
    if d.rvol(i) < 1.5 and d.norm_pm > 0:
        return None
    width = (high - low) / high * 100.0
    return _finish("orb", d, i, "long", low - 0.01, ["Brud over de første 15 minutters top", "Første brud i dag"] + ([f"Relativ volumen {d.rvol(i):.1f}"] if d.norm_pm > 0 else []),
                   [("Over VWAP", d.c[i] > d.vwap[i]), ("Åbningsrange under 3 %", width < 3.0), ("9-EMA over 20-EMA", d.ema9[i] > d.ema20[i])])


def vwap_bounce(d: DayData, i: int) -> Signal | None:
    if i < 30:
        return None
    above = sum(1 for k in range(i - 20, i) if d.c[k] > d.vwap[k])
    if above < 14 or min(d.l[i - 3: i + 1]) > d.vwap[i] * 1.0015:
        return None
    if not (d.c[i] > d.o[i] and d.c[i] > d.vwap[i] and d.c[i] > d.ema9[i]):
        return None
    return _finish("vwap_bounce", d, i, "long", min(d.l[i - 3: i + 1]) - 0.01,
                   [f"{above} af 20 lukkekurser over VWAP", "Rørte VWAP og vendte op", "Grøn bar over VWAP og 9-EMA"],
                   [("Relativ volumen over 1", d.rvol(i) >= 1.0), ("20-EMA stigende", d.ema20[i] > d.ema20[i - 10])])


def vwap_reclaim(d: DayData, i: int) -> Signal | None:
    if i < 20 or d.c[i] <= d.vwap[i] or d.c[i - 1] > d.vwap[i - 1]:
        return None
    below = sum(1 for k in range(i - 12, i) if d.c[k] < d.vwap[k])
    avg = d.vol_avg(i)
    if below < 8 or avg <= 0 or d.v[i] < 1.3 * avg:
        return None
    return _finish("vwap_reclaim", d, i, "long", min(d.l[i - 7: i + 1]) - 0.01,
                   [f"{below} af 12 lukkekurser under VWAP", "Lukker nu over VWAP", f"Volumen {d.v[i] / avg:.1f} gange snittet"],
                   [("Grøn bar", d.c[i] > d.o[i]), ("Over 9-EMA", d.c[i] > d.ema9[i])])


def hod_breakout(d: DayData, i: int) -> Signal | None:
    if i < 30:
        return None
    prior = max(d.h[:i])
    if d.c[i] <= prior:
        return None
    window_high, window_low = max(d.h[i - 8: i]), min(d.l[i - 8: i])
    tight = (window_high - window_low) / d.c[i] * 100.0
    day = (d.c[i] - d.o[0]) / d.o[0] * 100.0 if d.o[0] else 0.0
    if tight > 1.5 or day < 2.0 or (d.norm_pm > 0 and d.rvol(i) < 1.5):
        return None
    return _finish("hod_breakout", d, i, "long", window_low - 0.01,
                   ["Lukker over dagens højeste kurs", f"Stram range {tight:.1f} %", f"Op {day:.1f} % på dagen"],
                   [("Relativ volumen over 3", d.rvol(i) >= 3.0), ("Over VWAP", d.c[i] > d.vwap[i]), ("9-EMA over 20-EMA", d.ema9[i] > d.ema20[i])])


def _flag(d: DayData, i: int, side: str) -> tuple[float, float] | None:
    """(flagets top eller bund, stangens størrelse i %) hvis der er et flag, ellers None."""
    for flag_len in range(3, 11):
        top_i = i - flag_len
        if top_i < 14:
            continue
        window = range(top_i - 12, top_i + 1)
        if side == "long":
            peak = d.h[top_i]
            if peak < max(d.h[top_i - 12: top_i + 1]):
                continue
            base = min(d.l[k] for k in window)
            pole = (peak - base) / base * 100.0
            flag_low = min(d.l[top_i + 1: i])
            if pole < 3.0 or (peak - flag_low) > 0.5 * (peak - base) or max(d.h[top_i + 1: i], default=0) > peak:
                continue
            pole_vol = sum(d.v[top_i - 8: top_i + 1]) / 9.0
            flag_vol = sum(d.v[top_i + 1: i]) / max(1, flag_len - 1)
            if flag_vol >= pole_vol:
                continue
            return max(d.h[top_i + 1: i]), pole
        floor = d.l[top_i]
        if floor > min(d.l[top_i - 12: top_i + 1]):
            continue
        base = max(d.h[k] for k in window)
        pole = (base - floor) / base * 100.0
        flag_high = max(d.h[top_i + 1: i])
        if pole < 3.0 or (flag_high - floor) > 0.5 * (base - floor) or min(d.l[top_i + 1: i], default=1e18) < floor:
            continue
        return min(d.l[top_i + 1: i]), pole
    return None


def bull_flag(d: DayData, i: int) -> Signal | None:
    if i < 16:
        return None
    found = _flag(d, i, "long")
    if found is None or d.c[i] <= found[0]:
        return None
    flag_top, pole = found
    low = min(d.l[i - 10: i])
    return _finish("bull_flag", d, i, "long", low - 0.01, [f"Stang på {pole:.1f} %", "Flag med faldende volumen, højst halvdelen givet tilbage", "Brud over flagets top"],
                   [("Over VWAP", d.c[i] > d.vwap[i]), ("Relativ volumen over 1,5", d.rvol(i) >= 1.5)])


def bear_flag(d: DayData, i: int) -> Signal | None:
    if i < 16:
        return None
    found = _flag(d, i, "short")
    if found is None or d.c[i] >= found[0]:
        return None
    high = max(d.h[i - 10: i])
    return _finish("bear_flag", d, i, "short", high + 0.01, [f"Fald på {found[1]:.1f} %", "Flag, højst halvdelen tilbage", "Brud under flagets bund"],
                   [("Under VWAP", d.c[i] < d.vwap[i]), ("Relativ volumen over 1,5", d.rvol(i) >= 1.5)])


def momentum_breakout(d: DayData, i: int) -> Signal | None:
    if i < 25:
        return None
    if not (d.c[i] > max(d.h[i - 20: i]) and d.ema9[i] > d.ema20[i] and d.c[i] > d.vwap[i] and d.rsi[i] < 85):
        return None
    if d.norm_pm > 0 and d.rvol(i) < 2.0:
        return None
    return _finish("momentum_breakout", d, i, "long", min(d.l[i - 4: i + 1]) - 0.01,
                   ["Brud over de seneste 20 minutters top", "9-EMA over 20-EMA", "Over VWAP", f"RSI {d.rsi[i]:.0f}"],
                   [(f"Relativ volumen {d.rvol(i):.1f} over 3", d.rvol(i) >= 3.0), ("Grøn trendbar", _body_bull(d, i))])


def parabolic_short(d: DayData, i: int) -> Signal | None:
    if i < 30 or d.vwap[i] <= 0:
        return None
    ext = (d.c[i] - d.vwap[i]) / d.vwap[i] * 100.0
    day = (d.hod[i] - d.o[0]) / d.o[0] * 100.0 if d.o[0] else 0.0
    if ext < 8.0 or day < 20.0 or d.rsi[i - 1] < 80.0:
        return None
    if not (d.c[i] < d.o[i] and d.c[i] < d.l[i - 1]):
        return None
    return _finish("parabolic_short", d, i, "short", d.hod[i] + 0.01, [f"{ext:.1f} % over VWAP", f"Op {day:.0f} % på dagen", f"RSI {d.rsi[i - 1]:.0f} før vendingen", "Rød bar under forrige bars bund"],
                   [("Volumen over snittet", d.v[i] > d.vol_avg(i)), ("Under 9-EMA", d.c[i] < d.ema9[i])], target=d.vwap[i], cap_pct=8.0, min_rr=1.5)


def failed_breakout(d: DayData, i: int) -> Signal | None:
    if i < 40:
        return None
    level = max(d.h[i - 30: i - 5])
    broke = [k for k in range(i - 5, i) if d.c[k] > level * 1.001]
    if not broke or not (d.c[i] < level and d.c[i] < d.o[i]):
        return None
    return _finish("failed_breakout", d, i, "short", max(d.h[i - 5: i + 1]) + 0.01, ["Brud over de forrige 30 minutters top", "Lukker nu tilbage under niveauet", "Rød bar"],
                   [("Under VWAP", d.c[i] < d.vwap[i]), ("Volumen over snittet", d.v[i] > d.vol_avg(i))])


def reversal(d: DayData, i: int) -> Signal | None:
    if i < 25:
        return None
    rng = d.h[i] - d.l[i]
    if rng <= 0 or d.l[i] > d.lod[i] * 1.003:
        return None
    wick = (min(d.o[i], d.c[i]) - d.l[i]) / rng
    avg = d.vol_avg(i)
    if wick < 0.6 or avg <= 0 or d.v[i] < 2.0 * avg or d.rsi[i] > 30.0 or d.c[i] <= d.o[i]:
        return None
    return _finish("reversal", d, i, "long", d.l[i] - 0.01, [f"Nedre hale {wick * 100:.0f} % af baren", f"Volumen {d.v[i] / avg:.1f} gange snittet", f"RSI {d.rsi[i]:.0f}", "Ved dagens bund, grøn bar"],
                   [("Mere end 1 ATR under VWAP", d.vwap[i] - d.c[i] > d.atr[i]), ("Volumen over 3 gange snittet", d.v[i] >= 3.0 * avg)])


def ma_breakout(d: DayData, i: int) -> Signal | None:
    if i < 55:
        return None
    if not (d.c[i] > d.ema50[i] and d.c[i - 1] <= d.ema50[i - 1] and d.ema20[i] > d.ema20[i - 5]):
        return None
    if d.norm_pm > 0 and d.rvol(i) < 1.2:
        return None
    return _finish("ma_breakout", d, i, "long", d.ema50[i] - 0.5 * d.atr[i], ["Lukker op gennem 50-EMA", "20-EMA stigende"] + ([f"Relativ volumen {d.rvol(i):.1f}"] if d.norm_pm > 0 else []),
                   [("Over VWAP", d.c[i] > d.vwap[i]), ("Grøn bar", d.c[i] > d.o[i])])


def mean_reversion(d: DayData, i: int) -> Signal | None:
    if i < 30:
        return None
    gap = d.vwap[i] - d.c[i]
    if d.atr[i] <= 0 or gap < 2.0 * d.atr[i] or d.rsi[i] > 25.0 or d.c[i] <= d.o[i]:
        return None
    return _finish("mean_reversion", d, i, "long", d.l[i] - 0.5 * d.atr[i], [f"{gap / d.atr[i]:.1f} ATR under VWAP", f"RSI {d.rsi[i]:.0f}", "Grøn bar"],
                   [("Volumen over snittet", d.v[i] > d.vol_avg(i))], target=d.vwap[i], min_rr=1.2)


def vcp(d: DayData, i: int) -> Signal | None:
    if i < 30:
        return None
    ranges = []
    for w in range(3):
        end = i - 8 * (2 - w)
        part = range(end - 8, end)
        ranges.append((max(d.h[k] for k in part) - min(d.l[k] for k in part)))
    last_high = max(d.h[i - 8: i])
    if not (ranges[0] > ranges[1] > ranges[2] > 0) or d.c[i] <= last_high or d.c[i] <= d.ema20[i]:
        return None
    if d.norm_pm > 0 and d.rvol(i) < 1.3:
        return None
    return _finish("vcp", d, i, "long", min(d.l[i - 8: i]) - 0.01, ["Tre faldende range-vinduer", "Brud over sidste vindues top", "Over 20-EMA"],
                   [("Over VWAP", d.c[i] > d.vwap[i]), ("Relativ volumen over 2", d.rvol(i) >= 2.0), ("Daglig trend op", bool(d.daily_trend_up))])


def price_action(d: DayData, i: int) -> Signal | None:
    if i < 25:
        return None
    # barerne i-5..i-2 er trenden, i-1 er pullbacken, i er signalbaren
    trend = sum(1 for k in range(i - 6, i - 1) if _body_bull(d, k))
    if trend < 3 or d.c[i] <= d.h[i - 1] or d.c[i] <= d.ema20[i]:
        return None
    pull_range = d.h[i - 1] - d.l[i - 1]
    prior_range = max(d.h[k] - d.l[k] for k in range(i - 6, i - 1))
    higher = d.l[i - 1] > min(d.l[i - 6: i - 2])
    if pull_range > prior_range or not higher:
        return None
    return _finish("price_action", d, i, "long", d.l[i - 1] - 0.01, [f"{trend} af 5 barer er grønne trendbarer", "Kort pullback med højere bund", "Signalbar over pullbackens top", "Over 20-EMA"],
                   [("Over VWAP", d.c[i] > d.vwap[i]), ("Signalbaren er en trendbar", _body_bull(d, i))])


def ema_pullback(d: DayData, i: int) -> Signal | None:
    if i < 30:
        return None
    if not (d.ema20[i] > d.ema20[i - 20] and min(d.l[i - 2: i + 1]) <= d.ema20[i] * 1.001 and d.c[i] > d.ema20[i] and d.c[i] > d.o[i] and d.c[i] > d.vwap[i]):
        return None
    return _finish("ema_pullback", d, i, "long", min(d.l[i - 2: i + 1]) - 0.01, ["20-EMA stigende over 20 minutter", "Rørte 20-EMA og vendte op", "Grøn bar over 20-EMA og VWAP"],
                   [("9-EMA over 20-EMA", d.ema9[i] > d.ema20[i]), ("Relativ volumen over 1", d.rvol(i) >= 1.0)])


def combo_trend_momentum(d: DayData, i: int) -> Signal | None:
    if i < 25 or d.daily_trend_up is not True:
        return None
    if not (d.c[i] > max(d.h[i - 20: i]) and d.norm_pm > 0 and d.rvol(i) >= 2.0 and d.c[i] > d.vwap[i]):
        return None
    if sum(1 for k in range(i - 4, i + 1) if _body_bull(d, k)) < 3:
        return None
    return _finish("combo_trend_momentum", d, i, "long", min(d.l[i - 4: i + 1]) - 0.01,
                   ["Momentum: brud over 20 minutters top på relativ volumen", "VWAP: kursen over VWAP", "Price action: mindst 3 af 5 grønne trendbarer", "Trend: daglig lukkekurs over stigende 20-dages gennemsnit"],
                   [("Relativ volumen over 3", d.rvol(i) >= 3.0), ("RSI under 80", d.rsi[i] < 80.0)])


DETECTORS: dict[str, Callable[[DayData, int], Signal | None]] = {
    "gap_and_go": gap_and_go, "orb": orb, "vwap_bounce": vwap_bounce, "vwap_reclaim": vwap_reclaim, "hod_breakout": hod_breakout,
    "bull_flag": bull_flag, "bear_flag": bear_flag, "momentum_breakout": momentum_breakout, "parabolic_short": parabolic_short,
    "failed_breakout": failed_breakout, "reversal": reversal, "ma_breakout": ma_breakout, "mean_reversion": mean_reversion, "vcp": vcp,
    "price_action": price_action, "ema_pullback": ema_pullback, "combo_trend_momentum": combo_trend_momentum,
}
assert set(DETECTORS) == set(LIBRARY), "hver strategi i biblioteket skal have en detektor"


def scan_day(symbol: str, d: DayData, i: int, enabled: set[str] | None = None) -> list[Signal]:
    """Alle signaler på bar i for ét symbol. Fejl i én detektor må aldrig stoppe de andre."""
    out: list[Signal] = []
    if i < MIN_BARS or i >= len(d.bars):
        return out
    for sid, fn in DETECTORS.items():
        if enabled is not None and sid not in enabled:
            continue
        try:
            sig = fn(d, i)
        except (IndexError, ValueError, ZeroDivisionError):
            sig = None
        if sig is not None:
            sig.symbol = symbol
            out.append(sig)
    return out


# --------------------------------------------------------------------------- uafhængige bekræftelser


def independent_confirmations(signals: list[Signal]) -> dict[str, Any]:
    """Hvor mange UAFHÆNGIGE strategier peger samme vej. To strategier er kun uafhængige, hvis de ikke deler en underliggende
    signalfamilie (fx begge bygger på VWAP). Vi vælger grådigt de stærkeste signaler, hvis familier ikke overlapper."""
    result: dict[str, Any] = {"count": 0, "strategies": [], "shared": []}
    for side in ("long", "short"):
        same = sorted((s for s in signals if s.side == side), key=lambda s: s.quality, reverse=True)
        used: set[str] = set()
        picked: list[Signal] = []
        shared: list[str] = []
        for sig in same:
            families = set(LIBRARY[sig.strategy].families)
            if families & used:
                shared.append(sig.strategy)
                continue
            used |= families
            picked.append(sig)
        if len(picked) > result["count"]:
            result = {"count": len(picked), "side": side, "strategies": [s.strategy for s in picked], "shared": shared}
    return result


# --------------------------------------------------------------------------- markedsforhold (aktier)

REGIME_NOTE = (
    "Tærsklerne er bottens egne startværdier (indeks: ændring siden åbning ±0,3 %/0,8 %, ATR i % af kursen 0,025/0,07, volumen mod normal 0,6). "
    "De er ikke kalibreret på historik endnu. Nyhedsdrevet marked kan appen ikke se."
)


def stock_regime(index_bars: list[Bar], daily: list[Bar] | None = None, session_minutes: int = 390) -> dict[str, Any]:
    """Markedet lige nu ud fra indekset (fx SPY) i dag. Returnerer tags, som strategierne matches mod."""
    if len(index_bars) < MIN_BARS:
        return {"tags": [], "label": "Ukendt", "detail": "For få minutter af indekset til at se regimet endnu.", "note": REGIME_NOTE}
    d = DayData(index_bars, daily, session_minutes)
    i = len(index_bars) - 1
    change = (d.c[i] - d.o[0]) / d.o[0] * 100.0 if d.o[0] else 0.0
    atr_pct = d.atr[i] / d.c[i] * 100.0 if d.c[i] else 0.0
    tags: list[str] = []
    if change >= 0.3 and d.c[i] > d.vwap[i]:
        tags.append("bullish")
    elif change <= -0.3 and d.c[i] < d.vwap[i]:
        tags.append("bearish")
    else:
        tags.append("sideways")
    if abs(change) >= 0.8 and ((change > 0 and d.ema9[i] > d.ema20[i]) or (change < 0 and d.ema9[i] < d.ema20[i])):
        tags.append("strong_momentum")
    if atr_pct >= 0.07:
        tags.append("high_vol")
    elif atr_pct <= 0.025:
        tags.append("low_vol")
    if d.norm_pm > 0 and d.rvol(i, 30) <= 0.6:
        tags.append("low_liquidity")
    names = {"bullish": "Bullish", "bearish": "Bearish", "sideways": "Sidelæns", "strong_momentum": "Stærkt momentum", "high_vol": "Høj volatilitet", "low_vol": "Lav volatilitet", "low_liquidity": "Lav likviditet"}
    return {"tags": tags, "label": ", ".join(names[t] for t in tags), "change_pct": round(change, 2), "atr_pct": round(atr_pct, 3),
            "detail": f"Indekset er {change:+.2f} % siden åbning, gennemsnitlig bar-udsving {atr_pct:.3f} %.".replace(".", ","), "note": REGIME_NOTE}


def fits_regime(strategy_id: str, tags: list[str]) -> dict[str, Any]:
    """Passer strategien til markedet lige nu? Ukendt marked tæller som ukendt, ikke som nej."""
    s = LIBRARY[strategy_id]
    if not tags:
        return {"fits": None, "why": "Markedsforholdet er ukendt endnu."}
    bad = [t for t in tags if t in s.avoid]
    if bad:
        return {"fits": False, "why": f"Bruges ikke ved: {', '.join(bad)}."}
    good = [t for t in tags if t in s.suits]
    if s.suits and not good:
        return {"fits": False, "why": "Passer ikke til de nuværende forhold."}
    return {"fits": True, "why": f"Passer til: {', '.join(good)}." if good else "Kan bruges i alle forhold."}
