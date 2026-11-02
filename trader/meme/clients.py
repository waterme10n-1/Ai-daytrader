"""HTTP-klienter: DexScreener, RugCheck, Jupiter, X og Solana RPC.

Alle svar parses defensivt. API'erne ændrer sig, og et felt der mangler må aldrig vælte
motoren: det giver bare et tomt eller konservativt resultat.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from trader.meme.config import DEXSCREENER_URL, RUGCHECK_URL, SOL_MINT, X_API_URL, MemeSettings

logger = logging.getLogger("trader.meme.clients")

USER_AGENT = "ai-daytrader-meme/1.0"


class SourceError(RuntimeError):
    pass


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
    def __init__(self, base: str, *, headers: dict[str, str] | None = None, timeout: float = 12.0) -> None:
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            merged.update(headers)
        self.base = base.rstrip("/")
        self._client = httpx.Client(base_url=self.base, headers=merged, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict | None = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.base}{path}: {exc.__class__.__name__}") from exc
        if response.status_code == 429:
            raise SourceError(f"{self.base}{path}: rate limit (429)")
        if response.status_code >= 400:
            raise SourceError(f"{self.base}{path}: HTTP {response.status_code} {response.text[:160]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.base}{path}: svaret var ikke JSON") from exc

    def post(self, path: str, payload: dict) -> Any:
        try:
            response = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.base}{path}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise SourceError(f"{self.base}{path}: HTTP {response.status_code} {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.base}{path}: svaret var ikke JSON") from exc


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


class DexScreener:
    def __init__(self) -> None:
        self.http = _Http(DEXSCREENER_URL)

    def close(self) -> None:
        self.http.close()

    def _solana_list(self, path: str) -> list[dict]:
        data = self.http.get(path)
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        return [row for row in rows if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress")]

    def latest_boosts(self) -> list[dict]:
        return self._solana_list("/token-boosts/latest/v1")

    def top_boosts(self) -> list[dict]:
        return self._solana_list("/token-boosts/top/v1")

    def latest_profiles(self) -> list[dict]:
        return self._solana_list("/token-profiles/latest/v1")

    def pairs_for(self, mints: list[str]) -> dict[str, PairInfo]:
        result: dict[str, PairInfo] = {}
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        for start in range(0, len(unique), 30):
            chunk = unique[start : start + 30]
            data = self.http.get(f"/tokens/v1/solana/{','.join(chunk)}")
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
    )


class RugCheck:
    def __init__(self) -> None:
        self.http = _Http(RUGCHECK_URL, timeout=15.0)
        self._cache: dict[str, tuple[float, RugReport]] = {}

    def close(self) -> None:
        self.http.close()

    def report(self, mint: str, max_age: float = 600.0) -> RugReport:
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        try:
            data = self.http.get(f"/v1/tokens/{mint}/report")
        except SourceError:
            data = self.http.get(f"/v1/tokens/{mint}/report/summary")
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
    )


@dataclass
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    raw: dict


class Jupiter:
    def __init__(self, settings: MemeSettings) -> None:
        headers = {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else None
        self.http = _Http(settings.jupiter_url, headers=headers, timeout=15.0)
        self.slippage_bps = settings.slippage_bps
        self.priority_fee_lamports = settings.priority_fee_lamports

    def close(self) -> None:
        self.http.close()

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int | None = None) -> Quote:
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

    def swap_transaction(self, quote: Quote, user_public_key: str) -> str:
        payload = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": max(10_000, self.priority_fee_lamports),
                    "priorityLevel": "high",
                }
            },
        }
        data = self.http.post("/swap/v1/swap", payload)
        tx = (data or {}).get("swapTransaction") if isinstance(data, dict) else None
        if not tx:
            raise SourceError(f"Jupiter byggede ingen transaktion: {str(data)[:200]}")
        return str(tx)

    def prices(self, mints: list[str]) -> dict[str, float]:
        result: dict[str, float] = {}
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        for start in range(0, len(unique), 50):
            chunk = unique[start : start + 50]
            data = self.http.get("/price/v3", params={"ids": ",".join(chunk)})
            if isinstance(data, dict):
                payload = data.get("data") if isinstance(data.get("data"), dict) else data
                for mint, row in payload.items():
                    if isinstance(row, dict):
                        price = _num(row.get("usdPrice") or row.get("price"))
                        if price > 0:
                            result[mint] = price
        return result

    def sol_usd(self) -> float:
        price = self.prices([SOL_MINT]).get(SOL_MINT, 0.0)
        if price <= 0:
            raise SourceError("Kunne ikke hente SOL-kursen fra Jupiter.")
        return price

    def _token_list(self, path: str, params: dict | None = None) -> list[TokenInfo]:
        data = self.http.get(path, params=params)
        rows = data if isinstance(data, list) else (data or {}).get("tokens") or []
        tokens = []
        for row in rows:
            if isinstance(row, dict):
                parsed = parse_token_info(row)
                if parsed is not None:
                    tokens.append(parsed)
        return tokens

    def top_tokens(self, category: str, interval: str = "1h", limit: int = 50) -> list[TokenInfo]:
        return self._token_list(f"/tokens/v2/{category}/{interval}", {"limit": str(limit)})

    def token_infos(self, mints: list[str]) -> dict[str, TokenInfo]:
        result: dict[str, TokenInfo] = {}
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        for start in range(0, len(unique), 100):
            chunk = unique[start : start + 100]
            for token in self._token_list("/tokens/v2/search", {"query": ",".join(chunk)}):
                result[token.mint] = token
        return result


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
