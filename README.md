# AI Daytrader

Lokal daytrader til likvide amerikanske aktier og ETF'er. Dansk brugerflade, papirpenge først, og en simulator der kører en hel dag gennem den samme motor.

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
