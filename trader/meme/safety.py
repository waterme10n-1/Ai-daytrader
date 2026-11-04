"""Hårde filtre: markedsdata, rug-tjek og exit-rute. Ingen AI her, og intet kan tilsidesætte dem."""

from __future__ import annotations

from trader.meme.clients import PairInfo, Quote, RugReport, TokenInfo
from trader.meme.config import SniperSettings, StrategyParams


def _usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def market_rejections(pair: PairInfo, params: StrategyParams, now_ms: int | None = None) -> list[str]:
    reasons: list[str] = []
    if pair.dex_id.lower() in {"pumpfun", "pump.fun", "moonshot", "launchlab", "bonk"}:
        reasons.append("Stadig på bonding curve (ikke gradueret til en rigtig pool)")
    if pair.liquidity_usd < params.min_liquidity_usd:
        reasons.append(f"Likviditet {_usd(pair.liquidity_usd)} under {_usd(params.min_liquidity_usd)}")
    if pair.volume.get("h1", 0.0) < params.min_volume_h1_usd:
        reasons.append(f"Volumen sidste time {_usd(pair.volume.get('h1', 0.0))} under {_usd(params.min_volume_h1_usd)}")
    if pair.txns("h1") < params.min_txns_h1:
        reasons.append(f"Kun {pair.txns('h1')} handler sidste time")
    age = pair.age_minutes(now_ms)
    if age is None:
        reasons.append("Ukendt alder")
    else:
        if age < params.min_age_minutes:
            reasons.append(f"For ny ({age:.0f} min.) — de fleste rugs sker i de første minutter")
        if age > params.max_age_hours * 60:
            reasons.append(f"For gammel ({age / 60:.0f} timer) til en momentum-handel")
    if pair.mcap and pair.mcap < params.min_mcap_usd:
        reasons.append(f"Markedsværdi {_usd(pair.mcap)} under {_usd(params.min_mcap_usd)}")
    if pair.mcap and pair.mcap > params.max_mcap_usd:
        reasons.append(f"Markedsværdi {_usd(pair.mcap)} over {_usd(params.max_mcap_usd)}")
    if pair.change.get("h1", 0.0) > params.max_change_h1_pct:
        reasons.append(f"Overophedet: +{pair.change['h1']:.0f} % på en time")
    if pair.change.get("m5", 0.0) < -15.0:
        reasons.append(f"Styrtdykker lige nu ({pair.change['m5']:.0f} % på 5 min.)")
    ratio = pair.buy_ratio("h1")
    if ratio is not None and ratio < params.min_buy_ratio_h1:
        reasons.append(f"Flere sælgere end købere ({ratio * 100:.0f} % køb sidste time)")
    if pair.price_usd <= 0:
        reasons.append("Ingen pris")
    return reasons


def rug_rejections(report: RugReport | None, token: TokenInfo | None, params: StrategyParams) -> list[str]:
    reasons: list[str] = []
    if report is None:
        reasons.append("RugCheck kunne ikke hentes — springes over for en sikkerheds skyld")
    else:
        if report.rugged:
            reasons.append("RugCheck markerer tokenet som rugged")
        if report.mint_authority:
            reasons.append("Mint authority er aktiv (udvikleren kan printe nye tokens)")
        if report.freeze_authority:
            reasons.append("Freeze authority er aktiv (udvikleren kan fryse din beholdning)")
        for name in report.dangers[:3]:
            reasons.append(f"RugCheck fare: {name}")
        if report.risk > params.max_rug_score:
            reasons.append(f"Rug-risiko {report.risk:.2f} over {params.max_rug_score:.2f}")
        if report.top10_pct is not None and report.top10_pct > params.max_top10_pct:
            reasons.append(f"Top-10 ejer {report.top10_pct:.0f} % (maks. {params.max_top10_pct:.0f} %)")
    if token is not None:
        if token.mint_disabled is False and not (report and report.mint_authority):
            reasons.append("Jupiter: mint authority er ikke slået fra")
        if token.freeze_disabled is False and not (report and report.freeze_authority):
            reasons.append("Jupiter: freeze authority er ikke slået fra")
        if token.dev_balance_pct is not None and token.dev_balance_pct > 10.0:
            reasons.append(f"Udvikleren ejer stadig {token.dev_balance_pct:.0f} %")
        if token.top_holders_pct is not None and token.top_holders_pct > params.max_top10_pct + 15.0:
            reasons.append(f"Jupiter: topholdere ejer {token.top_holders_pct:.0f} %")
    return reasons


def roundtrip_loss_pct(buy: Quote, sell: Quote) -> float:
    """Hvor meget SOL man taber ved at købe og straks sælge igen. Fanger honeypots og tynde pools."""
    if buy.in_amount <= 0:
        return 100.0
    return max(0.0, (1.0 - sell.out_amount / buy.in_amount) * 100.0)


def roundtrip_rejections(buy: Quote | None, sell: Quote | None, params: StrategyParams) -> list[str]:
    if buy is None:
        return ["Jupiter fandt ingen købsrute"]
    if sell is None:
        return ["Jupiter fandt ingen salgsrute tilbage — mulig honeypot"]
    loss = roundtrip_loss_pct(buy, sell)
    reasons = []
    if loss > params.max_roundtrip_loss_pct:
        reasons.append(f"Køb+salg koster {loss:.1f} % (maks. {params.max_roundtrip_loss_pct:.1f} %) — tynd pool eller skat")
    if buy.price_impact_pct > params.max_roundtrip_loss_pct / 2:
        reasons.append(f"Prisimpact ved køb {buy.price_impact_pct:.1f} %")
    return reasons


BONDING_CURVE_DEXES = {"pumpfun", "pump.fun", "moonshot", "launchlab", "bonk"}


def sniper_rejections(pair: PairInfo, token: TokenInfo | None, cfg: SniperSettings, age_minutes: float) -> list[str]:
    """Hurtige tjek for helt nye tokens. RugCheck og exit-rute tjekkes bagefter."""
    reasons: list[str] = []
    if age_minutes > cfg.max_age_minutes:
        reasons.append(f"For gammel til sniping ({age_minutes:.0f} min.)")
    if pair.dex_id.lower() in BONDING_CURVE_DEXES:
        reasons.append("Stadig på bonding curve")
    if pair.liquidity_usd < cfg.min_liquidity_usd:
        reasons.append(f"Likviditet {_usd(pair.liquidity_usd)} under {_usd(cfg.min_liquidity_usd)}")
    if pair.change.get("m5", 0.0) < -25.0:
        reasons.append(f"Dumper allerede ({pair.change['m5']:.0f} % på 5 min.)")
    buys, sells = pair.buys.get("m5", 0), pair.sells.get("m5", 0)
    if sells > max(4, buys * 2):
        reasons.append(f"Massivt salg lige nu ({sells} salg mod {buys} køb på 5 min.)")
    if token is not None:
        if token.mint_disabled is False:
            reasons.append("Mint authority er ikke slået fra")
        if token.freeze_disabled is False:
            reasons.append("Freeze authority er ikke slået fra")
        if token.dev_balance_pct is not None and token.dev_balance_pct > 5.0:
            reasons.append(f"Udvikleren ejer {token.dev_balance_pct:.0f} %")
        if token.top_holders_pct is not None and token.top_holders_pct > cfg.max_top10_pct:
            reasons.append(f"Topholdere ejer {token.top_holders_pct:.0f} % (maks. {cfg.max_top10_pct:.0f} %)")
        if token.holders is not None and token.holders < cfg.min_holders:
            reasons.append(f"Kun {token.holders} holdere (min. {cfg.min_holders})")
    if cfg.require_socials:
        has_social = bool(pair.socials or pair.websites or (token and (token.twitter or token.telegram or token.website)))
        if not has_social:
            reasons.append("Ingen X, Telegram eller hjemmeside")
    return reasons
