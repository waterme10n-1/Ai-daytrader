# AI Daytrader

Daytrader til likvide amerikanske aktier og ETF'er. Motoren handler kun i to vinduer omkring åbning og lukning, flader alt ud kl. 15:50 amerikansk østkysttid og starter på papirpenge. Rigtige penge kan først slås til, når en række krav er opfyldt i koden — ikke kun i brugerfladen.

Univers: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA og AMD. SPY er markedspejlet.

## Kør lokalt

Kræver Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Åbn [http://127.0.0.1:8741](http://127.0.0.1:8741). Uden login-variabler binder serveren til `127.0.0.1` og kræver ikke login. Sæt `HOST` til noget andet end loopback, og processen nægter at starte, før `APP_USERNAME`, `APP_PASSWORD` eller `APP_PASSWORD_HASH`, og `SESSION_SECRET` er udfyldt.

Uden nøgler kan du stadig bruge **Simulér én dag**. Knappen kører en hel børsdag gennem den rigtige motor med syntetiske kurser og regelbaseret analyse. Syntetiske resultater siger intet om rigtig performance, og de tæller ikke med i kravene for at låse rigtige penge op.

## Alpaca-papirnøgler

1. Opret en konto på [alpaca.markets](https://alpaca.markets) og åbn paper trading.
2. Under API Keys: opret et nøglesæt til paper.
3. Sæt dem i `.env`:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
```

Papirordrer sendes til `https://paper-api.alpaca.markets`. Markedsdata hentes fra `https://data.alpaca.markets` med `feed=iex` (gratisniveauet). Samme kodevej bruges til live, bare mod `https://api.alpaca.markets` og med live-nøglerne.

## Anthropic-nøgle

1. Opret en nøgle i [Claude Console](https://console.anthropic.com/).
2. Sæt den i `.env`:

```
ANTHROPIC_API_KEY=...
```

Standardmodellen er `claude-opus-5-5`. Websøgning bruger værktøjet `web_search_20260209` (højst 5 opslag), indsats `high` og server-side fallback. Model, værktøjsversion, indsats og fallback kan ændres i `.env` uden at røre koden.

Hvis `ANTHROPIC_API_KEY` er tom, eller Claude svarer med en fejl, kører analysen videre på de faste regler med sikkerhed 70 og en tydelig mærkning om, at det ikke er en modelvurdering. Et afvisningssvar fra Claude springer kandidaterne over i stedet for at handle alligevel.

## Telegram (valgfrit)

Sæt både `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID`, hvis du vil have besked ved handler, lukninger, dagligt tab og fejl. Uden begge sker der ingenting.

## Fra papir til rigtige penge

Alle punkter skal være sande på samme tid, og de tjekkes i koden:

- mindst 60 papirhandelsdage
- mindst 50 lukkede papirhandler
- nettoresultat over 0
- profitfaktor mindst 1,2
- maksimalt drawdown under 10 %
- `ALLOW_LIVE=true`
- `ALPACA_LIVE_API_KEY` og `ALPACA_LIVE_SECRET_KEY` er udfyldt
- du skriver præcis `JEG FORSTÅR AT JEG KAN TABE RIGTIGE PENGE`

Simulerede handler tæller ikke. Live-størrelse bruger aldrig mere end `LIVE_MAX_CAPITAL` (standard 1.000 USD), også hvis kontoen er større. Ved genstart falder systemet tilbage til papir, hvis kravene ikke længere holder. Knappen **Tilbage til papirpenge** lukker live-positioner.

Med egenkapital under 25.000 USD i live blokeres nye handler, når Alpaca melder 3 eller flere daytrades.

## Udgiv på Railway

Tjenesten skal køre som ét eksemplar. SQLite og motortråden kan ikke deles mellem processer. Image bygges fra `Dockerfile`, som starter `python run.py` med én worker. `/healthz` er åben og bruges som healthcheck.

1. Opret en tjeneste ud fra dette repo. Railway opdager `Dockerfile`.
2. Tilføj et volume og montér det på `/data`.
3. Sæt miljøvariablerne nedenfor. `PORT` sætter Railway selv.
4. Generér et domæne under Settings → Networking. Åbn det og log ind.

Påkrævede variabler:

```
HOST=0.0.0.0
DB_PATH=/data/trader.db
APP_USERNAME=dit-brugernavn
APP_PASSWORD=en-lang-adgangskode
SESSION_SECRET=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ANTHROPIC_API_KEY=...
```

`SESSION_SECRET` skal være mindst 32 tegn:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Vil du ikke have adgangskoden i klartekst, så lav en hash og sæt `APP_PASSWORD_HASH` i stedet for `APP_PASSWORD`:

```bash
python -c "from trader.auth import hash_password; print(hash_password('en-lang-adgangskode'))"
```

Cookie sættes med `HttpOnly` og `SameSite=Strict`. Bag Railway (HTTPS) sættes også `Secure`, fordi proxyen sender `X-Forwarded-Proto: https`. Fem forkerte login-forsøg låser i et kvarter. Tilstandsskiftende POST kræver headeren `X-CSRF-Token`.

Valgfrit, samme navne som lokalt: `ALPACA_LIVE_API_KEY`, `ALPACA_LIVE_SECRET_KEY`, `ALLOW_LIVE`, `LIVE_MAX_CAPITAL`, `ANTHROPIC_MODEL`, `ANTHROPIC_WEB_SEARCH_TOOL`, `ANTHROPIC_EFFORT`, `ANTHROPIC_FALLBACKS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Skaler ikke tjenesten til mere end ét replika. Et nyt replika ville åbne sin egen motor mod samme databasefil.

## Test

```bash
pytest
```

## Claude Code-connectors (MCP)

`.mcp.json` i roden giver Claude Code fire connectors, når du arbejder i dette repo. Claude Code spørger første gang, om du vil godkende dem.

| Connector | Hvad den gør | Kræver |
|---|---|---|
| perplexity | Søger på nettet med kilder | `PERPLEXITY_API_KEY` (console.perplexity.ai, betales pr. brug) |
| firecrawl | Læser en webside som ren tekst | Intet til basisbrug |
| playwright | Egen browser: klik, formularer, test | Intet. Node.js 18+ |
| posthog | Svarer ud fra dine PostHog-tal | `POSTHOG_PERSONAL_API_KEY` (PostHog → Personal API keys) |

Nøglerne sættes som miljøvariabler i din shell, ikke i filen, så de aldrig ender i git:

```bash
export PERPLEXITY_API_KEY="..."
export POSTHOG_PERSONAL_API_KEY="..."
```

Composio er ikke med i filen, fordi den installeres med sit eget script og et browser-login. Kør på din egen maskine: `curl -fsSL https://composio.dev/install | sh`, genstart terminalen og kør `composio login`.

Tjek i Claude Code med `/mcp`. Fjern en med `claude mcp remove <navn>`.
