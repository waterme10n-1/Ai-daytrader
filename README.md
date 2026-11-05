# Memebot

En 24/7-bot til memecoins på Solana med rug-tjek (RugCheck, Jupiter), exit-rute-tjek, en sniper til helt nye tokens, Top 10 (køb lige efter dagens mest profitable traders og sælg, når de sælger), valgfri X- og Claude-research, copy-trading af udvalgte wallets og en model, der lærer af hver lukket handel. Den starter på papirpenge med rigtige kurser og kan først handle med rigtige penge fra en separat Phantom-konto, når papirresultatet er positivt. Dansk brugerflade, bygget til mobil og kan lægges på hjemmeskærmen som app.

```bash
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Åbn http://127.0.0.1:8741. Opsætning, Railway og alle indstillinger står i [LÆS_MIG.md](LÆS_MIG.md).

```bash
pytest
```

**Handelsbordet** (`/desk`): seks bots i én gruppechat til amerikanske aktier på Alpacas papirkonto. SCOUT finder bevægelsen, NEWS grunden, CHART indgang, mål og stop, SKEPTIC argumenterer imod, SIZER sætter beløbet, og EXECUTOR køber selv (autopilot) eller spørger dig. Se afsnittet *Handelsbordet* i [LÆS_MIG.md](LÆS_MIG.md).
