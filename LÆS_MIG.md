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

Testene kører med falske datakilder og en falsk Solana-kæde, så de kræver hverken nøgler eller netværk. Der er også en stresstest, der kører memebotten gennem tilfældige markeder med pumps, dumps, rugs, nedbrud i datakilderne og tilfældige knaptryk, og efter hver runde tjekker, at regnskabet stemmer, og at ingen grænse er brudt:

```bash
python tests/meme_soak.py 50 250 hard
```

---

# Memebot (Solana-memecoins)

Memebotten kører i samme proces som aktiemotoren, 24/7, og har sit eget dashboard på `/meme` (link øverst på forsiden). Den starter **altid på papirpenge**: rigtige kurser fra DexScreener og rigtige quotes fra Jupiter, men ingen transaktioner.

## Dashboardet

Efter login lander du på `/meme`. Siden opdaterer sig selv hvert 8. sekund.

- **Oversigt**: egenkapital med graf (24 t, 7 d eller alt), målet om Claude Max, seneste scanning som tragt (fundet → marked → rug-tjek → score → købt), aktivitet, dagligt resultat og hvorfor botten solgte.
- **Sniper**: tænd og sluk, sæt dit gevinstmål og stop-loss, og se de nye tokens botten har set på.
- **Positioner**: et kort pr. position med en skala, der viser kursen i forhold til stop-loss, købskurs, delsalg og trailing stop, samt links til DexScreener, RugCheck, Solscan og X. Knappen **Sælg nu** sælger med det samme.
- **Radar**: hvert token botten har set på, hvilket trin det faldt ved og hvorfor. Filtrér og søg, og bloker tokens, botten aldrig må købe.
- **Journal**: alle lukkede handler. Klik på en række for begrundelse og transaktioner.
- **Læring**: lektioner, signalvægte, kilder og strategiparametre.
- **Wallet**: bot-wallet'ens saldo, forbind din Phantom, indbetal og hæv, Axiom-genveje, samt wallets botten følger (tilføj og fjern uden genstart).
- **Indstillinger**: kravene før rigtige penge og alle indstillinger.

Knapper: **Scan nu**, **Pause køb** (åbne positioner passes stadig) og **Nødstop** (sælger alt). Klokken øverst slår browser-notifikationer til ved køb og salg. Genveje: `1`–`8` skifter fane, `S` scanner, `P` sætter køb på pause, `/` søger i radaren og `T` skifter tema.

## Forbindelser, sikkerhed og opfølgning

**Test forbindelser.** Ved opstart, og når du trykker på knappen under Indstillinger, kalder botten Jupiter, DexScreener, RugCheck, Solana RPC (og din wallet, X, Claude, Telegram og Jito, hvis de er sat op) og viser, hvad der virker. Ved hver fejl står der, hvad du skal rette. Gør det som det første efter udgivelsen.

**Realistisk papirhandel.** Papirhandler fejler engang imellem (standard 5 %), og så betales gebyret alligevel. Der lægges 1 % ekstra slippage på hver handel og 2 % mere på snipes, fordi kursen når at flytte sig, før en rigtig handel lander. Så bliver papirresultatet ikke for flot, før du slår rigtige penge til.

**Skyggehandler.** Botten følger kursen på de tokens, den afviste, og dem den købte, i 6 timer og regner ud, hvad der ville være sket med dens egne salgsregler. Under Læring kan du se, om afviste tokens faldt bagefter (godt valgt) eller steg (misset chance). Modellen lærer også lidt af de missede chancer. Hvis halvdelen af de tokens, scoren afviste, steg bagefter, sænker botten selv kravet en smule. Sikkerhedsfiltrene løsnes aldrig automatisk.

**Hvilket mål ville have virket?** På Sniper-fanen bruger botten kursforløbene fra de tokens, den købte eller næsten købte, til at regne gevinstmål fra +25 % til +500 % mod stop fra -20 % til -50 % igennem. Fra 20 forløb og op viser den gennemsnittet efter gebyrer, og med én knap kan du bruge det bedste. Fortiden er ingen garanti, og kurserne måles kun én gang i minuttet.

**Udvikler-alarm.** Ved hvert køb noterer botten udvikleren og de tre største holdere (fra RugCheck) og tjekker deres beholdning hvert minut. Sælger udvikleren mindst halvdelen eller topholderne tilsammen 40 %, sælger botten alt, og tokenet blokeres.

**To-trins-login.** Kør `python -m trader.auth totp`, tilføj nøglen i en godkendelses-app (Google Authenticator, 1Password, Authy) og sæt `APP_TOTP_SECRET` i Railway. Så kræver login en kode, og siden spørger om en ny kode, før der hæves, og før rigtige penge slås til.

**Telegram.** Med `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID` kan du skrive til botten: `/status`, `/positioner`, `/pause`, `/fortsaet`, `/saelg BONK`, `/nodstop ja`, `/genoptag`, `/sniper til|fra`, `/maal 150`, `/rapport` og `/hjaelp`. Kun beskeder fra din egen chat besvares, og der kan ikke hæves penge eller slås rigtige penge til via Telegram. Hver dag kl. 19 UTC (`MEME_REPORT_HOUR_UTC`) kommer en dagsrapport med resultat, udgifter, skyggehandler og dagens lektioner.

**Ærligt regnestykke.** Hosting (`MEME_HOSTING_USD_PER_MONTH`, standard 5 USD for Railway) og andre abonnementer (`MEME_OTHER_COSTS_USD_PER_MONTH`, fx X API eller en betalt RPC) trækkes fra sammen med Claude-forbruget. Målet viser, hvad botten reelt skal tjene for at betale Claude Max.

**Hurtigere handler.** Stop-loss og rug-salg sendes med højere prioritetsgebyr, og fejler de, prøves de igen med dobbelt slippage. Med `MEME_EXECUTION=jito` sendes handler med et tip til Jitos block engine, så de lander oftere og ikke ses i den offentlige strøm.

## Ærlige forventninger

- Claude Max koster 100 eller 200 USD om måneden. Fra 15 USD kræver det over 650 % afkast hver måned. Det sker næsten aldrig. Regn med, at indsatsen kan gå tabt.
- De fleste nye memecoins går mod nul, og mange er decideret svindel. Rug-tjekkene fanger de klassiske fælder, ikke alle.
- Claude-API'et betales separat fra et Max-abonnement. Hosting på Railway koster også et par dollars om måneden. Dashboardet trækker AI-udgifterne fra resultatet, så du ser det reelle tal.
- Papirresultater er bedre end virkeligheden: rigtige handler kan fejle, blive front-runnet eller få dårligere pris.

## Hvad botten gør

Hvert minut:

1. **Finder kandidater** fra Jupiter (trending og organisk score), DexScreener (boosts og nye profiler) og køb fra wallets, du følger.
2. **Markedsfiltre**: likviditet, volumen, antal handler, alder, markedsværdi, ikke overophedet, flere købere end sælgere, ikke på en bonding curve.
3. **Rug-tjek** via RugCheck og Jupiter: mint- og freeze authority skal være slået fra, ingen "danger"-risici, begrænset ejerkoncentration, udvikleren må ikke sidde på store beholdninger. Kan RugCheck ikke svare, købes der ikke.
4. **Exit-tjek**: botten beder Jupiter om en købs- og en salgspris. Kan tokenet ikke sælges igen, eller koster frem og tilbage mere end 8 %, købes der ikke (honeypot- og tynd-pool-filter).
5. **Score**: en lille læringsmodel vægter signalerne (købspres, likviditet, organisk handel, sociale links, X-omtale, copy-signal, rug-risiko m.fl.).
6. **Claude** (hvis nøgle og budget er sat) søger på X, nyheder og fora efter røde flag: falske konti, koordineret shill, kopier af kendte tokens, rug-historik. Claude kan kun sige nej. Claude kan aldrig tvinge et køb igennem eller ændre risikogrænserne.
7. **Risiko**: højst `MEME_POSITION_USD` pr. handel, aldrig mere end `MEME_BANKROLL_USD` i alt, højst `MEME_MAX_OPEN` åbne og `MEME_MAX_TRADES_PER_DAY` køb om dagen.

Hvert 15. sekund overvåges åbne positioner: stop-loss (-25 %), halvdelen sælges ved +60 %, trailing stop 25 % fra toppen, salg hvis likviditeten trækkes (rug-alarm), hvis intet sker, eller hvis den fulgte trader sælger. Tomme token-konti lukkes, så rent-depositummet kommer tilbage.

## Sniper: helt nye tokens

Sniperen er en ekstra strategi ved siden af den almindelige. Hvert 10. sekund henter den tokens, hvis første pool lige er oprettet (Jupiter), og nye profiler fra DexScreener. Et token købes kun, hvis det består alt dette:

1. Det er mellem `Vent mindst` (standard 60 sek.) og `Maks. alder` (standard 30 min.) gammelt. Botten venter med vilje lidt, fordi mange rugs sker i det første minut.
2. Det er ikke på en bonding curve, har mindst 8.000 USD likviditet, ikke dumper lige nu og ikke har massivt salg.
3. Mint- og freeze authority er slået fra, udvikleren ejer højst 5 %, top-10 ejer højst 30 %, og der er mindst 25 holdere.
4. Det har X, Telegram eller en hjemmeside (kan slås fra).
5. RugCheck har ingen alvorlige advarsler, og Jupiter kan både købe og sælge tokenet igen uden stort tab (honeypot-tjek).
6. Risikogrænserne tillader det: standard 1,50 USD pr. snipe, højst 2 åbne snipes og 10 om dagen, og bankroll-loftet gælder.

Salg sker ved **dit gevinstmål** (standard +100 %). Du kan vælge at sælge det hele ved målet eller kun en del og lade resten følge toppen. Der er også stop-loss (standard -35 %), rug-alarm hvis likviditeten trækkes, og en tidsgrænse (standard 60 min.).

Alt kan ændres på fanen **Sniper** i dashboardet: tænd og sluk, gevinstmål (med knapper til +25 % op til +500 %), andel der sælges, stop-loss, beløb pr. snipe, holdetid og filtrene. Siden viser også, hvor mange af dine snipes der skal ramme målet, før du går i nul. Læringen må kun stramme filtrene (fx efter rugs), aldrig ændre dit mål, dit stop eller beløbet.

Vær klar over:

- Sniping er den mest risikable måde at handle memecoins på. De fleste nye tokens rammer aldrig +100 %.
- Professionelle snipere køber i samme blok, som poolen oprettes, med private noder. Denne bot er bevidst langsommere, fordi den tjekker tokenet først. Den køber derfor sjældent i bunden.
- Claude bruges ikke til snipes som standard, fordi research tager op til et par minutter. Du kan slå det til, men så kommer botten senere ind.

## Sådan lærer den

- Efter hver lukket handel flyttes vægtene på de signaler, der bar handlen (op ved gevinst, ned ved tab). Dashboardet viser vægtene og en lektion pr. handel.
- Hver 5. handel justeres filtrene ud fra mønstre: mange rugs giver strengere rug-krav, mange stop-loss giver krav om mere likviditet og mindre jagt på toppe, og så videre.
- En gang i døgnet gennemgår Claude de seneste handler og skriver lektioner, som bruges i næste research. Claude må foreslå ændringer af filtrene, men kun inden for faste grænser.
- Kilder og fulgte wallets, der taber over tid, bliver automatisk slået fra.
- Positionsstørrelse, bankroll og tabsgrænser kan hverken læringen eller Claude ændre.

## Tabsgrænser

- Dagligt tab på `MEME_DAILY_LOSS_PCT` af bankroll (standard 25 %): ingen nye køb resten af døgnet (UTC). Åbne positioner beholder deres stop.
- Samlet tab på `MEME_MAX_DRAWDOWN_PCT` (standard 50 %): alt sælges, og botten står stille, indtil du trykker **Genoptag**.
- **Nødstop** sælger alt med det samme.

## Fra papir til rigtige penge

Alle punkter skal være opfyldt, og de tjekkes i koden:

- mindst 20 lukkede papirhandler, fordelt på mindst 3 dage
- papirresultat over 0 og profitfaktor mindst 1,2
- `MEME_ALLOW_LIVE=true`
- `SOLANA_PRIVATE_KEY` kan læses
- du skriver præcis `JEG FORSTÅR AT JEG KAN TABE HELE INDSATSEN` på `/meme`

### Sådan sætter du wallet'en op (vigtigt)

1. Opret en **ny konto** i Phantom (Konti → Tilføj/forbind → Opret ny konto). Brug aldrig din hovedkonto.
2. Send kun det beløb, botten må tabe, til den nye konto: fx 15 USD i SOL plus ca. 0,03 SOL til gebyrer og depositum for token-konti.
3. Vis den private nøgle for **kun den konto** (Indstillinger → Administrer konti → kontoen → Vis privat nøgle). Del aldrig din recovery-sætning (seed phrase). Den giver adgang til alle dine konti.
4. Sæt nøglen som hemmelig variabel `SOLANA_PRIVATE_KEY` hos Railway. Indsæt den aldrig i en chat, en fil i repoet eller en besked. Nøglen bruges kun til at signere lokalt i botten og vises aldrig i dashboardet.
5. Opret en gratis nøgle hos [helius.dev](https://www.helius.dev) og sæt `SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...`. Den offentlige RPC er for langsom til at handle sikkert.

### Phantom i dashboardet

Fanen **Wallet** på `/meme` kobler botten sammen med Phantom:

- **Bot-wallet**: saldo, adresse og link til Solscan. Fordi bot-wallet'en er en konto i din Phantom, kan du også følge hver handel direkte i Phantom-appen.
- **Forbind Phantom**: forbind din hovedkonto (Phantom-udvidelsen på computeren, eller åbn dashboardet inde i Phantom-appen på mobilen). Så kan du **indbetale** til botten med ét klik. Phantom viser beløbet, og du godkender selv. Botten kan aldrig hæve fra din hovedkonto.
- **Hæv til din Phantom**: send SOL fra bot-wallet'en tilbage til din hovedkonto. Det virker kun til adressen i `MEME_WITHDRAW_ADDRESS`, som du selv sætter i Railway. Så kan ingen omdirigere pengene, heller ikke hvis nogen skulle komme ind i dashboardet. Reserven til gebyrer bliver altid stående.
- **Automatisk udbetaling**: sæt `MEME_SWEEP_ABOVE_USD` (fx 5). Når botten handler live og har mere end bankroll + 5 USD i fri SOL, sendes overskuddet til din Phantom, højst hver halve time. Gevinsten havner altså hos dig og ikke i botten.

### Axiom

Axiom har ingen officiel API til bots. Der findes uofficielle biblioteker, men de kræver dit Axiom-login, og det giver adgang til din Axiom-konto. Derfor handler botten ikke gennem Axiom. Du kan bruge Axiom ved siden af:

- Knappen **AX** ved hvert token åbner det i Axiom. Linkformatet kan ændres med `MEME_AXIOM_LINK`, hvis Axiom skifter det.
- Tilføj bot-wallet'ens adresse i Axioms wallet tracker for at følge dens handler dér.
- Find dygtige traders i Axiom og indsæt deres adresser under **Fulgte wallets** (der er også et felt til mange adresser på én gang).

Botten bruger højst `MEME_BANKROLL_USD` i alt, også hvis du sender flere penge til kontoen. Knappen **Tilbage til papirpenge** sælger alle live-positioner.

## Valgfrie tilføjelser

- **Claude-research**: `ANTHROPIC_API_KEY` og `MEME_AI_DAILY_BUDGET_USD` (standard 0,50 USD om dagen). Standardmodellen er `claude-opus-5-5` med indsats `medium` og websøgning. Er budgettet brugt, kører botten videre på regler. Vil du bruge en billigere model, kan du sætte `MEME_ANTHROPIC_MODEL`. Ældre modeller skal muligvis have `MEME_ANTHROPIC_WEB_SEARCH_TOOL=web_search_20250305`.
- **X API**: `X_BEARER_TOKEN` fra developer.x.com (betalt). Botten søger efter kontraktadressen og tickeren og måler, hvor mange forskellige konti der skriver om tokenet. `X_DAILY_REQUESTS` begrænser forbruget.
- **Copy-trading**: `MEME_COPY_WALLETS=adresse1,adresse2`. Find kandidater på fx GMGN, Birdeye eller Kolscan, og tjek selv deres historik. Første gang botten ser en wallet, husker den kun, hvor den er nået til; gamle handler kopieres aldrig. Et køb fra en fulgt wallet skal stadig bestå alle tjek ovenfor.
- **Telegram**: samme `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID` som aktiemotoren. Du får besked ved køb, salg, tabsgrænser og fejl.

## Kør 24/7

Lokalt: `python run.py` og åbn [http://127.0.0.1:8741/meme](http://127.0.0.1:8741/meme). Computeren skal så være tændt hele tiden.

På Railway: følg afsnittet *Udgiv på Railway* ovenfor. Memebotten gemmer sin database i `/data/meme.db` på samme volume. Tilføj de `MEME_*`-variabler, du vil ændre. Alle har fornuftige standarder, så botten kører på papir uden ekstra opsætning.

Datakilder: DexScreener, RugCheck og Jupiter (`lite-api.jup.ag`) kræver ingen nøgle. Hvis Jupiter lukker lite-api, så opret en gratis nøgle på portal.jup.ag og sæt `JUPITER_API_URL=https://api.jup.ag` og `JUPITER_API_KEY`.
