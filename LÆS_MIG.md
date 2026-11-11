# Memebot

En 24/7-bot til memecoins på Solana. Den finder nye og trendende tokens, tjekker dem for rug (RugCheck og Jupiter), sikrer at de kan sælges igen, kan følge udvalgte wallets, og lærer af hver lukket handel. Den starter **altid på papirpenge**: rigtige kurser fra DexScreener og rigtige quotes fra Jupiter, men ingen transaktioner. Rigtige penge kan først slås til, når en række krav er opfyldt i koden, ikke kun i brugerfladen.

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

## Claude-nøgle (valgfri)

Med `ANTHROPIC_API_KEY` researcher Claude hver kandidat på X og nyheder, før botten køber, og gennemgår dagens fejl. Opret nøglen i [Claude Console](https://console.anthropic.com/). Forbruget betales separat fra et Claude Max-abonnement. Der er intet dagligt loft som standard, så Claude tjekker coins hele dagen. Hvert tjek koster ca. 0,15-0,40 USD, og du kan følge forbruget under **AI i dag** i dashboardet. Vil du have et loft, slår du **Dagligt AI-loft** til under Mere → Indstillinger → Tabsgrænser. Uden nøgle kører botten på regler og RugCheck.

## Udgiv på Railway

Tjenesten skal køre som ét eksemplar. SQLite og motortråden kan ikke deles mellem processer. Image bygges fra `Dockerfile`, som starter `python run.py` med én worker. `/healthz` er åben og bruges som healthcheck.

1. Opret en tjeneste ud fra dette repo. Railway opdager `Dockerfile`.
2. Tilføj et volume og montér det på `/data`.
3. Sæt miljøvariablerne nedenfor.
4. Generér et domæne under Settings → Networking med port `8741`. Åbn det og log ind.

Påkrævede variabler:

```
HOST=0.0.0.0
PORT=8741
DB_PATH=/data/trader.db
APP_USERNAME=dit-brugernavn
APP_PASSWORD=en-lang-adgangskode
SESSION_SECRET=...
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

Anbefalet: `JUPITER_API_KEY` (gratis fra portal.jup.ag) og `SOLANA_RPC_URL` (gratis Helius-nøgle). Valgfrit: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `APP_TOTP_SECRET` og alle `MEME_*`-variabler i `.env.example`.

Skaler ikke tjenesten til mere end ét replika. Et nyt replika ville åbne sin egen motor mod samme databasefil.

## Test

```bash
pytest
```

Testene kører med falske datakilder og en falsk Solana-kæde, så de kræver hverken nøgler eller netværk. Der er også en stresstest, der kører memebotten gennem tilfældige markeder med pumps, dumps, rugs, nedbrud i datakilderne og tilfældige knaptryk, og efter hver runde tjekker, at regnskabet stemmer, og at ingen grænse er brudt:

```bash
python tests/meme_soak.py 50 250 hard
```

## Dashboardet

**Udseende.** Under **Menu → Udseende** (Memebot) eller **Indstillinger → Udseende** (Handelsbordet) vælger du baggrund (Mørk, Midnat, Lys eller Telefon, som følger telefonens indstilling) og en af ti farver (lime, som er standard, blå, indigo, lilla, pink, orange, guld, grøn, mint og Solana). Valget gælder begge sider og huskes på enheden.

Efter login lander du på `/meme`. Siden er bygget til mobilen først og opdaterer sig selv hvert 8. sekund. Memebot og Handelsbordet har nu samme udseende: de samme farver, skrifter og kort. Du skifter mellem lys og mørk tilstand i **Menu**, og valget huskes på telefonen. Navigationen ligger i bunden på telefonen og øverst på en computer, ligesom på handelsbordet.

- **Hjem → Relæ** (det første, du ser) viser botten som fem agenter, der giver stafetten videre:
  - **Spejder** finder nye tokens (scanningen, sniperen og Top 10-signaler) og tæller, hvor mange den har set.
  - **Analytiker** tjekker hvert token: RugCheck (rug, mint/freeze, LP, ejerfordeling), Jupiters audit (mistænkelig, serie-udvikler, organisk score), insidere, X-omtale og sociale links. Tallet er andelen, der består.
  - **Køber** udfører købet for den strategi, tokenet kom fra (almindelig, sniper eller Top 10), inkl. prøvekøb.
  - **Risiko** følger de åbne positioner (stop-loss, trailing stop, rug-alarm).
  - **Kasserer** sælger og viser det samlede realiserede resultat.

  Over relæet står egenkapitalen med en gange-faktor i forhold til startbeløbet, under det en aktivitetslog med agentens farve, og nederst en målstige (1,25×, 1,5×, 2×, 3×, 5× og 10×). Alle tal kommer fra bottens rigtige kandidater, positioner og handler. Alle tre strategier går gennem den samme analytiker, også Top 10, som nu også tjekker Jupiters audit, insidere og X.
- **Hjem → Kontrolrum** er bottens kontrolrum. Alt, der tæller, tæller rigtigt: siden får de præcise tider fra motoren og tæller selv ned sekund for sekund.
  - Øverst står klokken og hvad botten laver lige nu (fx *Scan om 0:42* eller *Scanner · 0:03*). Under den kører et bånd med dine åbne positioner og dagens tal.
  - **Egenkapitalen** ruller som på en tavle og blinker grønt eller rødt, når den ændrer sig. Grafen viser 24 t, 7 d eller alt.
  - **Motoren** er en reaktor med tre ringe: den yderste er markeds-scanningen (hvert minut, ét lys pr. sekund), den midterste sniperen (hvert 10. sekund) og den inderste Top 10 (hvert 8. sekund). Ringen fyldes, mens der tælles ned, og drejer rundt, mens opgaven kører. I midten står tiden til næste scanning. Ved siden af står, hvad sidste runde fandt, og nederst hvornår positionerne sidst blev tjekket, hvor længe botten har kørt, og hvornår den nye dag (UTC) starter. Trykker du **Scan nu**, kan du se scanningen køre med det samme. Mister siden forbindelsen, står der *Ingen forbindelse* i stedet for en nedtælling, der ikke passer.
  - **Åbne positioner** er fliser med kursen den seneste halve time, hvor langt kursen er fra stop og mål, et ur der viser, hvor længe botten har holdt den, og hvor lang tid der er tilbage, før den senest bliver solgt. Prikken er grøn, når kursen er frisk. Graferne gemmes, så de også er der efter en genstart.
  - **Radaren** viser hver coin, botten har kigget på. Grøn er købt, gul fik for lav score, orange faldt på risiko, blå faldt på markedsfiltrene og rød på rug-tjekket. Jo tættere på midten, jo længere nåede den. Prikkerne lyser op, når strålen passerer dem, og nye coins blinker. Tryk på en prik for at se hvorfor.
  - Derefter **Hvad tjener penge?** (med kontakten til strategi-vagten), **Datakilder** (hvor meget af Jupiter-, RugCheck- og DexScreener-kvoten der er brugt det seneste minut), **Log**, målet om Claude Max og seneste lektion. Nøgletal, tragten for seneste scanning, dagligt resultat og hvorfor botten solgte ligger under **Flere tal og grafer**.
- **Handler**: **Åbne** viser et kort pr. position med en skala for kursen i forhold til stop-loss, købskurs, delsalg og trailing stop, links til DexScreener, RugCheck, Solscan, X og Axiom, og knappen **Sælg nu**. **Historik** viser alle lukkede handler. Tryk på en handel for begrundelse og transaktioner, og for **Hvorfor den tabte** (eller vandt): hvordan den sluttede, og hvilke af tokenets tal der hang sammen med tab eller gevinst. Når der er nok skyggehandler, bygger forklaringen på bottens egne data ("tokens med organisk score under 37 gav i snit -20 %"). Ellers nævner den kendte faresignaler som lav organisk score, mange insidere eller køb efter en stor stigning. Ved tabere står den vigtigste grund også under handlen i listen.
- **Strategi → Almindelig**: **Antal handler** (maks. køb pr. dag og åbne for den almindelige strategi og sniperen, og prøvekøb pr. dag; gælder papirkontoen) og alle filtre og salgsregler for den almindelige strategi (likviditet, organisk score, insidere, stop-loss, gevinstmål osv.), altid inden for faste grænser. Tal, du selv retter, rører læringen ikke i 7 dage (der står "sat af dig"). **Nulstil** sætter alt tilbage til standard.
- **Sniper**: tænd og sluk, sæt gevinstmål og stop-loss, filtre for nye tokens, hvilket mål der ville have virket, og de nye tokens botten har set. **Radar** viser hvert token botten har set på, hvilket trin det faldt ved og hvorfor. Filtrér, søg og bloker tokens.
- **Wallet**: bot-wallet'ens saldo, forbind din Phantom, indbetal og hæv, wallets botten følger, Axiom-genveje og blokerede tokens.
- **Mere**: **Læring** (lektioner, signalvægte, kilder, skyggehandler, Hvad virker?, **Automatiske ændringer** og strategiparametre) og **Indstillinger** (app på hjemmeskærmen, papirkontoen, kravene før rigtige penge, forbindelsestest, sikkerhed, alle indstillinger og Telegram).

**Start papirkontoen forfra.** Under Mere → Indstillinger → **Papirkonto** skriver du et beløb (fx 50 USD) og trykker **Start forfra**. Så sker der følgende:
- Åbne papirpositioner lukkes, og resultaterne starter fra nul.
- De gamle handler gemmes i et arkiv i databasen.
- Botten beholder alt, den har lært: vægte, lektioner, skyggehandler og mål.
- Beløbene pr. handel skaleres med. Med standardindstillingerne bliver 50 USD til 10 USD pr. almindelig handel, 5 USD pr. snipe og 6,67 USD pr. Top 10-handel. Faste gebyrer fylder så meget mindre i hver handel.

Det gælder kun papir. Med rigtige penge er `MEME_BANKROLL_USD` stadig det hårde loft. Efter opdateringen i november 2026 starter en papirkonto, der allerede har handlet, automatisk forfra med 50 USD én gang.

Alt, der flytter penge eller ændrer botten (sælg, nødstop, bloker, hæv, nulstil), spørger først i et vindue nederst på skærmen. Info-beskeder vises kun på Hjem, og en besked, du har lukket, forbliver lukket. Klokken i menuen slår notifikationer til ved køb og salg. På en computer: `1`–`9` skifter side, `S` scanner, `P` sætter køb på pause, `/` søger i radaren og `T` skifter tema.

**Som app på telefonen.** Åbn dit link på telefonen og log ind. På iPhone (Safari): tryk på Del-knappen (firkanten med pilen op) og vælg **Føj til hjemmeskærm**. På Android (Chrome): tryk på **⋮** og vælg **Installer app**, eller brug knappen **Installer app** under Mere → Indstillinger. Så får du et Memebot-ikon, og dashboardet åbner uden adresselinje. I appen kan du trække ned for at opdatere. Du logger ind én gang i appen, og så husker den dig i 7 dage.

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
3. **Rug-tjek** via RugCheck og Jupiter: mint- og freeze authority skal være slået fra, ingen "danger"-risici, begrænset ejerkoncentration, udvikleren må ikke sidde på store beholdninger. Kan RugCheck ikke svare, købes der ikke. Desuden (se **Værn mod tab** nedenfor): Jupiters organiske score skal være mindst 30, tokenet må ikke være markeret som mistænkeligt af Jupiter, udvikleren må ikke have lavet over 50 tokens, og RugCheck må højst finde 50 insider-wallets.
4. **Exit-tjek**: botten beder Jupiter om en købs- og en salgspris. Kan tokenet ikke sælges igen, eller koster frem og tilbage mere end 8 %, købes der ikke (honeypot- og tynd-pool-filter).
5. **Score**: en lille læringsmodel vægter signalerne (købspres, likviditet, organisk handel, sociale links, X-omtale, copy-signal, rug-risiko m.fl.).
6. **Claude** (hvis nøglen er sat) søger på X, nyheder og fora efter røde flag: falske konti, koordineret shill, kopier af kendte tokens, rug-historik. Claude kan kun sige nej. Claude kan aldrig tvinge et køb igennem eller ændre risikogrænserne.
7. **Risiko**: højst `MEME_POSITION_USD` pr. handel, aldrig mere end `MEME_BANKROLL_USD` i alt, højst `MEME_MAX_OPEN` åbne (standard 4) og `MEME_MAX_TRADES_PER_DAY` køb om dagen (standard 15). På papir kan antallet ændres under **Strategi → Almindelig → Antal handler**.

**Prøvekøb** (kun papir): et token, hvis score er højst 0,15 under grænsen, købes for en tredjedel af beløbet (mindst 1 USD). Det samme gælder tokens fra en strategi, som strategi-vagten har sat på pause. Så får botten flere rigtige handler at lære af uden at risikere det fulde beløb. Højst 8 om dagen (kan ændres, 0 slår dem fra) og 3 åbne ad gangen. De har egne grænser, så de tager ikke pladsen fra almindelige køb, de står med mærket "prøve", og de tæller ikke med i kravene før rigtige penge. Botten tjekker nu op til 8 tokens grundigt pr. runde og køber op til 3.

Hvert 15. sekund overvåges åbne positioner: stop-loss (-25 %), halvdelen sælges ved +60 %, trailing stop 25 % fra toppen, salg hvis likviditeten trækkes (rug-alarm), hvis intet sker, eller hvis den fulgte trader sælger. Tomme token-konti lukkes, så rent-depositummet kommer tilbage.

## Sniper: helt nye tokens

Sniperen er en ekstra strategi ved siden af den almindelige. Hvert 10. sekund henter den tokens, hvis første pool lige er oprettet (Jupiter), og nye profiler fra DexScreener. Et token købes kun, hvis det består alt dette:

1. Det er mellem `Vent mindst` (standard 5 min.) og `Maks. alder` (standard 30 min.) gammelt. Botten venter med vilje, fordi de første 5 minutters handel afslører de fleste rug pulls. Tokens, der er for unge, kommer på en venteliste og tjekkes, når de er gamle nok, også selv om de er gledet ud af listen over de nyeste.
2. Det er ikke på en bonding curve, har mindst 8.000 USD likviditet, ikke dumper lige nu og ikke har massivt salg.
3. Mint- og freeze authority er slået fra, udvikleren ejer højst 5 %, top-10 ejer højst 30 %, og der er mindst 25 holdere. Jupiter må ikke markere tokenet som mistænkeligt, og udvikleren må højst have lavet 20 tokens.
4. Det har X eller Telegram (kan slås fra). En hjemmeside alene tæller ikke: den siger intet om, om tokenet overlever.
5. RugCheck har ingen alvorlige advarsler, og Jupiter kan både købe og sælge tokenet igen uden stort tab (honeypot-tjek).
6. Risikogrænserne tillader det: standard 1,50 USD pr. snipe, højst 3 åbne snipes og 15 om dagen (kan ændres under Strategi → Almindelig → Antal handler), og bankroll-loftet gælder.

Salg sker ved **dit gevinstmål** (standard +100 %). Du kan vælge at sælge det hele ved målet eller kun en del og lade resten følge toppen. Der er også stop-loss (standard -35 %), rug-alarm hvis likviditeten trækkes, og en tidsgrænse (standard 60 min.).

Alt kan ændres på fanen **Sniper** i dashboardet: tænd og sluk, gevinstmål (med knapper til +25 % op til +500 %), andel der sælges, stop-loss, beløb pr. snipe, holdetid og filtrene. Siden viser også, hvor mange af dine snipes der skal ramme målet, før du går i nul. Læringen må kun stramme filtrene (fx efter rugs), aldrig ændre beløbet. Mål og stop flyttes kun af **Det bedste mål automatisk**, hvis det er slået til. Tal, du selv retter, rører læringen ikke i 7 dage.

Vær klar over:

- Sniping er den mest risikable måde at handle memecoins på. De fleste nye tokens rammer aldrig +100 %.
- Professionelle snipere køber i samme blok, som poolen oprettes, med private noder. Denne bot er bevidst langsommere, fordi den tjekker tokenet først. Den køber derfor sjældent i bunden.
- Claude bruges ikke til snipes som standard, fordi research tager op til et par minutter. Du kan slå det til, men så kommer botten senere ind.

## Top 10: følg dagens bedste traders

Under **Strategi → Top 10** kan du slå en funktion til, der køber lige efter de traders, der har tjent mest i dag, og sælger, når de sælger. Den er slået fra, til du tænder den.

1. **Listen**: indsæt dine egne traders under **Dine traders** på Top 10-siden (én pr. linje: adressen og gerne et navn, højst 25). Har du en egen liste, følger botten kun den. Ellers henter den med `SOLANATRACKER_API_KEY` (fra solanatracker.io) hver halve time de mest profitable Solana-traders i dag eller den seneste uge, og uden nøgle bruger den de wallets, du følger under Wallet.
2. **Køb**: hvert 8. sekund kigger botten efter nye handler fra dem. Køber en af dem et token for mindst 0,1 SOL, og er købet højst 2 minutter gammelt, tjekker botten tokenet (likviditet, RugCheck og at det kan sælges igen) og køber for dit beløb pr. handel. Er bottens pris over 15 % højere end den pris, traderen selv betalte (**Maks. pris over traderens købspris**), springes købet over: kursen er allerede løbet, og botten ville bare købe traderens gevinst. Claude springes over her, fordi det skal gå hurtigt.
3. **Salg**: sælger traderen tokenet, sælger botten hele positionen med det samme. Det gælder også, hvis traderen er røget af listen, eller funktionen er slået fra. Som sikkerhedsnet er der stop-loss (standard 30 %, kan slås fra med **Brug stop-loss** under Top 10), rug-alarm (likviditeten trækkes), en gevinstsikring hvis kursen er fordoblet og falder 35 % fra toppen, og en tidsgrænse (standard 12 timer), hvis traderen aldrig sælger.
4. **Grænser**: maks. åbne kopier (standard 3) og køb pr. dag (standard 10). Top 10 har ingen daglig tabsgrænse: den bliver ved med at kopiere, også når resten af botten har ramt dagens tabsgrænse. Vil du have, at den stopper dér, så slå **Stop ved dagens tabsgrænse** til under Top 10. Bankroll-loftet, den samlede tabsgrænse, pause og nødstop gælder altid. Traders, der giver tab over tid, slås automatisk fra.

Under Top 10 ser du dagens traders med deres resultat, hvad botten har tjent på at kopiere hver af dem, og de seneste signaler: hvad traderen gjorde, og hvad botten gjorde (købt, solgt eller sprunget over og hvorfor).

Vær ærlig over for dig selv: dagens vindere er ofte heldige eller bots, og du køber altid lidt efter dem til en højere kurs. Kør det på papir og se resultatet, før du bruger rigtige penge. Funktionen kigger ofte på Solana, så brug en egen RPC (fx en gratis Helius-nøgle i `SOLANA_RPC_URL`).

## Hvad tjener penge, og sniperens bedste mål

- **Hvad tjener penge?** På Hjem står hver strategi (Almindelig, Sniper, Top 10) med antal handler, gevinstprocent, resultat og de seneste handler. Tryk **Hold pause** for at stoppe en strategi, og **Genoptag** for at starte den igen.
- **Forventet pr. handel** står ved hver strategi på Hjem: hvad de seneste op til 40 tokens, strategien ville købe (købt eller kun stoppet af grænser som maks. åbne), gav i snit med dens egne salgsregler efter gebyrer. Det er et bedre mål for, om strategien har en fordel, end de få rigtige handler.
- **Strategi-vagt** (slået til som standard, kontakten står under Hvad tjener penge? på Hjem, eller `MEME_STRATEGY_GUARD`): taber en strategi på sine seneste 8–12 handler (profitfaktor under 0,8), holder den selv pause. Den holder også pause allerede efter 4 tabende handler, hvis mindst 30 tokens, den ville købe, i snit har tabt mindst 5 %. Pausen slutter ikke bare, når tiden er gået: botten starter kun strategien igen, når mindst 15 tokens fra pausen i snit har givet mindst +1 % efter gebyrer. Ellers forlænges pausen med et døgn ad gangen. Er der efter 4 døgn stadig for få tokens til at bedømme den, starter den igen. Din egen pause (Hold pause) slutter kun, når du trykker Genoptag. Ved opdateringen i november 2026 blev vagten slået til én gang, og Top 10 blev slået fra, fordi kopiering med forsinkelse sjældent giver overskud. Begge dele kan du ændre igen bagefter.
- **Det bedste mål automatisk** (slået til som standard, `MEME_AUTO_TARGETS`): gælder både sniperen og den almindelige strategi. Når skyggehandlerne har mindst 40 kursforløb, og et andet gevinstmål og stop klart har virket bedre (mindst 2 procentpoint pr. handel) og givet overskud, skifter strategien selv, højst én gang i døgnet pr. strategi. Den almindelige strategi bruger sine egne salgsregler (trailing og stale) i regnestykket. Ændringen står under Læring. Slå det fra under Strategi → Sniper.

## Værn mod tab (research nov. 2026)

Botten tabte penge, så vi gennemgik forskning og analyser af Solana-memecoins (blandt andet Bitquery, Pine Analytics og videnskabelige artikler om rug pulls, pump.fun og copy trading). Det førte til disse værn:

- **Organisk score** (`Min. organisk score`, standard 30): Jupiter måler, hvor meget af handlen der kommer fra rigtige wallets og ikke bots. Lav score betyder, at volumen er pumpet op af bots. Gælder den almindelige strategi. Sniperen har sin egen grænse (standard fra, fordi helt nye tokens sjældent har en score endnu).
- **Mistænkelig**: Jupiter markerer nogle tokens som mistænkelige. De købes aldrig.
- **Serie-udviklere** (`Maks. tokens lavet af udvikleren`, standard 50 og 20 for sniperen): en udvikler, der har lavet mange tokens, er typisk en "token-fabrik", der laver nye tokens for at sælge dem til køberne.
- **Insidere** (`Maks. insider-wallets`, standard 50): RugCheck finder wallets, der hænger sammen via overførsler. Mange insidere betyder, at nogle få sidder på meget af beholdningen og kan sælge på én gang.
- **Telegram og X** vægter mest i scoren. En hjemmeside alene tæller næsten ikke.
- **Sniperen venter 5 minutter** (før 1 minut). Gemte indstillinger fra før er sat op til 5 minutter én gang; du kan selv ændre det bagefter.
- **Top 10 jagter ikke kursen** (se ovenfor).

Papirkontoen er startet forfra med 50 USD én gang, så resultaterne viser de nye regler. Læringen (vægte, lektioner, skyggehandler og mål) er beholdt.

## Hvad virker? Fakta pr. token og den automatiske strammer

For hvert token, botten følger (købt eller afvist), gemmes de rå tal: likviditet, markedsværdi, alder, stigning, andel køb, organisk score, holdere, top-10, insidere, udviklerens antal tokens, Telegram/X/hjemmeside, boost og tidspunkt. Når skyggehandlen er færdig (6 timer), ved botten, hvad tokenet ville have givet med dens egne salgsregler efter gebyrer.

- **Læring → Hvad virker?** viser for hver strategi, hvordan tokens med lav, mellem og høj værdi af hver egenskab klarede sig. Egenskaberne med størst forskel står øverst. Det kræver mindst 30 færdige skyggehandler pr. strategi.
- **Den automatiske strammer** (følger **Det bedste mål automatisk**, `MEME_AUTO_TARGETS`): viser data, at en strammere grænse ville have sorteret mindst 25 tokens fra, som i snit tabte mindst 5 %, mens resten klarede sig tydeligt bedre, strammes grænsen. Kun strammere, aldrig løsere, højst ét filter pr. strategi i døgnet, og altid inden for de faste grænser. Hver stramning står som en lektion med tallene bag. Den almindelige strategi kan strammes på bottens score (min. score for køb, regnet med de vægte botten bruger nu), organisk score, likviditet, markedsværdi, alder, stigning sidste time, top-10, insidere og rug-risiko. Sniperen på ventetid, likviditet, holdere, top-10 og organisk score.
- **Automatiske ændringer** (Læring) samler alt, læringen selv har flyttet: mål og stop, strammede filtre og justeringer efter rugs og stop-loss, med tallene før og efter og grunden. **Fortryd** sætter tallene tilbage, og læringen rører dem så ikke i 7 dage. Er et tal ændret igen siden, står der "ændret siden", og du retter det direkte under Strategi.
- **Hent data (JSON)** på samme kort giver én fil med alle handler, skyggehandler med fakta, indstillinger, modelvægte og lektioner. Der er ingen nøgler eller adgangskoder i den. Send den videre, hvis du vil have en grundig analyse af, hvor botten taber.

**Flere coins igennem (nov. 2026).** Markedsfiltrene er løsnet: likviditet 15.000 $ (før 25.000), volumen 5.000 $ den seneste time, 40 handler, alder 15 min., markedsværdi 40.000 $, stigning op til 300 % på en time, 45 % køb, score 0,50 og organisk score 20. Sniperen: 6.000 $ likviditet, 15 holdere og top-10 op til 35 %. Rug-, mint/freeze-, honeypot-, insider- og mistænkelig-tjekkene er uændrede. En bot, der allerede kører, får de løsere tal én gang, men tal, du selv har sat, røres ikke, og ændringen kan fortrydes under Læring → Automatiske ændringer.

## Sådan lærer den

- Efter hver lukket handel flyttes vægtene på de signaler, der bar handlen (op ved gevinst, ned ved tab). Dashboardet viser vægtene og en lektion pr. handel.
- Hver 5. handel justeres filtrene ud fra mønstre: mange rugs giver strengere rug-krav, mange stop-loss giver krav om mere likviditet og mindre jagt på toppe, og så videre.
- En gang i døgnet gennemgår Claude de seneste handler og skriver lektioner, som bruges i næste research. Claude må foreslå ændringer af filtrene, men kun inden for faste grænser.
- Kilder og fulgte wallets, der taber over tid, bliver automatisk slået fra.
- Positionsstørrelse, bankroll og tabsgrænser kan hverken læringen eller Claude ændre.

## Tabsgrænser

Botten bruger aldrig mere end din bankroll (`MEME_BANKROLL_USD`). Det er det mest, den kan tabe, og det loft kan ikke slås fra.

Den daglige tabsgrænse er **slået fra som standard**, og den samlede tabsgrænse er **slået til**. Begge kan ændres under **Mere → Indstillinger → Tabsgrænser** (eller med `MEME_DAILY_LOSS_LIMIT` og `MEME_DRAWDOWN_LIMIT`):

- **Dagligt tabsstop**: når dagens tab når `MEME_DAILY_LOSS_PCT` af bankroll (standard 25 %), køber botten ikke mere resten af døgnet (UTC). Åbne positioner beholder deres stop.
- **Samlet tabsstop**: når det samlede tab når `MEME_MAX_DRAWDOWN_PCT` (standard 50 %), sælges alt, og botten står stille, indtil du trykker **Genoptag**.
- **Stop efter tab i dag** (`MEME_MAX_LOSSES_PER_DAY`, standard 3): når tre handler er lukket med tab i dag (UTC), køber botten ikke flere nye tokens den dag. Samme regel som bordet. **Genoptag** ændrer det ikke, fordi stoppet er til for at beskytte dig mod en dårlig dag.
- **Markedsfilter på SOL** (`MEME_SOL_FILTER_PCT`, standard 5): er SOL ned mere end 5 % den seneste time, køber botten ingen nye tokens, mens markedet falder. Salg fortsætter, og åbne positioner beholder deres stop. Botten skriver kun en besked om stoppet, højst hvert halve time.

**Nødstop** sælger alt med det samme og virker altid. Sniperen og den almindelige strategi har stadig deres egne stop-loss pr. position.

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

- **Claude-research**: `ANTHROPIC_API_KEY`. Intet dagligt loft som standard. Slå **Dagligt AI-loft** til i dashboardet, eller sæt `MEME_AI_BUDGET_LIMIT=true`, så stopper Claude for i dag ved `MEME_AI_DAILY_BUDGET_USD` (standard 0,50 USD), og botten kører videre på regler. `MEME_AI_DAILY_BUDGET_USD=0` slår Claude helt fra. Standardmodellen er `claude-opus-5-5` med indsats `medium` og websøgning. Vil du bruge en billigere model, kan du sætte `MEME_ANTHROPIC_MODEL`. Ældre modeller skal muligvis have `MEME_ANTHROPIC_WEB_SEARCH_TOOL=web_search_20250305`.
- **X API**: `X_BEARER_TOKEN` fra developer.x.com (betalt). Botten søger efter kontraktadressen og tickeren og måler, hvor mange forskellige konti der skriver om tokenet. `X_DAILY_REQUESTS` begrænser forbruget.
- **Copy-trading**: `MEME_COPY_WALLETS=adresse1,adresse2`. Find kandidater på fx GMGN, Birdeye eller Kolscan, og tjek selv deres historik. Første gang botten ser en wallet, husker den kun, hvor den er nået til; gamle handler kopieres aldrig. Et køb fra en fulgt wallet skal stadig bestå alle tjek ovenfor.
- **Telegram**: sæt `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID`. Du får besked ved køb, salg, tabsgrænser og fejl.

## Kør 24/7

Lokalt: `python run.py` og åbn [http://127.0.0.1:8741/meme](http://127.0.0.1:8741/meme). Computeren skal så være tændt hele tiden.

På Railway: følg afsnittet *Udgiv på Railway* ovenfor. Memebotten gemmer sin database i `/data/meme.db` på samme volume. Tilføj de `MEME_*`-variabler, du vil ændre. Alle har fornuftige standarder, så botten kører på papir uden ekstra opsætning.

Datakilder: DexScreener, RugCheck og Jupiter (`lite-api.jup.ag`) virker uden nøgle. Jupiter tillader dog kun ca. 30 kald i minuttet uden nøgle, og hos Railway deles grænsen med andre servere på samme IP. Ser du "rate limit (429)", så opret en **gratis** nøgle på portal.jup.ag og sæt `JUPITER_API_KEY` i Railway. Så bruges `api.jup.ag` automatisk med 60 kald i minuttet for din egen konto.

Botten holder selv regnskab med kaldene (25 i minuttet uden nøgle, 55 med) og prioriterer: salg og køb går altid forrest, quotes til nye kandidater må bruge 80 % af kvoten, og lister over nye eller trending tokens 60 %. Trending-lister gemmes i 5 minutter. Svarer Jupiter alligevel 429, holder botten pause (15 s, så 30 s og op til 2 minutter) og bruger imens den senest hentede liste. Et token, der kun blev stoppet af kvoten, afvises ikke, men tjekkes igen i næste runde. Under Indstillinger kan du se, hvor mange kald der er brugt det seneste minut.

RugCheck og DexScreener har samme beskyttelse:

- **RugCheck** oplyser ingen fast grænse, så botten bruger højst 30 kald i minuttet (`RUGCHECK_RPM`). Svarer RugCheck 429, holder botten pause og prøver ikke den lille opsummering med det samme, da den tæller i samme kvote. Der købes aldrig uden et frisk rug-tjek. Kandidater, der ikke nåede at blive tjekket, afvises ikke, men tjekkes igen i næste runde, også i sniperen.
- **DexScreener** tæller lister (nye profiler og boosts, 60 i minuttet) og kurser (300 i minuttet) hver for sig. Botten holder sig til 50 og 240. Listerne gemmes i 10–30 sekunder, så sniperen og scanningen deler ét kald. Kurser til dine åbne positioner går altid forrest. Siger DexScreener stop, bruger overvågningen Jupiters kurser imens.

---

# Handelsbordet (seks bots, én gruppechat)

Bygget efter guiden "How to build a trading desk in one group chat", men på Claude og Alpaca i stedet for Grok Bot og TradingView. Bordet kører i samme proces som memebotten og har sin egen side på `/desk` (skifteren øverst på siden). Det handler **kun på papirpenge**: amerikanske aktier hos Alpaca og danske aktier på sin egen papirkonto (se *Københavns børs*). Der findes ingen kodevej til rigtige penge for bordet.

## De seks bots

| Bot | Job | Kører på |
|---|---|---|
| **SCOUT** | Markerer en ticker på watchlisten, der flytter sig mindst 1 % på en time eller handler mindst 2 gange normal volumen. Højst 10 markeringer om dagen på kursbevægelse; markeringer kun på volumen har deres egen kvote, så de ikke bruger pladserne op i første time. En markering, CHART siger "Ingen setup" til (fx en aktie, der falder), tæller ikke i de 10, og aktien får én chance til: efter mindst en halv time markeres den igen, hvis kursen nu er på vej op. Før åbning finder den gaps (se nedenfor). | fast kode |
| **NEWS** | Finder grunden med websøgning og Alpaca-nyheder og skriver den på to linjer med link. Uden kilde: "Ingen klar grund". | Claude |
| **CHART** | Sætter indgang, mål og stop. Stoppet er højst 2 % under indgangen, og målet er mindst 2 gange så langt væk. Et lille dyk de sidste fem minutter (højst 0,2 %) er et tilbagefald, ikke en vending. Ellers "Ingen setup". Kun køb, ingen short. | fast kode |
| **SKEPTIC** | Giver de tre stærkeste grunde til, at handlen fejler, og afgør BUY, WAIT eller SKIP. Uden klar grund fra NEWS er svaret altid SKIP. Det håndhæves i koden, også hvis Claude siger BUY. | Claude |
| **SIZER** | Højst 5 % af papirkontoen pr. handel og højst 3 åbne eller ventende. Er en grænse ramt, er svaret 0. | fast kode |
| **EXECUTOR** | Beder om din godkendelse, lægger en bracket-ordre med stop og mål hos Alpaca, lukker alt kl. 15:50 ET og skriver dagsloggen kl. 16:15 ET med handler og saldo. | fast kode |

SCOUT, CHART og SIZER er bevidst almindelig kode. Tal fra en sprogmodel kan være opdigtede, så kurser, stop og beløb bliver regnet ud fra rigtige kursdata. Claude bruges til det, Claude er god til: at finde og vurdere nyheder og argumentere imod en handel.

## Autopilot eller godkendelser

**Autopilot er slået til som standard** (`DESK_AUTO_APPROVE=true`, eller fluebenet under **Bots**): når SKEPTIC siger BUY og SIZER har sat et beløb, køber EXECUTOR selv på papirkontoen uden at spørge. Samme tjek som ved en manuel godkendelse gælder stadig (kursen må ikke være løbet, lofterne skal holde), og **Stop nu** sætter alt på pause. SCOUT kigger hvert minut (`DESK_SCAN_MINUTES=1`).

**Prøvekøb er også slået til som standard** (`DESK_TRIAL_BUYS=true`, eller **Prøvekøb** under **Bots → Indstillinger**). Har en aktie flyttet sig, og har CHART en ren opstilling, køber bordet alligevel, men kun halv størrelse (2,5 % af kontoen i stedet for 5 %), når SKEPTIC siger WAIT, eller når NEWS ikke fandt en grund (SKIP). Et SKIP fra Claude med en nyhed i hånden respekteres. Det giver flere handler at se på papir, og bordet lærer af rigtige resultater i stedet for kun skyggeregnskab. Taber køb uden en grund fra NEWS mindst 3 gange med højst en tredjedel vindere, slår den lærte regel "Køb uden en grund fra NEWS" til, og så stopper bordet dem selv. Prøvekøb sker aldrig på en markering kun på volumen og aldrig, når en lært regel siger nej. Lofterne for åbne handler og dagens tab gælder som altid. Prøvekøb står som "prøvekøb" ved handlen og i Gruppen.

**Tre stopregler, der gør bordet mere forsigtigt, når det går dårligt.** Alle tre kan slås fra med 0.

- **Markedsfilter** (`DESK_MARKET_FILTER_PCT`, standard 1). Er S&P 500 eller C25 ned mere end 1 % siden åbningen, køber bordet ingen nye aktier, så længe markedet falder. Markeringerne vises stadig, og de tæller ikke som forslag, så de kan komme igen, når markedet vender.
- **Tidsstop** (`DESK_TIME_STOP_MIN`, standard 90). En handel, der efter 90 minutter stadig ligger under købskursen, lukkes til markedskurs. Pladsen bliver fri til en ny aktie, og kapitalen sidder ikke fast i en handel, der ikke går nogen steder. Handler over købskursen får lov at køre til stop eller mål.
- **Stop efter tab** (`DESK_MAX_LOSSES_PER_DAY`, standard 2). Når to handler er lukket med tab i dag, køber bordet ikke mere den dag. Det holder ikke en dårlig dag i gang.

Slår du autopiloten fra, ender hver BUY som et kort under **Godkend** (og i chatten) med hele kæden: markering, nyhed med link, de tre modargumenter og beløbet. Du har 10 minutter. Når du trykker **Godkend køb**, tjekker EXECUTOR igen, at kursen ikke er løbet mere end 0,5 % over indgangen og stadig ligger mellem stop og mål, og at SIZER's grænser holder. Ellers lægges ordren ikke. Er Telegram sat op, får du også en besked på telefonen.

## Tal med bordet

Skriv `@SKEPTIC hvorfor WAIT til NVDA?` (eller tryk på en af knapperne over skrivefeltet). Den nævnte bot svarer i sin rolle med bordets tal. Bots kan ikke lægge ordrer eller ændre grænser fra chatten. Skriv **Stop nu** for at sætte alt på pause (ventende godkendelser annulleres, åbne positioner beholder stop og mål), og **Start igen** for at fortsætte.

**Flere aktier igennem (nov. 2026).** SCOUT markerer nu fra 1 % på en time (før 1,5 %), gaps fra 2 % (før 3 %), og op til 10 markeringer om dagen (før 8), i både New York og København. Har du gemt de gamle standarder, giver de plads én gang; tal, du selv har valgt, bliver stående. CHART, SKEPTIC, SIZER og stoppene er uændrede, så hver aktie stadig skal have en ren opstilling.

## Bordet lærer af sine fejl

Bordet starter med det samme, når nøglerne er sat. Der er ingen prøveperiode, men det lærer af hver handel:

- **Hver markering får et udfald.** Købte handler får det rigtige resultat. Dem, bordet sagde nej til, får et skyggeresultat efter lukketid: ville de have ramt stop eller mål først?
- **Lærte regler.** Udfaldene sorteres i mønstre, fx køb mere end 1,5 % over VWAP, køb efter kl. 14 ET, aktier der allerede er steget 5 %, eller køb hvor grunden kun var en overskrift. Har et mønster tabt mindst 3 gange med højst en tredjedel vindere, slår reglen til, og nye køb af den slags får WAIT. Reglen slår selv fra igen, når mønstret begynder at vinde.
- **Lektioner.** Efter lukketid skriver SKEPTIC i gruppen, hvad bordet lærte i dag: tabte handler, nye regler og handler, den afviste, men som ville have vundet. SKEPTIC får lektionerne og de lærte regler med, når Claude vurderer næste handel.

Fanen **Læring** viser reglerne med tal, lektionerne og bordets resultat mod at holde S&P 500 (SPY) siden første dag. SKEPTIC's regnskab står under **Bots**.

## Siden

Forsiden **Overblik** viser papirkontoen, en nedtælling til børsen (New York eller København) åbner eller lukker, nøgletal, en radar over watchlisten, systemstatus, seneste forslag trin for trin, åbne positioner og watchlisten med kurver.

- **Nedtællingen virker altid.** Bordet har sin egen kalender over børsens åbningstider med helligdage og halve dage (regnet ud fra NYSE's regler). Svarer Alpaca, bruges Alpacas ur i stedet, fordi det også kender ekstraordinære lukninger.
- **Kurser hele døgnet.** Når børsen er lukket, henter bordet sidste handelsdags kurser hos Alpaca hvert 10. minut, så radar, kursbånd og watchlist altid har noget at vise.
- Tryk på en prik i radaren eller en række i watchlisten for at se aktien og spørge SKEPTIC eller NEWS. ⌘K/Ctrl+K åbner kommandolinjen, og tasterne 1–5 skifter fane.
- **Hvem handler lige nu.** I skifteren øverst får **USA** og **Danmark** en grøn prik, når den børs handler, og en gul før åbning. Hold musen over for at se åbne handler og dagens resultat.
- **Godmorgen i Gruppen.** Når en børs åbner, skriver bordet én linje: hvornår SCOUT kigger, hvor mange aktier og saldoen. Står bordet på pause, siger beskeden det.
- **Mangler kurserne, står grunden der.** Kan kurserne ikke hentes (fx hvis Yahoo eller Alpaca ikke svarer), viser watchlisten og statuslinjen hvorfor, og bordet prøver igen hvert minut. Er Yahoo nede, prøves Yahoos anden adresse. Beder Yahoo bordet vente (fejl 429), holder bordet pause med de danske kurser i 2, 4, 8 … op til 30 minutter (eller så længe Yahoo selv beder om) i stedet for at spørge igen hvert minut.
- På telefonen står kontoen øverst, så børsen med nedtællingen og åbne handler, og derefter resten.
- Bottenes beskeder skriver tal på dansk: `+3,5 %`, `103,44`, `4.965 kr.`.

## Opsætning

1. Opret en papirkonto på [app.alpaca.markets](https://app.alpaca.markets) (kontonavnet øverst til venstre → **Open New Paper Account** → **API Keys → Generate New Keys**) og sæt `DESK_ALPACA_API_KEY` og `DESK_ALPACA_SECRET_KEY` i Railway. Står `ALPACA_API_KEY` og `ALPACA_SECRET_KEY` der allerede, bruges de.
2. `ANTHROPIC_API_KEY` slår Claude til for NEWS og SKEPTIC. Bordet bruger `claude-haiku-5-5` som standard (`DESK_ANTHROPIC_MODEL` skifter den). Haiku koster ca. en fyrretyvendedel af Opus, men den vurderer også SKEPTIC's BUY og WAIT, så følg **Nej med rette** under Læring. Kør **Test Claude** først, og se at svaret er grønt. Der er intet dagligt loft som standard; sæt `DESK_AI_DAILY_BUDGET_USD` (fx 2) for at få et. Knappen **Test Claude** under Bots viser, om Claude svarer, og ellers præcis hvorfor (forkert nøgle, ingen kredit, ukendt model). Et abonnement på Claude (Pro eller Max) dækker ikke API'et: API-kredit købes for sig under Billing i Claude Console. Fejler Claude på en måde, der ikke går over af sig selv (ingen kredit, forkert nøgle eller model), holder bordet 15 minutters pause fra Claude, bruger faste regler imens og prøver så igen; **Test Claude** prøver altid med det samme. Siden viser det som en gul besked, for bordet handler videre. Uden Claude tager faste regler over, og de er strengere: kun en Alpaca-overskrift fra de sidste 18 timer (fra i går aftes og frem) tæller som grund, og SKEPTIC siger WAIT, hvis kursen ligger mere end 2 % over VWAP.
3. Alle `DESK_*`-variabler står i `.env.example`. På Railway ligger databasen som `/data/desk.db`.

SCOUT kigger hvert minut fra 9:45 til 15:30 ET (15:45–21:30 dansk tid). Intet her er finansiel rådgivning: bottene foreslår, du beslutter.

### Før åbning og gaps

Fra 4:00 til 9:30 ET (normalt 10:00–15:30 dansk tid) kigger SCOUT hvert 5. minut efter aktier på watchlisten, der er steget mindst 2 % siden gårsdagens lukkekurs (`DESK_GAP_PCT`). Kun handler fra samme dag tæller. NEWS finder grunden med det samme, så den står i Gruppen og på kortet **Før åbning**, før børsen åbner. Der bliver ikke købt noget før åbning.

Fra 9:45 til 11:00 ET markerer SCOUT et gap, der stadig er oppe mindst 2 % og ligger over VWAP, selv om det ikke har flyttet sig 2 % på den første time. Har NEWS allerede fundet grunden før åbning, bliver den genbrugt. Ellers slår NEWS op som normalt. Herefter går gappet gennem CHART, SKEPTIC og SIZER som alt andet. Taber gap-køb flere gange, lærer bordet det ("Gaps købt ved åbningen") og siger WAIT til dem.

Kurserne før åbning kommer fra IEX og kan være tynde. Slå det fra under **Bots → Indstillinger → Før åbning** eller med `DESK_PREMARKET=false`.

Hver markering koster typisk to Claude-kald (NEWS og SKEPTIC), og hvert gap før åbning ét. Vil du have et loft over udgiften, så sæt `DESK_AI_DAILY_BUDGET_USD`.

## Københavns børs

> **Slået fra lige nu.** Københavns børs er skjult, indtil kurserne fra Yahoo virker stabilt. Sæt `DESK_DK_ENABLED=true` i Railway for at slå den til igen.

Bordet handler også danske aktier på Nasdaq Copenhagen, side om side med New York. Skift mellem **USA** og **Danmark** øverst på siden. Siden husker dit valg. Hver børs har sin egen chat, sine egne handler, sin egen læring og sin egen database (`desk_dk.db`), men de samme seks bots og regler.

Alpaca handler kun amerikanske aktier, så København kører på **bordets egen papirkonto** i kroner:

- Kontoen starter med 100.000 kr. (`DESK_DK_START_CASH`).
- Kurserne kommer fra Yahoo Finance. Det er gratis og kræver ingen nøgle, men kurserne er uofficielle og kan være lidt forsinkede.
- Et køb fyldes til seneste kurs. Kurtagen (0,1 % pr. handel, `DESK_DK_FEE_PCT`) er trukket fra i købs- og salgskursen, så resultatet er efter kurtage.
- Bordet tjekker selv stop og mål mod hvert nyt minut, præcis som en bracket-ordre. Stop vinder, hvis begge rammes i samme minut.

Tiderne følger børsen:

| | New York | København |
|---|---|---|
| Åben | 9:30–16:00 ET (15:30–22:00 dansk tid) | 9:00–17:00 |
| SCOUT kigger | hvert minut, 9:45–15:30 ET | hvert 5. minut, 9:15–16:30 |
| Gaps ved åbningen | til 11:00 ET | til 10:30 |
| Alt lukkes | 15:50 ET | 16:50 |
| Dagslog | 16:15 ET | 17:15 |

Halve dage i New York flytter tiderne med.

København er lukket på danske helligdage: nytår, skærtorsdag, langfredag, 2. påskedag, Kristi himmelfart og dagen efter, 2. pinsedag, grundlovsdag, juleaften, juledag, 2. juledag og nytårsaften.

Der er ingen handel før åbning i København, så kortet **Før åbning** findes kun for New York. Gaps ved åbningen gælder begge steder.

Yahoo er gratis, men svarer 429 ("for mange forespørgsler"), hvis bordet spørger for tit. Derfor kigger SCOUT i København hvert 5. minut (`DESK_DK_SCAN_MINUTES`). Det er nok til bevægelser på en time. Mellem scanningerne spørger bordet ikke Yahoo, åbne handler tjekkes hvert 2. minut mod alle minutter siden sidst (så intet stop overses), og når børsen er lukket, hentes kurserne hvert 30. minut. SCOUT markerer i København fra 1 % på en time (`DESK_DK_MOVE_PCT`), fordi store danske aktier flytter sig mindre end de livlige amerikanske. Watchlisten er 16 likvide C25-aktier: NOVO-B, DSV, DANSKE, ORSTED, VWS, GMAB, PNDORA, CARL-B, COLO-B, NSIS-B, DEMANT, GN, NKT, BAVA, ZEAL og AMBU-B. Skriv tickers, som Yahoo skriver dem, uden `.CO` (fx `NOVO-B`). Bordet måles mod C25-indekset.

København er **slået fra som standard**, indtil kurserne fra Yahoo virker stabilt. `DESK_DK_ENABLED=true` slår den til igen, og så kommer **Danmark** frem i menuen.
