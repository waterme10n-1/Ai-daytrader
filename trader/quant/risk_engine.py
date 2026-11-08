"""Risikomotor for daytrading (USA og København) og memecoins.

Reglerne, som standard:

1. **Positionsstørrelse**: højst 1 % af porteføljen i risiko pr. handel (kan sættes op til 2 %, aldrig over).
   Risikoen er afstanden fra entry til stop-loss gange antal, plus gebyrerne for at komme ind og ud igen.
2. **Memecoins**: højst 0,5 % af porteføljen pr. handel (positionens størrelse, ikke kun risikoen, fordi et
   rug pull kan tage det hele). Der er altid en gevinstsikring ved 2x, hvor den oprindelige indsats sælges.
3. **Dagligt tabsstop**: falder porteføljen mere end 5 % på én dag, skal alle positioner lukkes, og der
   handles ikke i 24 timer.
4. **Gebyrer**: kurtage, amerikanske salgsafgifter (SEC og FINRA), spread og slippage, og for Solana
   transaktionsgebyr, prioritetsgebyr, pool-gebyr og slippage. Et signal godkendes kun, hvis det stadig er
   godt nok, når gebyrerne er trukket fra.

Alle beløb regnes i porteføljens valuta. Kurser i en anden valuta (fx DKK-aktier i en USD-portefølje)
omregnes med `TradeSignal.fx_to_base`. Motoren handler ikke selv: den svarer ja eller nej og siger hvor meget.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum

LAMPORTS_PER_SOL = 1_000_000_000

# Det højeste, risiko pr. handel kan sættes til. Kan ikke ændres af en konfiguration.
HARD_MAX_RISK_PCT = 0.02


def _pct(value: float, digits: int = 1) -> str:
    """0,054 → "5,4 %" (dansk komma)."""
    return f"{value * 100:.{digits}f}".replace(".", ",") + " %"


def _num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


class AssetClass(str, Enum):
    US_STOCK = "us_stock"
    DK_STOCK = "dk_stock"
    MEMECOIN = "memecoin"


@dataclass(frozen=True)
class RiskConfig:
    """Grænserne. Alle procenter er brøker: 0,01 = 1 %."""

    risk_per_trade_pct: float = 0.01
    memecoin_max_position_pct: float = 0.005
    memecoin_take_profit_multiple: float = 2.0
    max_daily_drawdown_pct: float = 0.05
    cooldown_hours: float = 24.0
    # Efter gebyrer skal den mulige gevinst være mindst 1,5 gange den mulige tabsrisiko.
    min_net_reward_risk: float = 1.5
    # Gebyrerne må højst æde 25 % af den mulige gevinst.
    max_fee_share_of_reward: float = 0.25
    # Hele aktier som standard (danske mæglere handler ikke brøkdele).
    fractional_stocks: bool = False

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade_pct <= HARD_MAX_RISK_PCT:
            raise ValueError(f"Risiko pr. handel skal være over 0 og højst {_pct(HARD_MAX_RISK_PCT, 0)}.")
        if not 0 < self.memecoin_max_position_pct <= HARD_MAX_RISK_PCT:
            raise ValueError(f"Memecoin-position skal være over 0 og højst {_pct(HARD_MAX_RISK_PCT, 0)} af porteføljen.")
        if self.memecoin_take_profit_multiple <= 1.0:
            raise ValueError("Gevinstsikringen for memecoins skal ligge over købskursen (fx 2,0 = 2x).")
        if not 0 < self.max_daily_drawdown_pct < 1:
            raise ValueError("Det daglige tabsstop skal være mellem 0 og 100 %.")
        if self.cooldown_hours < 0:
            raise ValueError("Pausen efter tabsstop kan ikke være negativ.")
        if self.min_net_reward_risk < 0 or not 0 < self.max_fee_share_of_reward <= 1:
            raise ValueError("Ugyldige gebyrkrav.")


# --------------------------------------------------------------------------- gebyrer


@dataclass(frozen=True)
class StockFees:
    """Omkostninger for én side af en aktiehandel (køb eller salg), i aktiens valuta.

    Satserne ændrer sig, og de afhænger af din mægler. Sæt dine egne, hvis standarderne ikke passer."""

    commission_pct: float = 0.0
    min_commission: float = 0.0
    per_share: float = 0.0
    # USA: SEC-afgift (Section 31) pr. dollar, kun ved salg. Satsen ændres af SEC flere gange om året.
    sec_fee_rate: float = 0.0
    # USA: FINRA TAF pr. aktie, kun ved salg, med et loft pr. handel.
    taf_per_share: float = 0.0
    taf_max: float = 0.0
    # Halv spread plus slippage pr. side, som andel af handlens værdi.
    spread_pct: float = 0.0

    def side(self, side: str, quantity: float, price: float) -> float:
        if quantity <= 0 or price <= 0:
            return 0.0
        notional = quantity * price
        commission = notional * self.commission_pct + quantity * self.per_share
        if commission > 0 or self.min_commission > 0:
            commission = max(self.min_commission, commission)
        regulatory = 0.0
        if side == "sell":
            regulatory += notional * self.sec_fee_rate
            taf = quantity * self.taf_per_share
            regulatory += min(self.taf_max, taf) if self.taf_max > 0 else taf
        return commission + regulatory + notional * self.spread_pct


# Alpaca: ingen kurtage. SEC 27,80 $ pr. mio. og FINRA TAF 0,000166 $ pr. aktie (maks. 8,30 $) ved salg
# er satserne fra 2024; tjek de aktuelle hos SEC og FINRA. 0,05 % spread og slippage pr. side.
US_FEES = StockFees(sec_fee_rate=0.0000278, taf_per_share=0.000166, taf_max=8.30, spread_pct=0.0005)
# Eksempel på en dansk netbank/mægler: 0,10 % kurtage, mindst 29 kr. Sæt din egen mæglers pris.
DK_FEES = StockFees(commission_pct=0.001, min_commission=29.0, spread_pct=0.001)


@dataclass(frozen=True)
class SolanaFees:
    """Omkostninger ved en memecoin-handel på Solana, i USD."""

    sol_price_usd: float
    base_fee_lamports: int = 5_000
    priority_fee_lamports: int = 100_000
    jito_tip_lamports: int = 0
    # Poolens gebyr pr. swap (typisk 0,25 % på Raydium og PumpSwap).
    swap_fee_pct: float = 0.0025
    # Forventet slippage pr. side. Nye memecoins har tynde pools.
    slippage_pct: float = 0.01
    # Depositum for at oprette en token-konto. Kommer tilbage, når kontoen lukkes efter salget.
    account_rent_sol: float = 0.00203928
    rent_refunded: bool = True

    def __post_init__(self) -> None:
        if self.sol_price_usd <= 0:
            raise ValueError("SOL-kursen skal være over 0.")

    def network_usd(self) -> float:
        lamports = self.base_fee_lamports + self.priority_fee_lamports + self.jito_tip_lamports
        return lamports / LAMPORTS_PER_SOL * self.sol_price_usd

    def side(self, side: str, quantity: float, price: float) -> float:
        if quantity <= 0 or price <= 0:
            return 0.0
        cost = self.network_usd() + quantity * price * (self.swap_fee_pct + self.slippage_pct)
        if side == "buy" and not self.rent_refunded:
            cost += self.account_rent_sol * self.sol_price_usd
        return cost


@dataclass(frozen=True)
class FeeModel:
    us: StockFees = US_FEES
    dk: StockFees = DK_FEES
    solana: SolanaFees = field(default_factory=lambda: SolanaFees(sol_price_usd=150.0))

    def side(self, asset: AssetClass, side: str, quantity: float, price: float) -> float:
        """Omkostning for én side, i instrumentets valuta."""
        if asset is AssetClass.US_STOCK:
            return self.us.side(side, quantity, price)
        if asset is AssetClass.DK_STOCK:
            return self.dk.side(side, quantity, price)
        return self.solana.side(side, quantity, price)

    def round_trip(self, asset: AssetClass, quantity: float, entry: float, exit_price: float) -> float:
        return self.side(asset, "buy", quantity, entry) + self.side(asset, "sell", quantity, exit_price)


@dataclass(frozen=True)
class TaxProfile:
    """Et groft skøn over skat på en gevinst. Ikke skatterådgivning: tjek dine egne satser på skat.dk.

    Aktier (Danmark): aktieindkomst, 27 % op til progressionsgrænsen og 42 % over.
    Krypto (Danmark): gevinster er personlig indkomst, altså din marginalskat (typisk ca. 37-52 %),
    og tab kan kun trækkes fra med en lavere værdi."""

    stock_rate: float = 0.27
    crypto_rate: float = 0.37

    def after_tax(self, asset: AssetClass, profit: float) -> float:
        if profit <= 0:
            return profit  # tabets skatteværdi afhænger af resten af året; regnes ikke med her
        rate = self.crypto_rate if asset is AssetClass.MEMECOIN else self.stock_rate
        return profit * (1.0 - rate)


# --------------------------------------------------------------------------- signaler og portefølje


@dataclass(frozen=True)
class TradeSignal:
    """Et købssignal. Kun køb (long). Kurser i instrumentets valuta."""

    symbol: str
    asset_class: AssetClass
    entry: float
    stop_loss: float
    take_profit: float | None = None
    # 1 enhed af instrumentets valuta i porteføljens valuta (fx 0,145 for DKK i en USD-portefølje).
    fx_to_base: float = 1.0


@dataclass
class PortfolioState:
    """Porteføljen lige nu. `equity` er inklusive urealiseret resultat på åbne positioner."""

    equity: float
    day_start_equity: float
    cash: float | None = None
    halted_until: datetime | None = None


@dataclass(frozen=True)
class ExitLeg:
    price: float
    quantity: float
    reason: str


@dataclass(frozen=True)
class DrawdownStatus:
    drawdown_pct: float
    breached: bool
    close_all: bool
    halted_until: datetime | None
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    approved: bool
    symbol: str
    asset_class: AssetClass
    quantity: float
    notional: float
    risk_amount: float
    risk_pct: float
    fees: float
    gross_reward: float
    net_reward: float
    net_reward_risk: float | None
    net_reward_after_tax: float
    take_profit: float | None
    exits: tuple[ExitLeg, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["asset_class"] = self.asset_class.value
        return data


# --------------------------------------------------------------------------- motoren


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None, fees: FeeModel | None = None, tax: TaxProfile | None = None) -> None:
        self.config = config or RiskConfig()
        self.fees = fees or FeeModel()
        self.tax = tax or TaxProfile()

    # ---- regel 3: dagligt tabsstop
    def check_daily_drawdown(self, portfolio: PortfolioState, now: datetime) -> DrawdownStatus:
        """Tjek dagens tab. Ved brud sættes `portfolio.halted_until`, og `close_all` beder kalderen lukke alt.

        `close_all` er kun sand første gang: under pausen er positionerne allerede lukket."""
        if portfolio.day_start_equity <= 0:
            raise ValueError("Dagens startværdi skal være over 0.")
        drawdown = max(0.0, (portfolio.day_start_equity - portfolio.equity) / portfolio.day_start_equity)
        if portfolio.halted_until is not None and now < portfolio.halted_until:
            return DrawdownStatus(drawdown, True, False, portfolio.halted_until, f"Handelsstop til {portfolio.halted_until:%d-%m %H:%M}.")
        if drawdown > self.config.max_daily_drawdown_pct:
            until = now + timedelta(hours=self.config.cooldown_hours)
            portfolio.halted_until = until
            reason = (
                f"Porteføljen er faldet {_pct(drawdown)} i dag (grænse {_pct(self.config.max_daily_drawdown_pct, 0)}). "
                f"Luk alle positioner. Ingen handler før {until:%d-%m %H:%M}."
            )
            return DrawdownStatus(drawdown, True, True, until, reason)
        return DrawdownStatus(drawdown, False, False, None)

    # ---- regel 2: memecoins sælger indsatsen ved 2x
    def memecoin_exits(self, quantity: float, entry: float, take_profit: float | None = None) -> tuple[ExitLeg, ...]:
        """Ved 2x sælges så mange tokens, at indsatsen og gebyrerne er hjemme. Resten kan løbe videre."""
        target = entry * self.config.memecoin_take_profit_multiple
        cost_in = quantity * entry + self.fees.side(AssetClass.MEMECOIN, "buy", quantity, entry)
        sol = self.fees.solana
        variable = sol.swap_fee_pct + sol.slippage_pct
        sell_qty = (cost_in + sol.network_usd()) / (target * (1.0 - variable))
        sell_qty = min(quantity, sell_qty)
        legs = [ExitLeg(round(target, 12), sell_qty, f"Træk indsatsen ud ved {self.config.memecoin_take_profit_multiple:g}x")]
        rest = quantity - sell_qty
        if rest > 1e-12 and take_profit is not None and take_profit > target:
            legs.append(ExitLeg(take_profit, rest, "Resten ved signalets kursmål"))
        return tuple(legs)

    # ---- regel 1 og 4: størrelse og gebyrer
    def _risk_cost(self, signal: TradeSignal, quantity: float) -> float:
        """Tab i porteføljens valuta, hvis stoppet rammes: kursfald plus gebyrer begge veje."""
        per_unit = (signal.entry - signal.stop_loss) * quantity
        fees = self.fees.round_trip(signal.asset_class, quantity, signal.entry, signal.stop_loss)
        return (per_unit + fees) * signal.fx_to_base

    def _round_quantity(self, signal: TradeSignal, quantity: float) -> float:
        if quantity <= 0:
            return 0.0
        if signal.asset_class is AssetClass.MEMECOIN or self.config.fractional_stocks:
            return math.floor(quantity * 1e6) / 1e6
        return float(math.floor(quantity + 1e-9))

    def size(self, signal: TradeSignal, portfolio: PortfolioState) -> float:
        """Største antal, der holder risikoen (inkl. gebyrer) under grænsen og respekterer loftet for memecoins og kontanter."""
        price = signal.entry * signal.fx_to_base
        budget = portfolio.equity * self.config.risk_per_trade_pct
        unit_risk = (signal.entry - signal.stop_loss) * signal.fx_to_base
        caps = [budget / unit_risk, portfolio.equity / price]
        if signal.asset_class is AssetClass.MEMECOIN:
            caps.append(portfolio.equity * self.config.memecoin_max_position_pct / price)
        if portfolio.cash is not None:
            caps.append(max(0.0, portfolio.cash) / price)
        high = max(0.0, min(caps))
        # Gebyrerne vokser med størrelsen (og har ofte en minimumskurtage): find den største størrelse, der holder.
        if self._risk_cost(signal, high) > budget:
            low = 0.0
            for _ in range(60):
                mid = (low + high) / 2
                if self._risk_cost(signal, mid) <= budget:
                    low = mid
                else:
                    high = mid
            high = low
        quantity = self._round_quantity(signal, high)
        # Kontanter skal også dække købsgebyret.
        while quantity > 0 and portfolio.cash is not None:
            needed = (quantity * signal.entry + self.fees.side(signal.asset_class, "buy", quantity, signal.entry)) * signal.fx_to_base
            if needed <= portfolio.cash:
                break
            step = 1.0 if signal.asset_class is not AssetClass.MEMECOIN and not self.config.fractional_stocks else quantity * 0.01
            quantity = self._round_quantity(signal, quantity - step)
        return quantity

    def evaluate(self, signal: TradeSignal, portfolio: PortfolioState, now: datetime) -> Decision:
        """Godkend eller afvis et signal. Afviste signaler har en eller flere grunde i `reasons`."""
        reasons: list[str] = []
        status = self.check_daily_drawdown(portfolio, now)
        if status.breached:
            reasons.append(status.reason)
        if signal.entry <= 0 or signal.fx_to_base <= 0:
            reasons.append("Ugyldig kurs.")
        if signal.stop_loss <= 0 or signal.stop_loss >= signal.entry:
            reasons.append("Stop-loss skal ligge under købskursen (kun køb understøttes).")
        memecoin = signal.asset_class is AssetClass.MEMECOIN
        take_profit = signal.take_profit
        if memecoin:
            floor_target = signal.entry * self.config.memecoin_take_profit_multiple
            if take_profit is None or take_profit < floor_target:
                take_profit = floor_target  # krav: gevinstsikring ved 2x
        elif take_profit is None:
            reasons.append("Mangler kursmål. Uden et mål kan gevinst mod risiko ikke regnes ud.")
        elif take_profit <= signal.entry:
            reasons.append("Kursmålet skal ligge over købskursen.")
        if reasons:
            return self._decision(False, signal, 0.0, take_profit, (), tuple(reasons))

        quantity = self.size(signal, portfolio)
        if quantity <= 0:
            return self._decision(False, signal, 0.0, take_profit, (), ("Porteføljen er for lille til én enhed inden for risikogrænsen.",))
        exits = self.memecoin_exits(quantity, signal.entry, signal.take_profit) if memecoin else (ExitLeg(take_profit, quantity, "Kursmål"),)

        fx = signal.fx_to_base
        risk_amount = self._risk_cost(signal, quantity)
        fees = self.fees.round_trip(signal.asset_class, quantity, signal.entry, take_profit) * fx
        gross = quantity * (take_profit - signal.entry) * fx
        net = gross - fees
        rr = net / risk_amount if risk_amount > 0 else None
        if net <= 0:
            reasons.append("Gebyrerne æder hele gevinsten.")
        elif fees > self.config.max_fee_share_of_reward * gross:
            reasons.append(f"Gebyrerne er {_pct(fees / gross, 0)} af den mulige gevinst (højst {_pct(self.config.max_fee_share_of_reward, 0)}).")
        if rr is not None and rr < self.config.min_net_reward_risk:
            reasons.append(f"Gevinst mod risiko efter gebyrer er 1:{_num(rr)} (mindst 1:{_num(self.config.min_net_reward_risk)}).")
        return self._decision(not reasons, signal, quantity, take_profit, exits, tuple(reasons), portfolio=portfolio)

    def _decision(
        self,
        approved: bool,
        signal: TradeSignal,
        quantity: float,
        take_profit: float | None,
        exits: tuple[ExitLeg, ...],
        reasons: tuple[str, ...],
        portfolio: PortfolioState | None = None,
    ) -> Decision:
        fx = signal.fx_to_base
        if quantity > 0 and take_profit is not None:
            risk_amount = self._risk_cost(signal, quantity)
            fees = self.fees.round_trip(signal.asset_class, quantity, signal.entry, take_profit) * fx
            gross = quantity * (take_profit - signal.entry) * fx
        else:
            risk_amount = fees = gross = 0.0
        net = gross - fees
        return Decision(
            approved=approved,
            symbol=signal.symbol,
            asset_class=signal.asset_class,
            quantity=quantity,
            notional=quantity * signal.entry * fx,
            risk_amount=risk_amount,
            risk_pct=risk_amount / portfolio.equity if portfolio and portfolio.equity > 0 else 0.0,
            fees=fees,
            gross_reward=gross,
            net_reward=net,
            net_reward_risk=net / risk_amount if risk_amount > 0 else None,
            net_reward_after_tax=self.tax.after_tax(signal.asset_class, net),
            take_profit=take_profit,
            exits=exits,
            reasons=reasons,
        )
