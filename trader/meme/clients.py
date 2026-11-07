"""HTTP-klienter: DexScreener, RugCheck, Jupiter, X og Solana RPC.

Alle svar parses defensivt. API'erne ændrer sig, og et felt der mangler må aldrig vælte
motoren: det giver bare et tomt eller konservativt resultat.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from trader.meme.config import DEXSCREENER_URL, RUGCHECK_URL, SOL_MINT, SOLANATRACKER_URL, X_API_URL, MemeSettings

logger = logging.getLogger("trader.meme.clients")

USER_AGENT = "ai-daytrader-meme/1.0"


class SourceError(RuntimeError):
    pass


class RateLimited(SourceError):
    """API'et har sagt stop (429), eller botten sparer selv på kvoten. Prøv igen senere."""


# Andel af minutkvoten hver slags kald må bruge. Salg og køb går altid forrest.
PRIORITY_SHARE = {"high": 1.0, "normal": 0.8, "low": 0.6}
HIGH_PRIORITY_MAX_WAIT = 8.0
COOLDOWN_START_SECONDS = 15.0
COOLDOWN_MAX_SECONDS = 120.0


class CallBudget:
    """Holder styr på kald pr. minut (glidende vindue), så vi ikke rammer API'ets grænse.

    Jupiter tæller quote, pris og token-lister i samme spand. Lavprioritets-kald (lister over nye
    tokens) springes over, når kvoten er ved at være brugt, så der altid er plads til salg.
    """

    def __init__(self, per_minute: int, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self.per_minute = max(1, int(per_minute))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()
        self._cooldown_until = 0.0
        self._strikes = 0
        self.limited_total = 0
        self.skipped_total = 0
        self.last_limited_at = 0.0

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()

    def _wait_needed(self, priority: str, now: float) -> float:
        """Sekunder til der er plads. 0 betyder klar nu."""
        if now < self._cooldown_until:
            return self._cooldown_until - now
        allowed = max(1, int(self.per_minute * PRIORITY_SHARE.get(priority, PRIORITY_SHARE["normal"])))
        if len(self._calls) < allowed:
            return 0.0
        return max(0.05, self._calls[len(self._calls) - allowed] + 60.0 - now)

    def take(self, priority: str = "normal") -> None:
        """Reserver en plads. Lister og nye køb springes over, når kvoten er brugt.

        Salg (high) springes aldrig over: de venter højst et par sekunder og prøver så alligevel,
        for et stop-loss, der ikke bliver sendt, er værre end endnu et 429.
        """
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                wait = self._wait_needed(priority, now)
                if wait <= 0 or (priority == "high" and waited >= HIGH_PRIORITY_MAX_WAIT):
                    self._calls.append(now)
                    return
                if priority != "high":
                    self.skipped_total += 1
                    reason = "venter efter rate limit" if now < self._cooldown_until else "sparer på kvoten"
                    raise RateLimited(f"{reason} ({len(self._calls)}/{self.per_minute} kald det seneste minut)")
                pause = min(wait, HIGH_PRIORITY_MAX_WAIT - waited)
            self._sleep(pause)
            waited += pause

    def limited(self, retry_after: float | None = None) -> float:
        """API'et svarede 429. Hold pause, længere for hver gang i træk."""
        with self._lock:
            self._strikes += 1
            self.limited_total += 1
            pause = COOLDOWN_START_SECONDS * (2 ** (self._strikes - 1))
            if retry_after and retry_after > 0:
                pause = max(pause, retry_after)
            pause = min(COOLDOWN_MAX_SECONDS, pause)
            now = self._clock()
            self.last_limited_at = now
            self._cooldown_until = max(self._cooldown_until, now + pause)
            return pause

    def succeeded(self) -> None:
        with self._lock:
            self._strikes = 0

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            self._prune(now)
            return {
                "used_last_minute": len(self._calls),
                "per_minute": self.per_minute,
                "cooldown_seconds": round(max(0.0, self._cooldown_until - now)),
                "limited_total": self.limited_total,
                "skipped_total": self.skipped_total,
                "limited_seconds_ago": round(now - self.last_limited_at) if self.last_limited_at else None,
            }


# Hvor gammel en liste over tokens må være, når et API siger stop. Bedre end ingenting.
STALE_LIST_SECONDS_DEFAULT = 1800.0


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


class _Http:
    def __init__(
        self,
        base: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 12.0,
        budget: CallBudget | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            merged.update(headers)
        self.base = base.rstrip("/")
        self.budget = budget
        self._client = httpx.Client(base_url=self.base, headers=merged, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, priority: str, **kwargs) -> httpx.Response:
        if self.budget is not None:
            self.budget.take(priority)
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.base}{path}: {exc.__class__.__name__}") from exc
        if response.status_code == 429:
            pause = self.budget.limited(_retry_after(response)) if self.budget is not None else 0
            note = f", venter {pause:.0f} s" if pause else ""
            raise RateLimited(f"{self.base}{path}: rate limit (429){note}")
        if self.budget is not None and response.status_code < 400:
            self.budget.succeeded()
        return response

    @staticmethod
    def _json(response: httpx.Response, where: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{where}: svaret var ikke JSON") from exc

    def get(self, path: str, params: dict | None = None, *, priority: str = "normal") -> Any:
        response = self._send("GET", path, priority, params=params)
        if response.status_code >= 400:
            raise SourceError(f"{self.base}{path}: HTTP {response.status_code} {response.text[:160]}")
        return self._json(response, f"{self.base}{path}")

    def post(self, path: str, payload: dict, *, priority: str = "normal") -> Any:
        response = self._send("POST", path, priority, json=payload)
        if response.status_code >= 400:
            raise SourceError(f"{self.base}{path}: HTTP {response.status_code} {response.text[:200]}")
        return self._json(response, f"{self.base}{path}")


# --------------------------------------------------------------------------- DexScreener


@dataclass
class PairInfo:
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    url: str
    price_usd: float
    liquidity_usd: float
    fdv: float
    mcap: float
    volume: dict[str, float]
    change: dict[str, float]
    buys: dict[str, int]
    sells: dict[str, int]
    created_at_ms: int
    websites: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    boosts_active: int = 0

    def age_minutes(self, now_ms: int | None = None) -> float | None:
        if not self.created_at_ms:
            return None
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0.0, (now - self.created_at_ms) / 60_000)

    def buy_ratio(self, window: str = "h1") -> float | None:
        buys = self.buys.get(window, 0)
        sells = self.sells.get(window, 0)
        total = buys + sells
        if total <= 0:
            return None
        return buys / total

    def txns(self, window: str = "h1") -> int:
        return self.buys.get(window, 0) + self.sells.get(window, 0)


def parse_pair(raw: dict) -> PairInfo | None:
    base = raw.get("baseToken") or {}
    mint = str(base.get("address") or "")
    if not mint:
        return None
    info = raw.get("info") or {}
    socials: dict[str, str] = {}
    for item in info.get("socials") or []:
        kind = str(item.get("type") or item.get("platform") or "").lower()
        url = str(item.get("url") or item.get("handle") or "")
        if kind and url and kind not in socials:
            socials[kind] = url
    websites = [str(item.get("url")) for item in info.get("websites") or [] if item.get("url")]
    txns = raw.get("txns") or {}
    buys = {key: _int((txns.get(key) or {}).get("buys")) for key in ("m5", "h1", "h6", "h24")}
    sells = {key: _int((txns.get(key) or {}).get("sells")) for key in ("m5", "h1", "h6", "h24")}
    volume = {key: _num((raw.get("volume") or {}).get(key)) for key in ("m5", "h1", "h6", "h24")}
    change = {key: _num((raw.get("priceChange") or {}).get(key)) for key in ("m5", "h1", "h6", "h24")}
    return PairInfo(
        mint=mint,
        symbol=str(base.get("symbol") or "?")[:24],
        name=str(base.get("name") or "")[:64],
        pair_address=str(raw.get("pairAddress") or ""),
        dex_id=str(raw.get("dexId") or ""),
        url=str(raw.get("url") or ""),
        price_usd=_num(raw.get("priceUsd")),
        liquidity_usd=_num((raw.get("liquidity") or {}).get("usd")),
        fdv=_num(raw.get("fdv")),
        mcap=_num(raw.get("marketCap")) or _num(raw.get("fdv")),
        volume=volume,
        change=change,
        buys=buys,
        sells=sells,
        created_at_ms=_int(raw.get("pairCreatedAt")),
        websites=websites,
        socials=socials,
        boosts_active=_int((raw.get("boosts") or {}).get("active")),
    )


def best_pairs(raw_pairs: list[dict]) -> dict[str, PairInfo]:
    """Vælg parret med mest likviditet pr. token, kun hvor tokenet er base og kæden er Solana."""
    chosen: dict[str, PairInfo] = {}
    for raw in raw_pairs or []:
        if str(raw.get("chainId") or "solana") != "solana":
            continue
        pair = parse_pair(raw)
        if pair is None or pair.price_usd <= 0:
            continue
        current = chosen.get(pair.mint)
        if current is None or pair.liquidity_usd > current.liquidity_usd:
            chosen[pair.mint] = pair
    return chosen


# DexScreener: 60 kald/min på profiler og boosts, 300 på kurser og par (pr. IP). Vi holder os under.
DEX_LISTS_RPM = 50
DEX_PAIRS_RPM = 240
# Lister over nye profiler og boosts. Sniperen og scanningen spørger begge; ét kald pr. vindue er nok.
DEX_PROFILES_TTL = 10.0
DEX_BOOSTS_TTL = 30.0


class _ListCache:
    """Gem en liste et øjeblik, og brug den seneste, hvis API'et siger stop."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, fetch, *, stale_seconds: float = STALE_LIST_SECONDS_DEFAULT):
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            value = fetch()
        except RateLimited:
            if cached and now - cached[0] < stale_seconds:
                return cached[1]
            raise
        with self._lock:
            self._items[key] = (time.monotonic(), value)
        return value


class DexScreener:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        lists_budget: CallBudget | None = None,
        pairs_budget: CallBudget | None = None,
    ) -> None:
        self.lists_budget = lists_budget or CallBudget(DEX_LISTS_RPM)
        self.pairs_budget = pairs_budget or CallBudget(DEX_PAIRS_RPM)
        # To forbindelser, fordi DexScreener tæller lister og kurser hver for sig.
        self.http = _Http(DEXSCREENER_URL, budget=self.pairs_budget, transport=transport)
        self.lists_http = _Http(DEXSCREENER_URL, budget=self.lists_budget, transport=transport)
        self._lists = _ListCache()

    def close(self) -> None:
        self.http.close()
        self.lists_http.close()

    def usage(self) -> dict:
        pairs = self.pairs_budget.snapshot()
        lists = self.lists_budget.snapshot()
        ago = [value for value in (pairs["limited_seconds_ago"], lists["limited_seconds_ago"]) if value is not None]
        return {
            "pairs": pairs,
            "lists": lists,
            "limited_total": pairs["limited_total"] + lists["limited_total"],
            "limited_seconds_ago": min(ago) if ago else None,
            "cooldown_seconds": max(pairs["cooldown_seconds"], lists["cooldown_seconds"]),
        }

    def _solana_list(self, path: str, ttl: float) -> list[dict]:
        def fetch() -> list[dict]:
            data = self.lists_http.get(path, priority="low")
            rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            return [row for row in rows if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress")]

        return list(self._lists.get(path, ttl, fetch))

    def latest_boosts(self) -> list[dict]:
        return self._solana_list("/token-boosts/latest/v1", DEX_BOOSTS_TTL)

    def top_boosts(self) -> list[dict]:
        return self._solana_list("/token-boosts/top/v1", DEX_BOOSTS_TTL)

    def latest_profiles(self) -> list[dict]:
        return self._solana_list("/token-profiles/latest/v1", DEX_PROFILES_TTL)

    def pairs_for(self, mints: list[str], *, priority: str = "normal") -> dict[str, PairInfo]:
        """Kurser og likviditet. Overvågning af åbne positioner kalder med priority="high"."""
        result: dict[str, PairInfo] = {}
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        for start in range(0, len(unique), 30):
            chunk = unique[start : start + 30]
            data = self.http.get(f"/tokens/v1/solana/{','.join(chunk)}", priority=priority)
            rows = data if isinstance(data, list) else (data or {}).get("pairs") or []
            result.update(best_pairs(rows))
        return result


# --------------------------------------------------------------------------- RugCheck


@dataclass
class RugReport:
    mint: str
    risk: float
    risks: list[dict]
    dangers: list[str]
    warnings: list[str]
    mint_authority: bool
    freeze_authority: bool
    top10_pct: float | None
    lp_locked_pct: float | None
    holders: int | None
    rugged: bool
    insiders: int
    decimals: int | None
    raw_score: float | None
    creator: str = ""
    top_owners: list = field(default_factory=list)


def parse_rug_report(mint: str, data: dict) -> RugReport:
    risks = [item for item in data.get("risks") or [] if isinstance(item, dict)]
    dangers = [str(item.get("name") or "Ukendt") for item in risks if str(item.get("level")).lower() == "danger"]
    warnings = [str(item.get("name") or "Ukendt") for item in risks if str(item.get("level")).lower() == "warn"]
    token = data.get("token") or {}
    mint_auth = data.get("mintAuthority", token.get("mintAuthority"))
    freeze_auth = data.get("freezeAuthority", token.get("freezeAuthority"))
    known = data.get("knownAccounts") or {}
    excluded = set()
    for address, meta in known.items():
        kind = str((meta or {}).get("type") or "").upper()
        if kind in {"AMM", "LOCKER", "BURN"}:
            excluded.add(address)
    holders = data.get("topHolders")
    top10: float | None = None
    if isinstance(holders, list) and holders:
        filtered = [
            row
            for row in holders
            if isinstance(row, dict) and row.get("address") not in excluded and row.get("owner") not in excluded
        ]
        top10 = sum(_num(row.get("pct")) for row in filtered[:10])
    lp_locked: float | None = None
    for market in data.get("markets") or []:
        lp = (market or {}).get("lp") or {}
        if "lpLockedPct" in lp:
            value = _num(lp.get("lpLockedPct"))
            lp_locked = value if lp_locked is None else max(lp_locked, value)
    if lp_locked is None and "lpLockedPct" in data:
        lp_locked = _num(data.get("lpLockedPct"))
    normalised = data.get("score_normalised")
    raw_score = data.get("score")
    if normalised is not None:
        base = min(1.0, max(0.0, _num(normalised) / 100.0))
    elif raw_score is not None:
        base = min(1.0, math.log10(1 + max(0.0, _num(raw_score))) / 4.5)
    else:
        base = 0.5
    risk = min(1.0, base + 0.25 * len(dangers) + 0.06 * len(warnings))
    total_holders = data.get("totalHolders")
    decimals = token.get("decimals")
    owners: list[tuple[str, float]] = []
    if isinstance(holders, list):
        for row in holders:
            if not isinstance(row, dict) or row.get("address") in excluded or row.get("owner") in excluded:
                continue
            owner = str(row.get("owner") or "")
            if owner and owner not in [item[0] for item in owners]:
                owners.append((owner, _num(row.get("pct"))))
            if len(owners) >= 3:
                break
    return RugReport(
        mint=mint,
        risk=risk,
        risks=risks,
        dangers=dangers,
        warnings=warnings,
        mint_authority=bool(mint_auth),
        freeze_authority=bool(freeze_auth),
        top10_pct=top10,
        lp_locked_pct=lp_locked,
        holders=_int(total_holders) if total_holders is not None else None,
        rugged=bool(data.get("rugged")),
        insiders=_int(data.get("graphInsidersDetected")),
        decimals=_int(decimals) if decimals is not None else None,
        raw_score=_num(raw_score) if raw_score is not None else None,
        creator=str(data.get("creator") or ""),
        top_owners=owners,
    )


# RugCheck oplyser ingen fast grænse. Vi er forsigtige og holder pause ved 429.
RUGCHECK_RPM_DEFAULT = 30


class RugCheck:
    def __init__(
        self,
        settings: MemeSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        budget: CallBudget | None = None,
    ) -> None:
        api_key = settings.rugcheck_api_key if settings is not None else ""
        per_minute = settings.rugcheck_rpm if settings is not None else RUGCHECK_RPM_DEFAULT
        self.budget = budget or CallBudget(per_minute)
        headers = {"X-API-KEY": api_key} if api_key else None
        self.http = _Http(RUGCHECK_URL, headers=headers, timeout=15.0, budget=self.budget, transport=transport)
        self._cache: dict[str, tuple[float, RugReport]] = {}

    def close(self) -> None:
        self.http.close()

    def usage(self) -> dict:
        return self.budget.snapshot()

    def report(self, mint: str, max_age: float = 600.0, *, priority: str = "normal") -> RugReport:
        """Et rug-tjek. Ved rate limit kastes RateLimited: kandidaten skal tjekkes igen, ikke afvises."""
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        try:
            data = self.http.get(f"/v1/tokens/{mint}/report", priority=priority)
        except RateLimited:
            raise  # opsummeringen tæller i samme kvote; prøv ikke igen med det samme
        except SourceError:
            data = self.http.get(f"/v1/tokens/{mint}/report/summary", priority=priority)
        if not isinstance(data, dict):
            raise SourceError("RugCheck returnerede ikke et objekt.")
        report = parse_rug_report(mint, data)
        self._cache[mint] = (time.time(), report)
        if len(self._cache) > 500:
            oldest = sorted(self._cache.items(), key=lambda item: item[1][0])[:200]
            for key, _value in oldest:
                self._cache.pop(key, None)
        return report


# --------------------------------------------------------------------------- Jupiter


@dataclass
class TokenInfo:
    mint: str
    symbol: str
    name: str
    decimals: int | None
    organic_score: float | None
    verified: bool
    holders: int | None
    mint_disabled: bool | None
    freeze_disabled: bool | None
    top_holders_pct: float | None
    dev_balance_pct: float | None
    twitter: str
    website: str
    telegram: str
    launchpad: str
    liquidity: float | None
    mcap: float | None
    stats_1h: dict
    created_at_ms: int = 0
    graduated: bool | None = None
    # Jupiter sætter audit.isSus, når tokenet er markeret som mistænkeligt (feltet findes kun dér).
    sus: bool = False
    # Antal tokens udvikler-wallet'en har lavet (audit.devMints). Mange = "token-fabrik".
    dev_mints: int | None = None
    dev: str = ""
    stats_5m: dict = field(default_factory=dict)


def _iso_ms(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value if value > 10**11 else value * 1000)
    try:
        from datetime import datetime, timezone

        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000)
    except ValueError:
        return 0


def parse_token_info(raw: dict) -> TokenInfo | None:
    mint = str(raw.get("id") or raw.get("address") or raw.get("mint") or "")
    if not mint:
        return None
    audit = raw.get("audit") or {}

    def opt_bool(key: str) -> bool | None:
        return bool(audit[key]) if key in audit and audit[key] is not None else None

    def opt_num(value: Any) -> float | None:
        return _num(value) if value is not None else None

    return TokenInfo(
        mint=mint,
        symbol=str(raw.get("symbol") or "?")[:24],
        name=str(raw.get("name") or "")[:64],
        decimals=_int(raw.get("decimals")) if raw.get("decimals") is not None else None,
        organic_score=opt_num(raw.get("organicScore")),
        verified=bool(raw.get("isVerified")),
        holders=_int(raw.get("holderCount")) if raw.get("holderCount") is not None else None,
        mint_disabled=opt_bool("mintAuthorityDisabled"),
        freeze_disabled=opt_bool("freezeAuthorityDisabled"),
        top_holders_pct=opt_num(audit.get("topHoldersPercentage")),
        dev_balance_pct=opt_num(audit.get("devBalancePercentage")),
        twitter=str(raw.get("twitter") or ""),
        website=str(raw.get("website") or ""),
        telegram=str(raw.get("telegram") or ""),
        launchpad=str(raw.get("launchpad") or ""),
        liquidity=opt_num(raw.get("liquidity")),
        mcap=opt_num(raw.get("mcap")),
        stats_1h=dict(raw.get("stats1h") or {}),
        created_at_ms=_iso_ms((raw.get("firstPool") or {}).get("createdAt") or raw.get("createdAt")),
        graduated=bool(raw.get("graduatedPool")) if "graduatedPool" in raw else None,
        sus="isSus" in audit and audit.get("isSus") is not False,
        dev_mints=_int(audit.get("devMints")) if audit.get("devMints") is not None else None,
        dev=str(raw.get("dev") or ""),
        stats_5m=dict(raw.get("stats5m") or {}),
    )


@dataclass
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    raw: dict


# Lister over trending tokens ændrer sig langsomt (1-times vindue). Ingen grund til at hente dem hvert minut.
TOP_TOKENS_TTL = 300.0
# Hvor gammel en liste må være, når Jupiter siger stop. Bedre end ingenting.
STALE_LIST_SECONDS = 1800.0
TOKEN_INFO_TTL = 120.0


class Jupiter:
    """Swap, priser og token-lister. Alle kald deler én minutkvote hos Jupiter."""

    def __init__(self, settings: MemeSettings, *, transport: httpx.BaseTransport | None = None, budget: CallBudget | None = None) -> None:
        headers = {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else None
        self.keyed = bool(settings.jupiter_api_key)
        self.budget = budget or CallBudget(settings.jupiter_rpm)
        self.http = _Http(settings.jupiter_url, headers=headers, timeout=15.0, budget=self.budget, transport=transport)
        self.slippage_bps = settings.slippage_bps
        self.priority_fee_lamports = settings.priority_fee_lamports
        # Uden nøgle deles grænsen med andre på samme IP. Hent nye tokens lidt sjældnere.
        self.recent_ttl = 6.0 if self.keyed else 15.0
        self._lists: dict[str, tuple[float, list[TokenInfo]]] = {}
        self._infos: dict[str, tuple[float, TokenInfo]] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def usage(self) -> dict:
        return {**self.budget.snapshot(), "keyed": self.keyed}

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int | None = None, *, priority: str = "high") -> Quote:
        if amount <= 0:
            raise SourceError("Beløbet til en quote skal være positivt.")
        data = self.http.get(
            "/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount)),
                "slippageBps": str(slippage_bps or self.slippage_bps),
                "restrictIntermediateTokens": "true",
            },
            priority=priority,
        )
        if not isinstance(data, dict) or "outAmount" not in data:
            message = (data or {}).get("error") if isinstance(data, dict) else None
            raise SourceError(f"Jupiter fandt ingen rute: {message or 'tomt svar'}")
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=_int(data.get("inAmount")),
            out_amount=_int(data.get("outAmount")),
            price_impact_pct=abs(_num(data.get("priceImpactPct"))) * 100.0,
            raw=data,
        )

    def swap_transaction(self, quote: Quote, user_public_key: str, *, urgent: bool = False, jito_tip: int | None = None) -> str:
        if jito_tip:
            fee: dict = {"jitoTipLamports": int(jito_tip * (2 if urgent else 1))}
        else:
            fee = {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": max(10_000, self.priority_fee_lamports * (3 if urgent else 1)),
                    "priorityLevel": "veryHigh" if urgent else "high",
                }
            }
        payload = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": fee,
        }
        data = self.http.post("/swap/v1/swap", payload, priority="high")
        tx = (data or {}).get("swapTransaction") if isinstance(data, dict) else None
        if not tx:
            raise SourceError(f"Jupiter byggede ingen transaktion: {str(data)[:200]}")
        return str(tx)

    def prices(self, mints: list[str], *, priority: str = "normal") -> dict[str, float]:
        result: dict[str, float] = {}
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        for start in range(0, len(unique), 50):
            chunk = unique[start : start + 50]
            data = self.http.get("/price/v3", params={"ids": ",".join(chunk)}, priority=priority)
            if isinstance(data, dict):
                payload = data.get("data") if isinstance(data.get("data"), dict) else data
                for mint, row in payload.items():
                    if isinstance(row, dict):
                        price = _num(row.get("usdPrice") or row.get("price"))
                        if price > 0:
                            result[mint] = price
        return result

    def sol_usd(self, *, priority: str = "normal") -> float:
        price = self.prices([SOL_MINT], priority=priority).get(SOL_MINT, 0.0)
        if price <= 0:
            raise SourceError("Kunne ikke hente SOL-kursen fra Jupiter.")
        return price

    def _token_list(self, path: str, params: dict | None = None, *, priority: str = "low") -> list[TokenInfo]:
        data = self.http.get(path, params=params, priority=priority)
        rows = data if isinstance(data, list) else (data or {}).get("tokens") or []
        tokens = []
        for row in rows:
            if isinstance(row, dict):
                parsed = parse_token_info(row)
                if parsed is not None:
                    tokens.append(parsed)
        return tokens

    def _cached_list(self, key: str, ttl: float, fetch, limit: int) -> list[TokenInfo]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._lists.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1][:limit]
        try:
            tokens = fetch()
        except RateLimited:
            # Jupiter siger stop: brug den seneste liste, hvis den ikke er for gammel.
            if cached and now - cached[0] < STALE_LIST_SECONDS:
                return cached[1][:limit]
            raise
        with self._cache_lock:
            self._lists[key] = (time.monotonic(), tokens)
        return tokens[:limit]

    def top_tokens(self, category: str, interval: str = "1h", limit: int = 50, *, priority: str = "low") -> list[TokenInfo]:
        fetch_limit = max(limit, 50)
        return self._cached_list(
            f"top:{category}:{interval}",
            TOP_TOKENS_TTL,
            lambda: self._token_list(f"/tokens/v2/{category}/{interval}", {"limit": str(fetch_limit)}, priority=priority),
            limit,
        )

    def recent_tokens(self, limit: int = 60, *, priority: str = "low") -> list[TokenInfo]:
        """Tokens hvis første pool lige er oprettet. Sniperens hovedkilde."""
        fetch_limit = max(limit, 60)
        return self._cached_list(
            "recent",
            self.recent_ttl,
            lambda: self._token_list("/tokens/v2/recent", {"limit": str(fetch_limit)}, priority=priority),
            limit,
        )

    def token_infos(self, mints: list[str], *, priority: str = "low") -> dict[str, TokenInfo]:
        result: dict[str, TokenInfo] = {}
        now = time.monotonic()
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        with self._cache_lock:
            for mint in unique:
                cached = self._infos.get(mint)
                if cached and now - cached[0] < TOKEN_INFO_TTL:
                    result[mint] = cached[1]
        missing = [mint for mint in unique if mint not in result]
        for start in range(0, len(missing), 100):
            chunk = missing[start : start + 100]
            fetched = self._token_list("/tokens/v2/search", {"query": ",".join(chunk)}, priority=priority)
            with self._cache_lock:
                for token in fetched:
                    result[token.mint] = token
                    self._infos[token.mint] = (time.monotonic(), token)
                if len(self._infos) > 2000:
                    for mint in sorted(self._infos, key=lambda key: self._infos[key][0])[:1000]:
                        self._infos.pop(mint, None)
        return result


# --------------------------------------------------------------------------- Solana Tracker (top-traders)

SOLANATRACKER_RPM = 20
_WALLET_KEYS = ("wallet", "address", "walletAddress", "wallet_address", "owner", "account", "trader")
_PNL_KEYS = ("realizedPnl", "realized_pnl", "realizedPnlUsd", "realized", "totalPnl", "total_pnl", "pnlUsd", "pnl_usd", "pnl", "profit", "total")
_WINRATE_KEYS = ("winRate", "win_rate", "winrate", "winPercentage", "win_percentage")
_TRADES_KEYS = ("trades", "totalTrades", "total_trades", "tradeCount", "trade_count", "txCount", "numTrades", "totalTransactions")


@dataclass
class TopTrader:
    wallet: str
    rank: int
    pnl_usd: float | None = None
    win_rate: float | None = None
    trades: int | None = None


def _looks_like_wallet(text: str) -> bool:
    return 32 <= len(text) <= 44 and all(ch in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for ch in text)


def _first_number(row: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            for inner in ("realized", "total", "usd", "value", "amount"):
                if isinstance(value.get(inner), (int, float, str)):
                    number = _num(value.get(inner), float("nan"))
                    if not math.isnan(number):
                        return number
            continue
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip()):
            number = _num(value, float("nan"))
            if not math.isnan(number):
                return number
    return None


def _trader_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("traders", "wallets", "leaderboard", "results", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            inner = _trader_rows(value)
            if inner:
                return inner
    return []


def parse_top_traders(data: Any) -> list[TopTrader]:
    """Leaderboardet fra Solana Tracker. Feltnavnene læses forsigtigt, så små ændringer ikke vælter botten."""
    traders: list[TopTrader] = []
    seen: set[str] = set()
    for row in _trader_rows(data):
        wallet = ""
        for key in _WALLET_KEYS:
            value = row.get(key)
            if isinstance(value, dict):
                value = value.get("address") or value.get("wallet")
            if isinstance(value, str) and _looks_like_wallet(value.strip()):
                wallet = value.strip()
                break
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        # Tallene kan ligge direkte i rækken eller i en "stats"-blok. Kig begge steder.
        places = [row["stats"], row] if isinstance(row.get("stats"), dict) else [row]

        def pick(keys: tuple[str, ...]) -> float | None:
            for place in places:
                value = _first_number(place, keys)
                if value is not None:
                    return value
            return None

        win = pick(_WINRATE_KEYS)
        if win is not None and win > 1.0:
            win /= 100.0  # nogle API'er giver procent, andre en brøk
        trades = pick(_TRADES_KEYS)
        for place in places:
            if trades is None and isinstance(place.get("buys"), (int, float)) and isinstance(place.get("sells"), (int, float)):
                trades = float(place["buys"]) + float(place["sells"])
        traders.append(
            TopTrader(
                wallet=wallet,
                rank=len(traders) + 1,
                pnl_usd=pick(_PNL_KEYS),
                win_rate=win,
                trades=int(trades) if trades is not None else None,
            )
        )
    return traders


def describe_shape(data: Any) -> str:
    """Til forbindelsestesten: hvilke felter svaret har, hvis vi ikke kunne finde nogen wallets."""
    rows = _trader_rows(data)
    if rows:
        return "felter: " + ", ".join(sorted(rows[0].keys())[:12])
    if isinstance(data, dict):
        return "nøgler: " + ", ".join(sorted(data.keys())[:12])
    return f"type: {type(data).__name__}"


class SolanaTracker:
    """Dagens (eller ugens) mest profitable Solana-traders fra Solana Tracker."""

    def __init__(self, settings: MemeSettings, *, transport: httpx.BaseTransport | None = None, budget: CallBudget | None = None) -> None:
        self.budget = budget or CallBudget(SOLANATRACKER_RPM)
        headers = {"x-api-key": settings.solanatracker_api_key} if settings.solanatracker_api_key else None
        self.http = _Http(settings.solanatracker_url or SOLANATRACKER_URL, headers=headers, timeout=15.0, budget=self.budget, transport=transport)
        self.last_shape = ""

    def close(self) -> None:
        self.http.close()

    def usage(self) -> dict:
        return self.budget.snapshot()

    def top_traders(self, days: int = 1, limit: int = 10) -> list[TopTrader]:
        data = self.http.get(
            "/v2/pnl/leaderboard/top",
            params={"days": str(int(days)), "limit": str(max(1, min(100, int(limit) * 2)))},
            priority="normal",
        )
        traders = parse_top_traders(data)
        self.last_shape = describe_shape(data)
        if not traders:
            raise SourceError(f"Solana Tracker gav ingen wallets ({self.last_shape}).")
        return traders[: int(limit)]


class JitoSender:
    """Sender signerede transaktioner direkte til Jitos block engine. Lander oftere og ses ikke i den offentlige strøm."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client = httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._client.close()

    def send(self, b64_tx: str) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [b64_tx, {"encoding": "base64"}]}
        try:
            response = self._client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise SourceError(f"Jito: {exc.__class__.__name__}") from exc
        data = response.json() if response.content else {}
        if response.status_code >= 400 or data.get("error"):
            raise SourceError(f"Jito: {data.get('error') or response.status_code}")
        return str(data.get("result"))


# --------------------------------------------------------------------------- X (valgfri)


@dataclass
class XBuzz:
    tweets: int
    authors: int
    likes: int
    reposts: int
    samples: list[str]


def parse_x_search(data: dict) -> XBuzz:
    tweets = [row for row in (data or {}).get("data") or [] if isinstance(row, dict)]
    authors = {row.get("author_id") for row in tweets if row.get("author_id")}
    likes = sum(_int((row.get("public_metrics") or {}).get("like_count")) for row in tweets)
    reposts = sum(_int((row.get("public_metrics") or {}).get("retweet_count")) for row in tweets)
    ranked = sorted(tweets, key=lambda row: _int((row.get("public_metrics") or {}).get("like_count")), reverse=True)
    samples = [" ".join(str(row.get("text") or "").split())[:240] for row in ranked[:5]]
    return XBuzz(tweets=len(tweets), authors=len(authors), likes=likes, reposts=reposts, samples=samples)


class XClient:
    def __init__(self, bearer: str) -> None:
        self.http = _Http(X_API_URL, headers={"Authorization": f"Bearer {bearer}"}, timeout=12.0)
        self._cache: dict[str, tuple[float, XBuzz]] = {}

    def close(self) -> None:
        self.http.close()

    def buzz(self, mint: str, symbol: str) -> XBuzz:
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]
        clean = "".join(ch for ch in symbol if ch.isalnum())
        query = f'("{mint}" OR ${clean}) -is:retweet' if 2 <= len(clean) <= 10 and not clean.isdigit() else f'"{mint}" -is:retweet'
        data = self.http.get(
            "/2/tweets/search/recent",
            params={
                "query": query,
                "max_results": "100",
                "tweet.fields": "public_metrics,created_at,author_id",
            },
        )
        result = parse_x_search(data if isinstance(data, dict) else {})
        self._cache[mint] = (time.time(), result)
        return result


# --------------------------------------------------------------------------- Solana RPC


class SolanaRPC:
    def __init__(self, url: str) -> None:
        self.url = url
        self._client = httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
        self._ids = 0

    def close(self) -> None:
        self._client.close()

    def call(self, method: str, params: list | None = None) -> Any:
        self._ids += 1
        payload = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or []}
        try:
            response = self._client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise SourceError(f"RPC {method}: {exc.__class__.__name__}") from exc
        if response.status_code == 429:
            raise SourceError(f"RPC {method}: rate limit (429). Brug en egen RPC, fx Helius.")
        if response.status_code >= 400:
            raise SourceError(f"RPC {method}: HTTP {response.status_code}")
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise SourceError(f"RPC {method}: {data['error']}")
        return (data or {}).get("result")

    def balance_lamports(self, pubkey: str) -> int:
        result = self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return _int((result or {}).get("value"))

    def token_accounts(self, owner: str, mint: str) -> list[dict]:
        """Returnerer [{pubkey, amount (raw), decimals, program}] for ejerens konti for mint."""
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        accounts = []
        for row in (result or {}).get("value") or []:
            account = row.get("account") or {}
            parsed = ((account.get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = parsed.get("tokenAmount") or {}
            accounts.append(
                {
                    "pubkey": row.get("pubkey"),
                    "amount": _int(amount.get("amount")),
                    "decimals": _int(amount.get("decimals")),
                    "program": account.get("owner"),
                }
            )
        return accounts

    def latest_blockhash(self) -> tuple[str, int]:
        result = self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = (result or {}).get("value") or {}
        return str(value.get("blockhash")), _int(value.get("lastValidBlockHeight"))

    def block_height(self) -> int:
        return _int(self.call("getBlockHeight", [{"commitment": "confirmed"}]))

    def send_transaction(self, b64: str) -> str:
        return str(
            self.call(
                "sendTransaction",
                [b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3, "preflightCommitment": "confirmed"}],
            )
        )

    def signature_status(self, signature: str) -> dict | None:
        result = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = (result or {}).get("value") or [None]
        return values[0]

    def signatures_for(self, address: str, limit: int = 15) -> list[dict]:
        result = self.call("getSignaturesForAddress", [address, {"limit": limit, "commitment": "confirmed"}])
        return [row for row in result or [] if isinstance(row, dict)]

    def transaction(self, signature: str) -> dict | None:
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )
