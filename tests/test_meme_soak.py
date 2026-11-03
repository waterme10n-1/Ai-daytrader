"""Stresstest i testsuiten: tilfældige markeder med pumps, dumps, rugs, nedbrud og knaptryk.

Efter hver runde tjekkes, at regnskabet stemmer, at grænserne holder, og at intet går galt.
Kør flere seeds direkte med: python tests/meme_soak.py 50 250 hard
"""

import pytest

from meme_soak import run


@pytest.mark.parametrize("seed", [101, 202, 303])
def test_random_market_normal(seed):
    result = run(seed, ticks=80)
    assert result["trades"] >= 0


@pytest.mark.parametrize("seed", [7, 77])
def test_random_market_hard(seed):
    run(seed, ticks=80, profile="hard")
