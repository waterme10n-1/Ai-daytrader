# AI Daytrader

Lokal daytrader til likvide amerikanske aktier og ETF'er. Dansk brugerflade, papirpenge først, og en simulator der kører en hel dag gennem den samme motor.

**Memebot** (`/meme`): en 24/7-bot til Solana-memecoins med rug-tjek (RugCheck, Jupiter), exit-rute-tjek, valgfri X- og Claude-research, copy-trading af udvalgte wallets og en model, der lærer af hver lukket handel. Den starter på papirpenge med rigtige kurser og kan først handle med rigtige penge fra en separat Phantom-konto, når papirresultatet er positivt. Se afsnittet *Memebot* i [LÆS_MIG.md](LÆS_MIG.md).

**Handelsbordet** (`/desk`): seks bots i én gruppechat. SCOUT finder bevægelsen, NEWS finder grunden, CHART sætter indgang, mål og stop, SKEPTIC argumenterer imod, SIZER sætter beløbet, og EXECUTOR spørger dig, før en papirordre lægges hos Alpaca. Se afsnittet *Handelsbordet* i [LÆS_MIG.md](LÆS_MIG.md).

Fuld opsætning af Alpaca-papirnøgler og Anthropic står i [LÆS_MIG.md](LÆS_MIG.md).

```bash
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Åbn http://127.0.0.1:8741. Uden nøgler bruges **Simulér én dag** (syntetiske kurser, kun regler). Syntetiske resultater siger intet om rigtig performance. Udgivelse på Railway med login står i [LÆS_MIG.md](LÆS_MIG.md).

```bash
pytest
```
