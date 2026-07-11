# Notebooks V2

Les notebooks `examples/00` à `examples/06` lisent les sources V2 :
`order_filled_v2`, `trades`, `markets_current`, `market_assets` et
`market_outcomes`.

```powershell
uv run python scripts/smoke_all_notebooks.py
```

Pour un lac complet, définissez `POLY_DATA_ROOT` et
`POLY_NOTEBOOK_MODE=full`. Les résultats officiels sont la seule source
d’étiquettes pour les PnL réalisés et les évaluations supervisées.
