# CLAUDE.md

Noter til Claude Code om dette repo. Brugerens dokumentation står i `LÆS_MIG.md`.

## Hvad det er

Én FastAPI-app med to dele, der kører i samme proces:

- **Memebot** (`/meme`, `trader/meme/`): memecoins på Solana. Papirpenge med rigtige kurser fra DexScreener og Jupiter, indtil live-kravene er opfyldt.
- **Handelsbordet** (`/desk`, `trader/desk/`): seks bots (SCOUT, NEWS, CHART, SKEPTIC, SIZER, EXECUTOR). USA handler på Alpacas papirkonto. Danmark (`?market=dk`) har sin egen papirkonto i kroner med kurser fra Yahoo (`denmark.py`).

Vigtige filer: `trader/app.py` (alle ruter), `trader/auth.py` (login, CSRF, TOTP), `trader/meme/engine.py` og `trader/desk/engine.py` (motorerne), `trader/static/meme.html` og `desk.html` (hele brugerfladen i én fil hver, uden build-trin).

## Kommandoer

```bash
pip install -r requirements.txt
pytest                                  # ca. 15 s, ingen nøgler eller netværk
python tests/meme_soak.py 50 250 hard   # stresstest af memebotten efter ændringer i motor, risiko eller wallet
python run.py                           # http://127.0.0.1:8741, intet login på 127.0.0.1
```

Testene bruger falske datakilder (`tests/meme_fakes.py`). Nye tests må ikke ramme netværket.

## Regler, der ikke må brydes

- **Papir først.** Svæk aldrig live-kravene i `MemeEngine._unlock` og `request_live` (`UNLOCK_MIN_*`, `MEME_ALLOW_LIVE`, `MEME_LIVE_PHRASE`), medmindre brugeren udtrykkeligt beder om det. Bordet taler kun med `paper-api.alpaca.markets` (`AlpacaPaper` i `trader/desk/alpaca.py`) og har ingen live-parameter. Sådan skal det blive.
- **Én proces.** `workers=1`, én motortråd og SQLite. Tilføj ikke flere workers, processer eller replikaer.
- Commit aldrig `.env`, API-nøgler eller wallet-nøgler.

## Konventioner

- Al tekst, brugeren ser (sider, hændelser, fejl, Telegram), og al dokumentation er på **dansk**. Tal skrives med komma: `15,00 $`, `2,5 %`.
- En ny indstilling får en miljøvariabel i `.env.example`, en linje i `LÆS_MIG.md` og helst et felt på siden.
- Ændres en standardværdi på bordet, så brug `_retire_old_defaults` i `trader/desk/engine.py`, så gemte gamle standarder giver plads én gang.
- Opdatér `LÆS_MIG.md`, når noget, brugeren ser eller indstiller, ændrer sig.
- Commit-beskeder er på engelsk: titlen siger, hvad brugeren får, og brødteksten forklarer hvorfor, gerne med punkter.

## Tjek brugerfladen

Efter ændringer i `trader/static/*.html`: start appen og tag skærmbilleder af `/meme` og `/desk` i telefonstørrelse med Playwright (forudinstalleret i cloud-sessioner, `require('playwright')`). Tjek, at der ikke er JS-fejl i konsollen eller vandret scroll, og se på billederne. Siderne loader uden nøgler og viser så forbindelsesfejl i stedet for data.

## Claude i appen

Standardmodellen er sat i `trader/meme/config.py` og `trader/desk/config.py` (`MEME_ANTHROPIC_MODEL`, `DESK_ANTHROPIC_MODEL`, `ANTHROPIC_MODEL`). Prisen pr. model, som bruges til "AI i dag", står i `trader/meme/research.py`. Skifter modellen, skal begge steder opdateres.
