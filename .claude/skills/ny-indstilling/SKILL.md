---
name: ny-indstilling
description: Tjekliste til at tilføje eller ændre en indstilling (miljøvariabel, grænse eller felt under Indstillinger) i Handelsbordet eller Memebot, så den virker alle steder - config, motor, API, side, .env.example, LÆS_MIG.md og test. Brug når brugeren vil have noget gjort justerbart, ændre en standardværdi eller tilføje en ny DESK_- eller MEME_-variabel.
---

# Ny indstilling

En indstilling skal hænge sammen på op til otte steder. Den nemmeste vej er at finde en eksisterende indstilling af samme slags og gå samme vej:

```bash
grep -rn "gap_pct\|DESK_GAP_PCT" trader tests .env.example LÆS_MIG.md
```

## Handelsbordet (`trader/desk/`), fx `gap_pct`

1. `trader/desk/config.py`: felt i `DeskSettings` med standardværdi og en dansk kommentar, og i `from_env` læst fra `DESK_…` med grænser (`min(…, max(…, _float(os.getenv(…), standard)))`). Gælder det også København, så tjek `from_env_dk`.
2. `trader/desk/engine.py`:
   - `cfg`: navnet i den rigtige tuple (float, int eller bool), så værdier gemt fra siden slår igennem.
   - `update_params`: grænser i `limits` (eller egen håndtering for bool), og en dansk linje i `changes`.
   - `status`: værdien med under `params`, så siden kan vise den.
   - Ændres en standardværdi: `_retire_old_defaults`, så gemte gamle standarder giver plads én gang.
3. `trader/static/desk.html`: et `<input name="…">` under Bots → Indstillinger, og navnet i både listen, der udfylder formularen, og listen, der gemmer den (søg efter `"gap_pct"` i filen).
4. `.env.example`: variablen med standardværdi og en kort dansk forklaring.
5. `LÆS_MIG.md`: hvad den gør, og hvor den slås til eller fra.
6. `tests/test_desk.py`: test af standardværdien og at `update_params` afviser værdier uden for grænserne.

## Memebot (`trader/meme/`)

Memebot har ikke én fælles indstillingsrute. Hver gruppe har sin egen (`/api/meme/limits` → `update_loss_limits`, `/api/meme/sniper`, `/api/meme/strategies`, `/api/meme/toptraders` …). Find den gruppe, indstillingen hører til, og følg en nabo-indstilling gennem `trader/meme/config.py` (felt, `from_env` og `public_meme_settings`), motoren, `trader/app.py` og `trader/static/meme.html`. Derefter `.env.example`, `LÆS_MIG.md` og en test.

## Regler

- Grænser håndhæves i Python, ikke kun på siden.
- En indstilling må aldrig kunne løsne papir-først-kravene eller de hårde lofter (`HARD_MAX_*`, `UNLOCK_MIN_*`).
- Tekst på siden er dansk, og tal skrives med komma.
- Kør `pytest` og `/ui-check` til sidst.
