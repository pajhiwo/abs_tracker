ABS Tracker

Auto-Brewery Syndrome — Diet & BAC Correlation Analysis

## Setup

```bash
cd abs_tracker
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## CLI Usage

```bash
python abs_tracker.py data/jo_log.xlsx              # default 3h look-back
python abs_tracker.py data/jo_log.xlsx --hours 5    # custom look-back window
python abs_tracker.py --test                        # run edge-case assertions
```

Output CSVs are saved to `output/`.

## Web UI

Start:
```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 in your browser, then upload your Excel file.

Stop: press `Ctrl+C` in the terminal, or from another terminal:
```bash
pkill -f "uvicorn app.main:app"
```

## How it works — data flow & correlation logic

### Excel layout (18 columns, A–R)

| Column | Index | Content |
|--------|-------|---------|
| A | 0 | **Date** — one date row starts a new day |
| B | 1 | **Meal** — "Breakfast", "Lunch", "Dinner", "Snack" |
| C | 2 | **Meal Time** — time the meal was eaten (e.g. 08:30) |
| D | 3 | **Product** — ingredient name |
| E–M | 4–12 | Nutrition data (grams, calories, macros) |
| N | 13 | **BAC Time** — time the BAC test was taken |
| O | 14 | **BAC Value** — promille reading (e.g. 0.12) |
| P | 15 | **Episode** — "yes" if flagged |
| Q | 16 | **Medication** — e.g. "Activated charcoal start" |
| R | 17 | **Comment** |

### Row hierarchy

The Excel is not a flat table — it's **hierarchical**:

```
Row: 2025-03-01              ← Date row (sets current_date)
Row:   Breakfast  08:30      ← Meal row (sets current_meal + meal_time)
Row:     Oatmeal             ← Ingredient row (inherits date + meal context)
Row:     Banana              ← Ingredient row
Row:              09:15 0.08 ← BAC reading row (own timestamp in col N+O)
Row:   Lunch      12:00     ← New meal
Row:     Chicken             ← Ingredient
Row:     Rice                ← Ingredient
Row:              13:30 0.15 ← BAC reading row
```

The parser walks row by row, maintaining state:
- **Date row** → updates `current_date`, resets meal context
- **Meal row** → updates `current_meal` + `current_meal_time` (combined into `meal_datetime`)
- **Ingredient row** → appended to `meals_df` with the current date+meal+time context
- **BAC row** → appended to `bac_df` with its own `bac_datetime` (date + BAC time)

**Key point**: Ingredients get their timestamp from the **meal header row**, not their own row. All ingredients under "Lunch 12:00" share `meal_datetime = 2025-03-01 12:00`.

### Look-back correlation (`map_lookback`)

For each BAC reading, a time window is applied to find which meals were eaten recently:

```
BAC reading at 2025-03-01 13:30 (0.15‰), with hours=3:

Window = [13:30 − 3h, 13:30] = [10:30, 13:30]

  Breakfast 08:30 → 08:30 < 10:30 → ❌ outside window
  Lunch     12:00 → 10:30 ≤ 12:00 ≤ 13:30 → ✅ inside window
    → Chicken paired with this BAC (hours_before = 1.5)
    → Rice    paired with this BAC (hours_before = 1.5)
```

The result is a `lookback_df` with one row per **(BAC reading, ingredient)** pair.

**Fallback**: If a meal has no time (just a date), date-level matching is used instead, flagged as `approximate=True`.

### Lift score calculation (`compute_lift_scores`)

For each ingredient, all BAC readings are split into two groups:

```
Example: Chicken appears in lookback for BAC readings #5, #8, #12

  "present" = readings where chicken was eaten within the window
      → mean BAC = 0.18‰

  "absent"  = all other readings
      → mean BAC = 0.09‰

  lift = mean_present / mean_absent = 0.18 / 0.09 = 2.0
```

- **Lift > 1** → BAC is higher when this ingredient was recently eaten → 🔴 suspect
- **Lift ≤ 1** → BAC is the same or lower → 🟢 likely safe
- **Low confidence** → fewer than `min_obs` pairings → ⚠️ insufficient data

### Pipeline overview

```
Excel rows
    │
    ├──→ meals_df  (date, meal, meal_datetime, ingredient, quantity_g)
    │       │
    │       └───────────┐
    │                   ▼
    ├──→ bac_df    ──→ map_lookback(bac_df, meals_df, hours)
    │    (bac_datetime,     │
    │     promille,         ▼
    │     episode)     lookback_df  (bac_idx ↔ ingredient pairs)
    │                       │
    │                       ▼
    └──────────────→ compute_lift_scores(bac_df, lookback_df)
                            │
                            ▼
                    lift_scores (ingredient, lift, n_present, n_absent, …)
```

The **hours slider** in the web UI controls the look-back window width. A wider window (e.g. 6h) catches more meals per reading but dilutes the signal; a narrower window (e.g. 1h) is stricter but may miss meals that take longer to ferment.