"""Jupiters minutkvote: spar på kaldene, gem lister, hold pause ved 429 og afvis aldrig et token pga. kvoten."""

from __future__ import annotations

import httpx
import pytest

import meme_fakes
from meme_fakes import make_engine
from test_meme_sniper import NEW, _arm
from trader.meme.clients import CallBudget, Jupiter, RateLimited
from trader.meme.config import JUPITER_RPM_KEYED, JUPITER_RPM_KEYLESS, KEYED_JUPITER, MemeSettings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_budget_keeps_room_for_sells():
    clock = Clock()
    budget = CallBudget(10, clock=clock, sleep=clock.sleep)
    for _ in range(6):
        budget.take("low")
    with pytest.raises(RateLimited):
        budget.take("low")  # lister må kun bruge 60 % af kvoten
    budget.take("normal")
    budget.take("normal")
    with pytest.raises(RateLimited):
        budget.take("normal")  # quotes til nye køb stopper ved 80 %
    budget.take("high")
    budget.take("high")
    assert clock.slept == []
    clock.now += 5
    budget.take("high")  # kvoten er fuld: et salg venter kort og sendes så alligevel
    assert clock.slept == [pytest.approx(8.0)]
    assert budget.snapshot()["used_last_minute"] == 11
    assert budget.skipped_total == 2
    clock.now += 60
    budget.take("low")  # vinduet glider: et minut senere er der plads igen


def test_budget_backs_off_after_429_and_recovers():
    clock = Clock()
    budget = CallBudget(50, clock=clock, sleep=clock.sleep)
    assert budget.limited() == 15
    with pytest.raises(RateLimited, match="venter efter rate limit"):
        budget.take("normal")
    budget.take("high")  # et salg venter højst 8 s på pausen og prøver så alligevel
    assert clock.slept == [pytest.approx(8.0)]
    assert budget.limited() == 30  # anden gang i træk: dobbelt så lang pause
    assert budget.limited(retry_after=100) == 100
    assert budget.limited() == 120  # loftet
    budget.succeeded()
    clock.now += 200
    assert budget.limited() == 15
    snap = budget.snapshot()
    assert snap["limited_total"] == 5 and snap["cooldown_seconds"] == 15


def _jupiter(handler, **overrides) -> Jupiter:
    cfg = meme_fakes.settings(**overrides)
    return Jupiter(cfg, transport=httpx.MockTransport(handler))


def _token_row(mint: str) -> dict:
    return {"id": mint, "symbol": mint[:4], "name": mint, "decimals": 6, "holderCount": 100}


def test_top_lists_are_cached_and_survive_429():
    calls = []
    state = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if state["status"] == 429:
            return httpx.Response(429, headers={"Retry-After": "40"}, json={"error": "slow down"})
        return httpx.Response(200, json=[_token_row("Aaaa1111"), _token_row("Bbbb2222")])

    jup = _jupiter(handler)
    assert len(jup.top_tokens("toptrending", "1h", 50)) == 2
    assert len(jup.top_tokens("toptrending", "1h", 20)) == 2
    assert calls == ["/tokens/v2/toptrending/1h"]  # anden gang fra cachen

    state["status"] = 429
    with pytest.raises(RateLimited, match="429"):
        jup.top_tokens("toporganicscore", "1h", 50)  # ingen gammel liste at falde tilbage på
    assert jup.usage()["cooldown_seconds"] >= 39  # Retry-After respekteres
    stamp, tokens = jup._lists["top:toptrending:1h"]
    jup._lists["top:toptrending:1h"] = (stamp - 10_000, tokens)
    with pytest.raises(RateLimited):
        jup.top_tokens("toptrending", "1h", 50)  # under pausen springes lister over, og listen er for gammel
    jup.close()


def test_stale_list_is_used_while_jupiter_says_stop():
    state = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["status"] == 429:
            return httpx.Response(429)
        return httpx.Response(200, json=[_token_row("Cccc3333")])

    jup = _jupiter(handler)
    jup.recent_tokens(60)
    stamp, tokens = jup._lists["recent"]
    jup._lists["recent"] = (stamp - 120, tokens)  # forældet, men under en halv time gammel
    state["status"] = 429
    assert [token.mint for token in jup.recent_tokens(60)] == ["Cccc3333"]
    assert jup.usage()["limited_total"] == 1
    jup.close()


def test_token_infos_are_cached_per_mint():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"].split(",")
        queries.append(query)
        return httpx.Response(200, json=[_token_row(mint) for mint in query])

    jup = _jupiter(handler)
    assert set(jup.token_infos(["Mint1111", "Mint2222"])) == {"Mint1111", "Mint2222"}
    assert set(jup.token_infos(["Mint2222", "Mint3333"])) == {"Mint2222", "Mint3333"}
    assert queries == [["Mint1111", "Mint2222"], ["Mint3333"]]
    jup.close()


def test_key_switches_to_api_host_and_higher_quota(monkeypatch):
    monkeypatch.delenv("JUPITER_API_URL", raising=False)
    monkeypatch.delenv("JUPITER_RPM", raising=False)
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    keyless = MemeSettings.from_env()
    assert keyless.jupiter_rpm == JUPITER_RPM_KEYLESS and "lite-api" in keyless.jupiter_url
    monkeypatch.setenv("JUPITER_API_KEY", "test-key")
    keyed = MemeSettings.from_env()
    assert keyed.jupiter_url == KEYED_JUPITER and keyed.jupiter_rpm == JUPITER_RPM_KEYED

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["host"] = request.url.host
        return httpx.Response(200, json={"So11111111111111111111111111111111111111112": {"usdPrice": 150.0}})

    jup = Jupiter(keyed, transport=httpx.MockTransport(handler))
    assert jup.sol_usd() == 150.0
    assert seen == {"key": "test-key", "host": "api.jup.ag"}
    assert jup.usage()["keyed"] is True
    jup.close()


class ThrottledJup(meme_fakes.FakeJup):
    def __init__(self, dex) -> None:
        super().__init__(dex)
        self.throttle = True

    def quote(self, input_mint, output_mint, amount, slippage_bps=None, **kw):
        if self.throttle and kw.get("priority") == "normal":
            raise RateLimited("sparer på kvoten (20/25 kald det seneste minut)")
        return super().quote(input_mint, output_mint, amount, slippage_bps)


def test_sniper_retries_a_token_that_only_hit_the_quota():
    engine, dex, rug, _jup, _notes = make_engine()
    jup = ThrottledJup(dex)
    engine.clients.jup = jup
    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    assert not [row for row in engine.store.recent_candidates("paper") if row["mint"] == NEW and row["stage"] == "rute"]
    assert NEW not in (engine.store.get_json("sniper_seen", {}) or {})  # ikke markeret som set

    jup.throttle = False
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    assert [row["mint"] for row in rows] == [NEW]


def test_rate_limit_alert_explains_the_free_key():
    engine, *_ = make_engine()
    clock = Clock()
    budget = CallBudget(25, clock=clock, sleep=clock.sleep)
    budget.limited()

    class Usage(meme_fakes.FakeJup):
        def usage(self):
            return {**budget.snapshot(), "keyed": False}

    engine.clients.jup = Usage(engine.clients.dex)
    texts = [alert["text"] for alert in engine.status()["alerts"]]
    assert any("portal.jup.ag" in text and "JUPITER_API_KEY" in text for text in texts)
    assert engine.status()["engine"]["jupiter"]["limited_total"] == 1
    # Uden Claude-nøgle kommer der kun én besked om X, ikke to.
    assert not any("X_BEARER_TOKEN" in text for text in texts)
