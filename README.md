# peak-api-examples

Three Python scripts showing how to call the PEAK platform API
(`https://api.cimenviro.com`):

| Script | What it does |
|---|---|
| `scripts/get_token.py` | logs in with username and password — plus a 2FA code if your login needs one — and prints an offline token |
| `scripts/get_sites.py` | swaps that token for an access token, then calls `GET /sites` |
| `scripts/get_history.py` | exports gridded point history for every equipment of one type to a CSV |

Each script is standalone — read it top to bottom and copy it into your own code.
Runs on Windows, macOS and Linux; the only prerequisite is
[uv](https://docs.astral.sh/uv/).

## 1. Install uv

**Windows** (PowerShell): `winget install --id=astral-sh.uv -e`
**macOS**: `brew install uv`
**Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Open a new terminal so `uv` is on `PATH`, then:

```
git clone https://github.com/cim-data-engineering/peak-api-examples.git
cd peak-api-examples
uv sync
```

`uv sync` creates `.venv`, installs `httpx`, `polars` and `python-dotenv`, and
downloads Python itself if the machine doesn't have it. Every command below is `uv run …`,
which uses `.venv` for you — no activating, no `pip install`.

Each script also declares its own dependencies in a `# /// script` header, so
`uv run scripts/get_sites.py` works on a machine that never ran `uv sync` — uv
reads the header and builds a throwaway environment. Copy a script somewhere
else and it still runs.

## 2. Get an offline token

```
uv run scripts/get_token.py
```

It prompts for your PEAK username, your password (not echoed), and a code at a
prompt reading `TOTP code`.

**That last prompt only applies if PEAK asks you for a six-digit code when you
sign in.** If your PEAK login is username and password alone, press Enter to skip
it and the field is left out of the request entirely. If your login does use 2FA,
read the code from your authenticator app immediately before pressing Enter —
codes last 30 seconds.

The script then prints an **offline token** — a refresh token that stays valid
until it is revoked. Copy it into a file named `.env` in the repo root:

```
OFFLINE_TOKEN_ACCESS=eyJhbGciOi…
```

`.env` is gitignored. Treat the token like a password: it is enough to call the
API as you, so don't commit it or paste it into a chat.

This is `grant_type=password` with `scope=openid offline_access` — the same call
as:

```bash
curl -X POST 'https://login.cimenviro.com/auth/realms/cimenviro/protocol/openid-connect/token' \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data 'grant_type=password' \
  --data 'client_id=api-external' \
  --data 'scope=openid offline_access' \
  --data 'username=<username>' \
  --data 'password=<password>' \
  --data 'totp=<code>'          # omit this line unless your login uses 2FA
```

## 3. Call the API

```
uv run scripts/get_sites.py
```

It prints the active sites, one per line, minus any assigned to the client named
in `EXCLUDE_CLIENT_NAME` (the bucket buildings get parked in when they leave the
platform — change the name to match your customer):

```
     3  12 Example Street
    16  200 Example Quay
    20  6-7 Example Park Drive
```

Four steps, one function each, all commented in the file:

1. `get_access_token()` posts the offline token back as `grant_type=refresh_token`
   and gets a short-lived access token. These last 24 h on this realm, so the
   script mints one per run rather than storing it.
2. `get_client_id()` calls `GET /users/clients?client_name=…`. Sites carry client
   *ids* only, and the users service is the only place names live.
3. `get_all_sites()` pages through `GET /sites?is_active=true` by cursor — the
   cursor is the last `site_id` seen, and `order_by_site_id=true` is required
   alongside it. A short page means the end.
4. Sites whose `clients` list holds that id are dropped; the rest are printed.

To call another endpoint, copy `api_get()` and change the path — the bearer header
is the only auth involved. Field names come from the live Swagger JSON, linked at
the top of `api-reference.md`; don't guess them. That file also records what the
schema doesn't say: `limit=25` is what `GET /sites` reliably answers (bigger pages
time out), array filters must repeat the key, and `site_name` is exact-match.

## 4. Export some history

One line, so it copies and pastes into any shell:

```
uv run scripts/get_history.py --site "100 Example Street" --type "Air Handling Units" --metadata "Unit Supply Air Temperature (Fahrenheit)" "Unit Return Air Temperature (Fahrenheit)" --start 2026-08-19 --end 2026-08-20
```

`--start` is inclusive, `--end` exclusive, and both are dates in the **site's own
timezone**. `--out` names the file; without it the name is built from the
arguments. One row per timestamp, one column per equipment and point:

```
Timestamp (America/Chicago),"AHU-23.S, Unit Return Air Temperature (Fahrenheit), °F, Building, 4 to 37"
2026-08-19 00:00,74.228
2026-08-19 00:15,74.299995
```

Progress goes to stderr, so the run above says what it found:

```
site 411 100 Example Street (America/Chicago)
31 favourite(s) across 23 Air Handling Units -- lookups took 11.6s
15 minute grid, 2026-08-19 to 2026-08-20 in 1 window(s)
  batch 1/1: 2,976 samples in 1.7s
2,976 samples in 1.7s
gridded in 0.0s
wrote …csv: 96 rows x 31 point column(s) -- 16.3s all up
```

The timings are there to show where a slow run goes. Usually it is the lookups,
not the history: the zone, level and collector lists cover the whole site, so
they cost the same whether you export one AHU or fifty. Polars barely registers.

Why it takes eleven steps rather than one call: PEAK stores a **favourite** per
equipment/point pair, and history is fetched by favourite. Turning four names
into a list of `fav_id`s means looking up the site, the equipment type, the
points, the equipment, and the favourites on it — then the zones and levels for
the column headers, and the collectors for the grid interval. Each of those is
one function in the file, in the order `main()` calls them.

Two details that are easy to get wrong, both explained in the file:

- **Samples do not land on the quarter hour.** A 15-minute point reports at
  `00:00:43.602`. Polars snaps each sample to the nearest slot and keeps the
  latest one per slot — averaging would invent readings.
- **The grid is built in the site's local time, then the timezone is dropped.**
  That is what makes midnight mean midnight on the day the clocks change.

Change `build_labels()` for a different column header, or replace the last two
lines of `main()` to keep the DataFrame instead of writing a CSV.

## Windows

The Python is portable; only the shell differs. In PowerShell:

```powershell
uv sync
uv run scripts\get_token.py
uv run scripts\get_sites.py
uv run scripts\get_history.py --site "100 Example Street" --type "Chiller" --metadata "Chilled Water Entering Temperature" --start 2026-08-19 --end 2026-08-20
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `login failed: HTTP 401 … Invalid user credentials` | wrong username or password, or — on a 2FA login — a stale TOTP code. The realm reports all three the same way |
| `token exchange failed: HTTP 400 … invalid_grant` | the offline token was revoked or its session went idle — run `get_token.py` again |
| `no OFFLINE_TOKEN_ACCESS in .env` | step 2 — the `.env` file is missing or the line is misspelt |
| `GET /sites failed: HTTP 401` | the token is valid but the account lacks permission for the endpoint |
| `GET /sites failed: HTTP 504` | the API gateway timed out — the unfiltered query is slow; add a filter such as `params={"site_ids": [411]}` |
| `GET /history failed: HTTP 414` | too many `fav_ids` in one URL — lower `FAV_BATCH` in `get_history.py` (100 is proven, 500 fails) |
| `GET /history failed: HTTP 504` | too many rows in one request — lower `WINDOW_DAYS`. Don't retry a 504; it will time out again |
| `no active site named …` / `no equipment type named …` | these filters are exact matches, and type names are inconsistently pluralised — `Air Handling Units` but `Chiller` |
| `warning: no point named …` | the API drops metadata names it doesn't recognise instead of failing — check the spelling against `GET /metadata` |
| a level or zone in a CSV header is blank | a paged read came back short — `limit` and `start_index` must be sent together or the API silently returns 25 records |
