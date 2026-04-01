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