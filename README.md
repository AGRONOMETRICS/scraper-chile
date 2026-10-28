# Scraper - Chile (Global Reefers DecoFrut FCM)

Scrapes the **DecoFrut FCM shipment report** from the Global Reefers API and
stores it in a PostgreSQL database: projected fruit shipments by season, market,
country, species, variety, exporter and importer, with their boxes and kilograms.

**Endpoint:** `GET https://api.globalreefers.app/api/reportes/decofrut/`
(docs at <https://api.globalreefers.app/docs/>, login required).

Same layout and pattern as `scraper-eurostat2`: every run pulls the report,
compares it against what is already stored, and reports which rows are **new**,
which are **updates**, and which stored rows the server no longer reports.

---

## Layout

| Path | What lives there |
|---|---|
| `main.py` | Fetching (paginated), parsing and the incoming-vs-stored comparison |
| `config.py` | `settings` (run-mode flags, URL, credentials, request tuning) and `get_logger` |
| `utils.py` | DataFrame casting/alignment, table truncation, email, timing, CLI parsing |
| `summary.py` | The run summary, rendered as plain text for the log and HTML for the email |
| `databases/db.py` | The column contract: which column is a key, a value, a label |
| `data/<season>/` | Cached JSON pages (git-ignored, written when `CREATE_JSON_FILES` is on) |
| `scripts/daily_process.sh` | Daily run (whole report) |
| `logs/` | `scraper_chile.log` |

---

## Setup

```bash
python -m venv env
env\Scripts\activate       # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in:

```
URL = https://api.globalreefers.app/
USERNAME = ...
PASSWORD = ...           # PASSWORSD is accepted too
DATABASE_URL = postgresql://user:password@localhost:5432/scraper-chile
RECORDS_TABLE = records
HISTORY_TABLE = records_original
```

The credentials are read straight from the `.env` file, because `USERNAME` is
also an OS variable on Windows and `load_dotenv` would not override it.

---

## Usage

```
python main.py [seasons ...] [--commodity COMMODITY ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--truncate] [--yes] [--email]
```

| Argument | Description | Default |
|---|---|---|
| `seasons` | Seasons as the report spells them (`2025-2026`) or by closing year (`2026`) | Whole report |
| `--commodity COMMODITY ...` | `Blueberries`, `Grapes`. One query per commodity | All of `COMMODITIES` |
| `--from` / `--to` | ETD range, inclusive, sent as `fechaDesde` / `fechaHasta` | Open |
| `--truncate` | Delete the stored rows for the scraped scope before comparing | — |
| `--yes` | Skip the `--truncate` confirmation prompt (for cron) | — |
| `--email` | Send the run summary by email | Off |

```bash
python main.py                                   # every season, all commodities
python main.py 2026                              # season 2025-2026
python main.py 2026 --commodity Grapes           # only grapes of that season
python main.py 2026 --from 2026-01-01 --to 2026-01-31
python main.py 2026 --truncate --yes             # wipe and rebuild the season
```

### Commodities (`main.py`)

The run covers only the commodities in `COMMODITIES`, named as our system names
them, each mapped to the `specie` the report files it under:

```python
COMMODITIES = [
    ('Blueberries', 'Arandanos'),
    ('Grapes', 'Uva De Mesa'),
]
```

Every stored row carries that name in the `commodity` column. Add a commodity
by adding a tuple (check the exact `specie` spelling against the report first:
`Arándanos` with an accent returns nothing).

---

## How a run decides what to write

1. **Fetch.** One query per (season × commodity), walked page by page
   (`pageSize=500`, the API maximum). The API orders by ETD and `rowId`, so
   sequential pages neither repeat nor skip rows. If the rows read do not add up
   to the `total` the server announced (the report reloaded mid-run), the read is
   discarded and the stored rows are left alone.
2. **Parse.** `parse_record` maps each row onto `databases/db.py`. Text is never
   `None` and numbers are never `NULL`. `rowId` is dropped (see caveats).
3. **History.** The raw rows for the scope replace the previous contents of
   `HISTORY_TABLE`, before value filtering. Rows with neither boxes nor kilograms
   never reach the records table.
4. **Scope.** The comparison is limited to the season the rows carry, plus the
   commodity asked for (`--commodity`) and the ETD range asked for (`--from`/`--to`).
   Stored rows outside that scope are never read as "removed from server".
5. **Clean duplicates / Compare / Report.** Unchanged from the Eurostat scraper:
   rows sharing a natural key are deleted and re-inserted; the rest are merged on
   `unique_together_keys` into new / updated / deleted, and the summary goes to
   the log (and email with `--email`), with a stored-vs-received count check.

### Column roles (`databases/db.py`)

| Role | Columns | Meaning |
|---|---|---|
| `unique` | `season`, `etd`, `etd_week`, `market`, `country`, `importer`, `exporter`, `pol`, `pod`, `specie`, `variety`, `production_method`, `cargo_type`, `vessel` | The natural key of a shipment row |
| value | `boxes`, `kilograms` | A change here makes the row an update |
| `label` | `commodity`, `market_group`, `region` | Derived names/groupings: written, never compared |
| `ignore` | `etl_processed` | Bookkeeping for the ETL |

### API field mapping

| API | Column |
|---|---|
| `etdweek` | `etd_week` (text, `week-year`, e.g. `1-2026`) |
| `productionmethod` | `production_method` |
| `cargotype` | `cargo_type` |
| `marketgroup` | `market_group` |
| `rowId` | not stored |
| — | `commodity`, from `specie` via `COMMODITIES` |
| everything else | same name |

---

## Configuration (`config.py`)

| Setting | Default | Description |
|---|---|---|
| `ALLOW_DUPLICATES` | `True` | Keep rows the server repeats |
| `TEST_RESPONSE` | `False` | Read cached JSON pages from `data/` instead of calling the API |
| `CREATE_JSON_FILES` | `True` | Write each page to `data/` |
| `DEBUG` | `True` | Log the full summary and the stored-vs-received count check |
| `REQUEST_DELAY` | `1` | Seconds between pages |
| `REQUEST_TIMEOUT` | `120` | Seconds to wait for a response |
| `REQUEST_RETRIES` | `2` | Extra attempts on timeouts / 5xx (400/401/403 are not retried) |
| `PAGE_SIZE` | `500` | Rows per page (API max) |

---

## Notes and API caveats

- **Auth is HTTP Basic.** The report rejects the Bearer JWT from
  `POST /api/token/` (`401 NOT_AUTHENTICATED`), while the other endpoints accept
  it. Basic with the same user/password works.
- **`rowId` is not a business id.** It is regenerated on every daily load, so the
  row is identified by every descriptive field instead.
- **Size.** The full report is ~103k rows (~207 pages of 500) as of 2026-10-28.
  A narrow ETD range (5 days) is ~3.7k rows, ~8 requests, ~20 s.
- **Filters are exact match** for `season`, `market`, `country` and `specie`,
  spelled as the report spells them (accents included: `Camión`, `Aéreo`).
- **Other endpoints** (`/api/endpoints/reservas/...`, `/tracking/...`,
  `/diccionarios/...`) are about the account's own bookings and are not scraped.
