---
name: ui-check
description: Tag skærmbilleder af Memebot (/meme) og Handelsbordet (/desk, USA og Danmark) i telefon- og computerstørrelse med Playwright, og find JS-fejl, HTTP-fejl, vandret scroll og felter, der hænger på indlæsning. Brug efter enhver ændring i trader/static/*.html eller i data, siderne viser, og når brugeren beder om at se siden.
---

# UI-tjek

Kør scriptet fra repoets rod:

```bash
node .claude/skills/ui-check/ui_check.js --out <mappe>
```

- `--out <mappe>`: hvor billederne lægges. Brug scratchpad-mappen, hvis sessionen har en. Standard er `<tmp>/ui-check`.
- `--alle-looks`: også Memebots fem udseender (neon, volt, is, rav, papir) og bordets lyse tema. Tager ca. 75 s i stedet for 25 s.
- `--hele-siden`: hele siden i stedet for kun det, der ses på skærmen.

Scriptet starter selv appen på en ledig port med en tom database, uden nøgler og uden login, så det aldrig rører rigtige konti eller bruger Claude-kredit. Det stopper serveren igen bagefter.

## Bagefter

1. Exit 1 betyder fejl. Hver fejl står under siden, den hører til. Ret dem.
2. Se altid på billederne med Read-værktøjet, også når alt er OK. Scriptet fanger ikke grimme layouts, forkerte tekster eller tal uden komma.
3. Uden netværk eller nøgler viser siderne forbindelsesfejl som "Alpaca er ikke forbundet". Det er forventet her.
4. Vis brugeren de vigtigste billeder, når ændringen er visuel.

Kræver Playwright (`require('playwright')`). Det er forudinstalleret i Claude Code-cloud-sessioner.
