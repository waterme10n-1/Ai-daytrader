"""Sikkerhedstjek af et Solana-token, før et købssignal godkendes.

Fire tjek, 25 point hver (safety_score 0-100):

1. **Likviditet**: mindst 95 % af pool-tokens (LP) skal være brændt eller låst, og en lås skal holde
   mindst 6 måneder (182 dage) endnu.
2. **Ejerfordeling**: de 10 største wallets må højst eje 20 % af tokenet. Pools (AMM), låse, brændte
   konti og kendte børs-wallets (CEX) tæller ikke med, og du kan selv tilføje adresser, der skal ses bort fra.
3. **Authorities**: mint authority og freeze authority skal være opgivet (slået fra).
4. **Omtale på X**: de seneste op til 100 opslag om tokenet vurderes for bot-aktivitet (nye konti, få
   følgere, kopieret tekst, samme konto igen og igen, mange opslag i samme minut, ingen reaktioner).

`trade_allowed` er kun sand, når 1-3 består, omtalen ikke er domineret af bots, og scoren er mindst 70.
Kan noget ikke hentes, regnes det som ikke bestået (bortset fra X, som er valgfri): hellere et nej for meget.

Data kommer fra RugCheck (`RUGCHECK_API_KEY` er valgfri) og X API v2 (`X_BEARER_TOKEN`, kræver et
betalt X-abonnement til søgning). Kør fra kommandolinjen:

    python -m trader.quant.memecoin_filter <MINT> [--ticker BONK] [--exclude ADRESSE ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

RUGCHECK_URL = "https://api.rugcheck.xyz"
X_API_URL = "https://api.x.com"
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True)
class FilterConfig:
    lp_min_secured_pct: float = 95.0
    lp_min_lock_days: int = 182
    top10_max_pct: float = 20.0
    min_score: float = 70.0
    social_min_posts: int = 20
    social_max_bot_share: float = 0.5
    # Konti af disse typer i RugChecks knownAccounts tælles ikke med som ejere.
    excluded_account_types: frozenset[str] = frozenset({"AMM", "LOCKER", "BURN", "CEX"})


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool | None  # None = kunne ikke vurderes
    points: float
    max_points: float
    detail: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Post:
    text: str
    author_id: str
    created_at: datetime | None = None
    author_created_at: datetime | None = None
    followers: int | None = None
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    verified: bool = False

    @property
    def engagement(self) -> int:
        return self.likes + self.replies + self.reposts + self.quotes


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _when(value: Any) -> datetime | None:
    """Unix-tid (sekunder eller millisekunder) eller ISO-tekst → tidspunkt i UTC."""
    if value in (None, "", 0):
        return None
    number = _num(value)
    if number is not None:
        if number <= 0:
            return None
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _dk(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


# --------------------------------------------------------------------------- 1. likviditet


def check_liquidity(report: dict, now: datetime, config: FilterConfig = FilterConfig()) -> Check:
    """LP brændt eller låst. RugCheck regner brændt LP med i lpLockedPct; låse står under `lockers` med udløbsdato."""
    secured: float | None = None
    for market in report.get("markets") or []:
        lp = (market or {}).get("lp") or {}
        value = _num(lp.get("lpLockedPct"))
        if value is not None:
            secured = value if secured is None else max(secured, value)
    if secured is None:
        secured = _num(report.get("lpLockedPct"))
    if secured is None:
        return Check("liquidity", False, 0.0, 25.0, "Ingen oplysninger om poolen. Tokenet kan ikke vurderes.")
    lockers = report.get("lockers") or {}
    entries = list(lockers.values()) if isinstance(lockers, dict) else list(lockers) if isinstance(lockers, list) else []
    unlocks = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        moment = _when(entry.get("unlockDate") or entry.get("unlock_date") or entry.get("unlockTime"))
        if moment is not None:
            unlocks.append(moment)
    data = {"secured_pct": round(secured, 2), "locks": len(unlocks)}
    if secured < config.lp_min_secured_pct:
        points = round(10.0 * max(0.0, secured) / 100.0, 1)
        return Check("liquidity", False, points, 25.0,
                     f"Kun {_dk(secured)} % af poolens LP er brændt eller låst (krav {_dk(config.lp_min_secured_pct, 0)} %). Udvikleren kan trække likviditeten.", data)
    if unlocks:
        first = min(unlocks)
        days = (first - now).total_seconds() / 86400
        data.update(unlock_at=first.isoformat(timespec="seconds"), days_left=round(days, 1))
        if days < config.lp_min_lock_days:
            return Check("liquidity", False, 10.0, 25.0,
                         f"LP er låst, men låsen udløber om {max(0, round(days))} dage (krav {config.lp_min_lock_days}).", data)
        return Check("liquidity", True, 25.0, 25.0, f"{_dk(secured)} % af LP er låst til {first:%d-%m-%Y}.", data)
    return Check("liquidity", True, 25.0, 25.0, f"{_dk(secured)} % af LP er brændt (ingen lås, der kan udløbe).", data)


# --------------------------------------------------------------------------- 2. ejerfordeling


def check_holders(report: dict, exclude: set[str] | frozenset[str] = frozenset(), config: FilterConfig = FilterConfig()) -> Check:
    known = report.get("knownAccounts") or {}
    skipped = {address for address, meta in known.items() if str((meta or {}).get("type") or "").upper() in config.excluded_account_types}
    skipped |= set(exclude)
    rows = []
    for row in report.get("topHolders") or []:
        if not isinstance(row, dict):
            continue
        if row.get("address") in skipped or row.get("owner") in skipped:
            continue
        pct = _num(row.get("pct"))
        if pct is not None:
            rows.append((pct, str(row.get("owner") or row.get("address") or "")))
    if not rows:
        return Check("holders", False, 0.0, 25.0, "Ingen oplysninger om de største ejere. Tokenet kan ikke vurderes.")
    rows.sort(reverse=True)
    top10 = sum(pct for pct, _owner in rows[:10])
    data = {"top10_pct": round(top10, 2), "largest_pct": round(rows[0][0], 2), "excluded": len(skipped)}
    if top10 > config.top10_max_pct:
        points = round(max(0.0, 25.0 * (1 - (top10 - config.top10_max_pct) / 30.0)), 1)
        return Check("holders", False, points, 25.0,
                     f"De 10 største ejere har {_dk(top10)} % (højst {_dk(config.top10_max_pct, 0)} %). De kan dumpe kursen.", data)
    return Check("holders", True, 25.0, 25.0, f"De 10 største ejere har {_dk(top10)} %.", data)


# --------------------------------------------------------------------------- 3. authorities


def check_authorities(report: dict) -> Check:
    token = report.get("token") or {}
    mint = report.get("mintAuthority", token.get("mintAuthority"))
    freeze = report.get("freezeAuthority", token.get("freezeAuthority"))
    problems = []
    if mint:
        problems.append("mint authority er aktiv (der kan laves flere tokens)")
    if freeze:
        problems.append("freeze authority er aktiv (din beholdning kan fryses)")
    points = 25.0 - 12.5 * len(problems)
    data = {"mint_authority": mint or None, "freeze_authority": freeze or None}
    if problems:
        return Check("authorities", False, points, 25.0, "Ikke opgivet: " + " og ".join(problems) + ".", data)
    return Check("authorities", True, 25.0, 25.0, "Mint og freeze authority er opgivet.", data)


# --------------------------------------------------------------------------- 4. omtale på X

_URL = re.compile(r"https?://\S+")
_HANDLE = re.compile(r"[@$#]\w+")
_NOISE = re.compile(r"[^a-zæøå ]+")


def _normalise(text: str) -> str:
    text = _URL.sub(" ", text.lower())
    text = _HANDLE.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def bot_signals(post: Post, now: datetime, *, duplicates: int, author_posts: int, same_minute: int) -> dict[str, bool]:
    """Kendte tegn på bot- eller betalt shill-aktivitet for ét opslag."""
    age_days = (now - post.author_created_at).total_seconds() / 86400 if post.author_created_at else None
    return {
        "new_account": age_days is not None and age_days < 30,
        "few_followers": post.followers is not None and post.followers < 25 and not post.verified,
        "copied_text": duplicates >= 3,
        "repeat_author": author_posts >= 3,
        "burst": same_minute >= 5,
        "no_engagement": post.engagement == 0,
    }


SIGNAL_WEIGHTS = {"copied_text": 0.35, "new_account": 0.25, "few_followers": 0.15, "repeat_author": 0.15, "burst": 0.1, "no_engagement": 0.1}
SIGNAL_LABELS = {
    "copied_text": "kopieret tekst",
    "new_account": "konti under 30 dage",
    "few_followers": "konti med under 25 følgere",
    "repeat_author": "samme konto igen og igen",
    "burst": "mange opslag i samme minut",
    "no_engagement": "ingen reaktioner",
}


def analyze_social(posts: list[Post] | None, now: datetime, config: FilterConfig = FilterConfig()) -> Check:
    """Andelen af opslag, der ligner bots. Et opslag tæller som bot ved mindst 0,5 i vægtede signaler."""
    if posts is None:
        return Check("social", None, 0.0, 25.0, "X er ikke sat op (X_BEARER_TOKEN). Omtalen er ikke vurderet.")
    if not posts:
        return Check("social", None, 0.0, 25.0, "Ingen opslag om tokenet på X den seneste uge.", {"posts": 0})
    texts = Counter(_normalise(post.text) for post in posts)
    authors = Counter(post.author_id for post in posts)
    minutes = Counter(post.created_at.replace(second=0, microsecond=0) for post in posts if post.created_at)
    totals: Counter = Counter()
    bots = 0
    organic_engagement = []
    for post in posts:
        normalised = _normalise(post.text)
        signals = bot_signals(
            post,
            now,
            duplicates=texts[normalised] if normalised else 0,
            author_posts=authors[post.author_id],
            same_minute=minutes[post.created_at.replace(second=0, microsecond=0)] if post.created_at else 0,
        )
        totals.update(name for name, hit in signals.items() if hit)
        score = sum(SIGNAL_WEIGHTS[name] for name, hit in signals.items() if hit)
        if score >= 0.5:
            bots += 1
        else:
            organic_engagement.append(post.engagement)
    share = bots / len(posts)
    top = [f"{SIGNAL_LABELS[name]} ({count})" for name, count in totals.most_common(3) if count]
    data = {
        "posts": len(posts),
        "bot_share": round(share, 3),
        "unique_authors": len(authors),
        "organic_posts": len(posts) - bots,
        "organic_median_engagement": statistics.median(organic_engagement) if organic_engagement else 0,
        "signals": dict(totals),
    }
    points = round(25.0 * (1.0 - share), 1)
    if len(posts) < config.social_min_posts:
        return Check("social", None, min(points, 10.0), 25.0,
                     f"Kun {len(posts)} opslag på X: for lidt til at vurdere omtalen. {_dk(share * 100, 0)} % ligner bots.", data)
    detail = f"{_dk(share * 100, 0)} % af {len(posts)} opslag ligner bots" + (f": {', '.join(top)}." if top else ".")
    if share > config.social_max_bot_share:
        return Check("social", False, points, 25.0, detail + " Omtalen er mest kunstig.", data)
    return Check("social", True, points, 25.0, detail, data)


def parse_x_posts(payload: dict) -> list[Post]:
    """Svar fra X API v2 /2/tweets/search/recent med expansions=author_id."""
    users = {str(user.get("id")): user for user in (payload.get("includes") or {}).get("users") or [] if isinstance(user, dict)}
    posts = []
    for tweet in payload.get("data") or []:
        if not isinstance(tweet, dict):
            continue
        author = users.get(str(tweet.get("author_id")), {})
        metrics = tweet.get("public_metrics") or {}
        followers = (author.get("public_metrics") or {}).get("followers_count")
        posts.append(Post(
            text=str(tweet.get("text") or ""),
            author_id=str(tweet.get("author_id") or ""),
            created_at=_when(tweet.get("created_at")),
            author_created_at=_when(author.get("created_at")),
            followers=int(followers) if isinstance(followers, (int, float)) else None,
            likes=int(metrics.get("like_count") or 0),
            replies=int(metrics.get("reply_count") or 0),
            reposts=int(metrics.get("retweet_count") or 0),
            quotes=int(metrics.get("quote_count") or 0),
            verified=bool(author.get("verified")),
        ))
    return posts


# --------------------------------------------------------------------------- samlet vurdering


def evaluate(
    mint: str,
    report: dict | None,
    posts: list[Post] | None,
    now: datetime | None = None,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
    config: FilterConfig = FilterConfig(),
) -> dict:
    """Saml de fire tjek til ét JSON-venligt svar med safety_score (0-100) og trade_allowed."""
    now = now or datetime.now(timezone.utc)
    if report is None:
        checks = [
            Check("liquidity", False, 0.0, 25.0, "RugCheck kunne ikke hentes."),
            Check("holders", False, 0.0, 25.0, "RugCheck kunne ikke hentes."),
            Check("authorities", False, 0.0, 25.0, "RugCheck kunne ikke hentes."),
        ]
    else:
        checks = [check_liquidity(report, now, config), check_holders(report, exclude, config), check_authorities(report)]
    checks.append(analyze_social(posts, now, config))
    score = sum(check.points for check in checks)
    hard_ok = all(check.passed is True for check in checks[:3])
    social_ok = checks[3].passed is not False
    allowed = hard_ok and social_ok and score >= config.min_score
    reasons = [check.detail for check in checks if check.passed is False]
    if hard_ok and social_ok and score < config.min_score:
        reasons.append(f"Samlet score {_dk(score, 0)} er under {_dk(config.min_score, 0)}.")
    return {
        "mint": mint,
        "safety_score": int(round(score)),
        "trade_allowed": allowed,
        "checks": {check.name: asdict(check) for check in checks},
        "reasons": reasons,
        "checked_at": now.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- hentning


def fetch_report(mint: str, *, api_key: str = "", client: httpx.Client | None = None) -> dict | None:
    """RugChecks fulde rapport. None, hvis den ikke kan hentes."""
    own = client is None
    client = client or httpx.Client(base_url=RUGCHECK_URL, timeout=15.0)
    try:
        response = client.get(f"/v1/tokens/{mint}/report", headers={"X-API-KEY": api_key} if api_key else None)
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            client.close()


def fetch_posts(mint: str, ticker: str | None, *, bearer: str, client: httpx.Client | None = None) -> list[Post] | None:
    """De seneste op til 100 opslag (uden reposts), der nævner adressen eller $TICKER. None, hvis X ikke svarer."""
    clean = "".join(ch for ch in (ticker or "") if ch.isalnum())
    query = f'("{mint}" OR ${clean}) -is:retweet' if 2 <= len(clean) <= 10 and not clean.isdigit() else f'"{mint}" -is:retweet'
    own = client is None
    client = client or httpx.Client(base_url=X_API_URL, timeout=15.0)
    try:
        response = client.get(
            "/2/tweets/search/recent",
            params={
                "query": query,
                "max_results": "100",
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id",
                "user.fields": "created_at,public_metrics,verified",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if response.status_code != 200:
            return None
        return parse_x_posts(response.json())
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            client.close()


def check_token(
    mint: str,
    *,
    ticker: str | None = None,
    exclude: set[str] | frozenset[str] = frozenset(),
    rugcheck_key: str | None = None,
    x_bearer: str | None = None,
    config: FilterConfig = FilterConfig(),
    now: datetime | None = None,
) -> dict:
    if not MINT_RE.match(mint or ""):
        raise ValueError("Det ligner ikke en Solana-adresse.")
    rugcheck_key = os.getenv("RUGCHECK_API_KEY", "") if rugcheck_key is None else rugcheck_key
    x_bearer = os.getenv("X_BEARER_TOKEN", "") if x_bearer is None else x_bearer
    report = fetch_report(mint, api_key=rugcheck_key)
    if ticker is None and report:
        ticker = str(((report.get("tokenMeta") or {}).get("symbol")) or "") or None
    posts = fetch_posts(mint, ticker, bearer=x_bearer) if x_bearer else None
    return evaluate(mint, report, posts, now, exclude=exclude, config=config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sikkerhedstjek af et Solana-token. Svarer med JSON.")
    parser.add_argument("mint", help="tokenets mint-adresse")
    parser.add_argument("--ticker", help="fx BONK (ellers bruges RugChecks navn)")
    parser.add_argument("--exclude", nargs="*", default=[], help="wallets der ikke tæller som ejere (fx børs-wallets)")
    args = parser.parse_args(argv)
    try:
        result = check_token(args.mint, ticker=args.ticker, exclude=set(args.exclude))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["trade_allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
