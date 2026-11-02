"""Start AI Daytrader. Ét worker-proces, så der kun kører én motorthread."""

from __future__ import annotations

import logging
import sys

import uvicorn

from trader.auth import enforce_startup
from trader.config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    try:
        enforce_startup(settings)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    print(f"AI Daytrader kører på http://{settings.host}:{settings.port}")
    # workers=1 er påkrævet: SQLite og motortråden må ikke deles mellem processer.
    uvicorn.run(
        "trader.app:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
