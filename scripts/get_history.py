#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28", "polars>=1.0", "python-dotenv>=1.0"]
# ///
r"""Export point history for every equipment of one type, gridded, to a CSV.

Wrapped here to fit the page; README.md has the same command on one line, ready
to paste.

    uv run scripts/get_history.py \
      --site "100 Example Street" \
      --type "Air Handling Units" \
      --metadata "Unit Supply Air Temperature (Fahrenheit)" \
                 "Unit Return Air Temperature (Fahrenheit)" \
      --start 2026-08-19 --end 2026-08-20

One row per timestamp, one column per (equipment, point):

    Timestamp (America/Chicago),"AHU-1, Unit Supply Air Temperature, °F, Level 2, Open Plan"
    2026-08-19 00:00,55.4

PEAK stores a *favourite* per equipment/point pair, and history is fetched by
favourite. So the work is a chain of lookups that turns four names into a list of
fav_ids, then a fetch, then a reshape. Each step below is one function, in the
order main() calls them:

  1. get_access_token   offline token -> 24 h access token
  2. find_site          site name -> site_id and timezone
  3. find_metadata_type equipment type name -> type_id
  4. find_metadata      point names -> metadata_ids (and their units)
  5. find_equipment     every active equipment of that type at the site
  6. find_favourites    the fav_ids to fetch history for
  7. build_zone_labels  zone_id -> (level name, zone name), for the headers
  8. build_labels       fav_id -> the CSV column header
  9. choose_interval    how far apart the grid rows are
 10. fetch_history      the samples, in batches small enough to survive
 11. grid_and_pivot     snap to the grid, dedup, pivot, fill gaps

Things to try changing: the column label in build_labels(), the grid interval in
choose_interval_minutes(), or the last two lines of main() -- return the Polars
DataFrame instead of writing a CSV and you have the data ready to plot.

Where the field names come from: the live Swagger JSON is authoritative -- do not
guess them.

  core service   https://api.cimenviro.com/swagger.json

`api-reference.md` in this repo records the behaviour the schema does not: which
filters the server ignores, how paging really works, and what sizes make a
request fail.
"""

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import polars as pl
from dotenv import load_dotenv

TOKEN_URL = "https://login.cimenviro.com/auth/realms/cimenviro/protocol/openid-connect/token"
CLIENT_ID = "api-external"
API_URL = "https://api.cimenviro.com"

# Records per page. Only used together with start_index -- see paged_get().
PAGE_SIZE = 500

# Favourites per /history request. The fav_ids go in the URL, so the batch size is
# really a URL length limit: 100 ids is a 2.2 KB URL and works, 250 (5.5 KB) works,
# 500 (11 KB) is rejected by CloudFront with 414 before it reaches the API
# (verified 2026-08-21).
FAV_BATCH = 100

# Days of history per /history request. The gateway kills anything over 30 seconds
# with a 504, and a 504 must not be retried -- ask for less instead. 31 favourites
# x 15 days = 44,609 rows took 12.9 s, and the time grows with the row count, so
# 100 favourites x 3 days (~28,800 rows) leaves plenty of headroom.
WINDOW_DAYS = 3

# Used when a collector does not say how often it polls. Same default as the
# platform's own trend tooling.
DEFAULT_INTERVAL_MINUTES = 15


def get_access_token(offline_token: str) -> str:
    """Step 1: swap the long-lived offline token for a short-lived access token.

    This is the same POST as get_token.py, with grant_type=refresh_token instead
    of the password grant. Access tokens last 24 h on this realm, so scripts mint
    a fresh one per run rather than storing it.
    """
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": offline_token,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"token exchange failed: HTTP {response.status_code} {response.text}")
    return response.json()["access_token"]


def api_get(access_token: str, path: str, params: dict) -> dict:
    """GET a path on the API and return the `data` object from the response.

    Every PEAK response is the same envelope -- {"status": …, "message": …,
    "data": {…}} -- so the payload is always one level down, under a key named
    after the collection ("sites", "favourites", "history").

    A list in `params` becomes a repeated query key: {"fav_ids": [1, 2]} is sent
    as fav_ids=1&fav_ids=2, which is what the API expects. Never pass an empty
    list -- httpx drops the key entirely and the filter silently disappears,
    which is how a request for two points turns into a request for all 18,000.
    """
    response = httpx.get(
        f"{API_URL}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=60,
    )
    if response.status_code != 200:
        raise SystemExit(f"GET {path} failed: HTTP {response.status_code} {response.text}")
    return response.json()["data"]


def paged_get(access_token: str, path: str, params: dict, collection: str) -> list[dict]:
    """Read every record from a paged endpoint.

    Send `limit` AND `start_index` together, always. Each on its own goes wrong,
    and quietly (all verified 2026-08-21):

      * `limit` alone is ignored -- asking for 10 of 1,061 metadata records
        returned all 1,061.
      * `start_index` alone caps the page at 25 whatever you asked for.
      * neither returns 25 records and no total -- GET /zones?site_id=411 gave 25
        of 885 zones with nothing to say it was short. That is the dangerous one:
        the export still runs and the level names come out blank.

    A page shorter than PAGE_SIZE means there is nothing after it.
    """
    records: list[dict] = []
    start_index = 0
    while True:
        page_params = dict(params)
        page_params["limit"] = PAGE_SIZE
        page_params["start_index"] = start_index
        page = api_get(access_token, path, page_params)[collection]
        records.extend(page)
        if len(page) < PAGE_SIZE:
            return records
        start_index += PAGE_SIZE


def find_site(access_token: str, site_name: str) -> dict:
    """Step 2: the site record, which carries the site_id and its timezone.

    `site_name` is an exact, whole-name match. A near miss is not an error: the
    API answers HTTP 200 with an empty list.
    """
    sites = api_get(access_token, "/sites", {"site_name": site_name, "is_active": True})["sites"]
    if not sites:
        raise SystemExit(
            f"no active site named {site_name!r} -- site_name is an exact match, "
            "run get_sites.py to see the spellings"
        )
    return sites[0]


def find_metadata_type(access_token: str, type_name: str) -> dict:
    """Step 3: the equipment type, e.g. "Air Handling Units" -> type_id 6.

    Exact match again, and plurality is per-type, not a rule: "Air Handling
    Units" is plural and "Air Handling Unit" returns nothing, but "Chiller" and
    "Boiler" are singular. Only 22 of the 97 names end in "s" (verified
    2026-08-21), so the name has to be read off the list rather than guessed.
    """
    types = api_get(access_token, "/metadata_types", {"type": type_name})["metadata_types"]
    if not types:
        raise SystemExit(
            f"no equipment type named {type_name!r} -- the match is exact, and some "
            'names are plural while others are not: "Air Handling Units", "Chiller"'
        )
    return types[0]


def find_metadata(access_token: str, type_id: int, metadata_names: list[str]) -> list[dict]:
    """Step 4: the points to export, one record per name, with its unit.

    The API drops names it does not recognise instead of complaining, so compare
    what came back against what was asked for and say which ones are missing --
    otherwise a typo just means a column quietly absent from the CSV.
    """
    metadata = api_get(
        access_token,
        "/metadata",
        {"type_id": type_id, "metadata_names": metadata_names},
    )["metadata"]

    found = {point["name"] for point in metadata}
    missing = [name for name in metadata_names if name not in found]
    if missing:
        print(
            "warning: no point named " + ", ".join(repr(name) for name in missing),
            file=sys.stderr,
        )
    if not metadata:
        raise SystemExit("none of the --metadata names exist on this equipment type")
    return metadata


def find_equipment(access_token: str, site_id: int, metadata_type_id: int) -> list[dict]:
    """Step 5: every active equipment of this type at this site.

    This list is what "all the AHUs" means -- a favourite is only exported if its
    equipment appears here. Each record carries the name for the column header,
    the zone_id for the level and zone, and the collector_id for the interval.
    """
    equipment = paged_get(
        access_token,
        "/equipment",
        {"site_id": site_id, "metadata_type_id": metadata_type_id, "is_active": True},
        "equipment",
    )
    if not equipment:
        raise SystemExit("the site has no active equipment of that type")
    return equipment


def find_favourites(access_token: str, site_id: int, metadata_ids: list[int]) -> list[dict]:
    """Step 6: the favourites -- one per equipment/point pair -- to fetch.

    Filter by site on the server, not afterwards: the response is not guaranteed
    to carry site_id even though the schema lists it, and an unfiltered read of
    this endpoint is 18,000 records and 6 MB.

    `canonical_equipment_id` is the field to join equipment on. It points at the
    parent equipment, which is the same thing as equipment_id for a plain AHU but
    not for equipment that is part of a larger assembly.
    """
    return paged_get(
        access_token,
        "/favourites",
        {"site_id": site_id, "metadata_ids": metadata_ids, "is_active": True},
        "favourites",
    )


def build_zone_labels(access_token: str, site_id: int) -> dict[int, tuple[str, str]]:
    """Step 7: zone_id -> (level name, zone name), for the column headers.

    Three calls, because the names live one table away from the zone: a zone
    holds a level_id and a zone_name_id, and those resolve to the text. All three
    endpoints need paging -- site 411 has 885 zones, 84 zone names and 64 levels.
    """
    zones = paged_get(access_token, "/zones", {"site_id": site_id}, "zones")
    zone_names = paged_get(access_token, "/zone_names", {"site_id": site_id}, "zone_names")
    levels = paged_get(access_token, "/levels", {"site_id": site_id}, "levels")

    zone_name_by_id = {record["zone_name_id"]: record["zone_name"] for record in zone_names}
    level_name_by_id = {record["level_id"]: record["level_name"] for record in levels}

    labels = {}
    for zone in zones:
        labels[zone["zone_id"]] = (
            level_name_by_id.get(zone["level_id"], ""),
            zone_name_by_id.get(zone["zone_name_id"], ""),
        )
    return labels


def build_labels(
    favourites: list[dict],
    equipment: list[dict],
    metadata: list[dict],
    zone_labels: dict[int, tuple[str, str]],
) -> dict[int, str]:
    """Step 8: fav_id -> CSV column header.

    Five comma-separated fields: equipment, point, unit, level, zone. The commas
    are why every header ends up quoted in the CSV.

    Favourites whose equipment is not in `equipment` are skipped -- the same
    point name can exist on other equipment types at the site.
    """
    equipment_by_id = {record["equipment_id"]: record for record in equipment}
    metadata_by_id = {record["metadata_id"]: record for record in metadata}

    labels: dict[int, str] = {}
    fav_id_by_label: dict[str, int] = {}
    for favourite in favourites:
        equip = equipment_by_id.get(favourite["canonical_equipment_id"])
        if equip is None:
            continue
        point = metadata_by_id[favourite["metadata_id"]]
        level_name, zone_name = zone_labels.get(equip["zone_id"], ("", ""))
        label = ", ".join(
            [
                equip["name"],
                point["name"],
                point["unit"] or "",
                level_name,
                zone_name,
            ]
        )
        # Two favourites on the same equipment and point would become one column
        # in the pivot, silently losing data. Stop instead of guessing.
        if label in fav_id_by_label:
            raise SystemExit(
                f"two favourites would share the column {label!r} "
                f"(fav_id {fav_id_by_label[label]} and {favourite['fav_id']}) -- "
                "pick one with GET /favourites and narrow --metadata"
            )
        fav_id_by_label[label] = favourite["fav_id"]
        labels[favourite["fav_id"]] = label

    if not labels:
        raise SystemExit("no favourites match that equipment type and those points")
    return labels


def parse_interval_minutes(collection_interval: str | None) -> int:
    """Turn a collector's polling interval into minutes.

    Only PT<n>M and PT<n>H are handled. That is not general ISO-8601 parsing --
    it is the entire set of values this API returns: across 209 collectors, 140
    said "PT15M" and 69 said null (verified 2026-08-21).
    """
    if collection_interval is None:
        return DEFAULT_INTERVAL_MINUTES
    if collection_interval.startswith("PT") and collection_interval.endswith("M"):
        return int(collection_interval[2:-1])
    if collection_interval.startswith("PT") and collection_interval.endswith("H"):
        return int(collection_interval[2:-1]) * 60
    print(
        f"note: cannot read collection_interval {collection_interval!r}, "
        f"using {DEFAULT_INTERVAL_MINUTES} minutes",
        file=sys.stderr,
    )
    return DEFAULT_INTERVAL_MINUTES


def choose_interval_minutes(access_token: str, site_id: int, equipment: list[dict]) -> int:
    """Step 9: how far apart the rows of the CSV are.

    Read it from the collectors behind the selected equipment rather than asking
    for it on the command line. One CSV is one grid, so when collectors disagree
    the coarsest one wins -- a 15-minute grid would put a 30-minute point on every
    second row and leave gaps in between.
    """
    collectors = paged_get(access_token, "/collectors", {"site_id": site_id}, "collectors")
    interval_by_collector = {
        record["collector_id"]: record["collection_interval"] for record in collectors
    }

    collector_ids = sorted(
        {record["collector_id"] for record in equipment if record["collector_id"] is not None}
    )
    minutes = [
        parse_interval_minutes(interval_by_collector.get(collector_id))
        for collector_id in collector_ids
    ]
    if not minutes:
        return DEFAULT_INTERVAL_MINUTES
    return max(minutes)


def seconds_since(mark: float) -> str:
    """How long since a time.monotonic() mark, for the progress lines.

    Worth printing: the lookups and the history fetch fail in different ways, so
    knowing which one is slow tells you whether to narrow --metadata or lower
    WINDOW_DAYS. monotonic() rather than time() because it cannot jump backwards.
    """
    return f"{time.monotonic() - mark:.1f}s"


def utc_string(local_time: datetime, site_zone: ZoneInfo) -> str:
    """A site-local wall-clock time as the UTC instant string the API wants.

    The dates on the command line are local to the site: midnight in Chicago is
    05:00Z or 06:00Z depending on the season, and only the timezone knows which.
    """
    return local_time.replace(tzinfo=site_zone).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def date_windows(
    start_local: datetime, end_local: datetime, window_days: int
) -> list[tuple[datetime, datetime]]:
    """Split the requested range into chunks of at most `window_days` days."""
    windows = []
    window_start = start_local
    while window_start < end_local:
        window_end = min(window_start + timedelta(days=window_days), end_local)
        windows.append((window_start, window_end))
        window_start = window_end
    return windows


def fetch_history(
    access_token: str,
    fav_ids: list[int],
    windows: list[tuple[datetime, datetime]],
    site_zone: ZoneInfo,
) -> list[dict]:
    """Step 10: the raw samples, as one flat list of {fav_id, ts, data}.

    Two nested loops, because two different things limit one request: how many
    fav_ids fit in the URL (FAV_BATCH) and how many rows fit in 30 seconds
    (WINDOW_DAYS).

    `end_exclusive=true` on every request. By default `end` is inclusive, so
    consecutive windows would both return the sample sitting exactly on the
    boundary (verified 2026-08-21).
    """
    batches = [fav_ids[index : index + FAV_BATCH] for index in range(0, len(fav_ids), FAV_BATCH)]
    total = len(batches) * len(windows)

    rows: list[dict] = []
    done = 0
    for batch in batches:
        for window_start, window_end in windows:
            batch_started = time.monotonic()
            page = api_get(
                access_token,
                "/history",
                {
                    "fav_ids": batch,
                    "start": utc_string(window_start, site_zone),
                    "end": utc_string(window_end, site_zone),
                    "end_exclusive": True,
                },
            )["history"]
            rows.extend(page)
            done += 1
            print(
                f"  batch {done}/{total}: {len(page):,} samples in {seconds_since(batch_started)}",
                file=sys.stderr,
            )
    return rows


def grid_and_pivot(
    rows: list[dict],
    labels: dict[int, str],
    site_timezone: str,
    start_local: datetime,
    end_local: datetime,
    interval_minutes: int,
) -> pl.DataFrame:
    """Step 11: samples in, one wide table out.

    Samples do not land on neat boundaries -- a 15-minute point reports at
    00:00:43.602 and 23:45:18.458 -- so they have to be snapped to a grid before
    two points can share a row. This is the same method the platform's own trend
    tooling uses in SQL, written as Polars expressions.

    One statement per stage, so each can be printed and inspected on its own.
    """
    # 1. The raw samples. `data` can be null, hence Float64 rather than a cast.
    frame = pl.DataFrame(
        {
            "fav_id": [row["fav_id"] for row in rows],
            "ts": [row["ts"] for row in rows],
            "data": [row["data"] for row in rows],
        },
        schema={"fav_id": pl.Int64, "ts": pl.Utf8, "data": pl.Float64},
    )

    # 2. UTC text -> the site's own wall clock, with the timezone then dropped.
    #    The grid is computed on naive local time on purpose: that is what makes
    #    "midnight" mean midnight on the day the clocks change.
    frame = frame.with_columns(
        pl.col("ts")
        .str.to_datetime(format="%Y-%m-%dT%H:%M:%S%.fZ", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone(site_timezone)
        .dt.replace_time_zone(None)
        .alias("local_ts")
    )

    # 3. Snap each sample to the nearest grid slot: 00:00:43 -> 00:00,
    #    23:52:10 -> 00:00 the next day on a 15-minute grid.
    frame = frame.with_columns(pl.col("local_ts").dt.round(f"{interval_minutes}m").alias("bucket"))

    # 4. One value per point per slot, latest sample wins. Sort by time first,
    #    then take the last row of each group -- not the average, which would
    #    invent a reading that the equipment never reported.
    frame = frame.sort("local_ts").group_by(["fav_id", "bucket"]).agg(pl.col("data").last())

    # 5. fav_id is meaningless in a spreadsheet; swap it for the column header.
    frame = frame.with_columns(
        pl.col("fav_id").replace_strict(labels, return_dtype=pl.Utf8).alias("label")
    )

    # 6. Long to wide: one row per slot, one column per point.
    wide = frame.pivot(on="label", index="bucket", values="data")

    # 7. Every slot in the requested range gets a row, even the ones where
    #    nothing reported, so a dropout reads as an empty cell instead of
    #    vanishing from the file. closed="left" honours the exclusive --end.
    grid = pl.DataFrame(
        {
            "bucket": pl.datetime_range(
                start_local,
                end_local,
                f"{interval_minutes}m",
                closed="left",
                time_unit="ms",
                eager=True,
            )
        }
    )
    table = grid.join(wide, on="bucket", how="left")

    # 8. A point that reported nothing at all in the range has no column yet.
    for label in labels.values():
        if label not in table.columns:
            table = table.with_columns(pl.lit(None, dtype=pl.Float64).alias(label))

    # 9. Sort the columns so two runs of the same request produce the same file,
    #    and format the timestamp -- left alone, Polars writes ISO-8601 with
    #    seconds and a T in the middle.
    table = table.select(["bucket", *sorted(labels.values())])
    table = table.with_columns(pl.col("bucket").dt.strftime("%Y-%m-%d %H:%M"))
    return table.rename({"bucket": f"Timestamp ({site_timezone})"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export point history for every equipment of one type, gridded, to a CSV."
    )
    parser.add_argument("--site", required=True, help="exact site name")
    parser.add_argument("--type", required=True, help="exact equipment type name, e.g. Chiller")
    parser.add_argument("--metadata", required=True, nargs="+", help="exact point name(s)")
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD, site local")
    parser.add_argument("--end", required=True, help="day to stop before, YYYY-MM-DD, site local")
    parser.add_argument("--out", help="CSV to write (default: built from the arguments)")
    return parser.parse_args()


def parse_date(text: str, flag: str) -> datetime:
    """A YYYY-MM-DD argument as midnight, with no timezone attached yet."""
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"{flag} must look like 2026-08-19, not {text!r}") from None


def main() -> None:
    started = time.monotonic()
    args = parse_args()

    start_local = parse_date(args.start, "--start")
    end_local = parse_date(args.end, "--end")
    if end_local <= start_local:
        raise SystemExit("--end must be after --start (--start is inclusive, --end is exclusive)")

    load_dotenv()
    offline_token = os.environ.get("OFFLINE_TOKEN_ACCESS")
    if not offline_token:
        raise SystemExit("no OFFLINE_TOKEN_ACCESS in .env -- run get_token.py to make one")

    access_token = get_access_token(offline_token)

    lookups_started = time.monotonic()
    site = find_site(access_token, args.site)
    site_zone = ZoneInfo(site["timezone"])
    print(f"site {site['site_id']} {site['site_name']} ({site['timezone']})", file=sys.stderr)

    metadata_type = find_metadata_type(access_token, args.type)
    metadata = find_metadata(access_token, metadata_type["type_id"], args.metadata)
    equipment = find_equipment(access_token, site["site_id"], metadata_type["type_id"])
    favourites = find_favourites(
        access_token, site["site_id"], [point["metadata_id"] for point in metadata]
    )
    zone_labels = build_zone_labels(access_token, site["site_id"])
    labels = build_labels(favourites, equipment, metadata, zone_labels)
    print(
        f"{len(labels)} favourite(s) across {len(equipment)} {metadata_type['type']}"
        f" -- lookups took {seconds_since(lookups_started)}",
        file=sys.stderr,
    )

    interval_minutes = choose_interval_minutes(access_token, site["site_id"], equipment)
    windows = date_windows(start_local, end_local, WINDOW_DAYS)
    print(
        f"{interval_minutes} minute grid, {args.start} to {args.end} in {len(windows)} window(s)",
        file=sys.stderr,
    )

    fetch_started = time.monotonic()
    rows = fetch_history(access_token, sorted(labels), windows, site_zone)
    print(f"{len(rows):,} samples in {seconds_since(fetch_started)}", file=sys.stderr)
    if not rows:
        raise SystemExit("no samples in that range -- check the dates, or try a wider one")

    grid_started = time.monotonic()
    table = grid_and_pivot(rows, labels, site["timezone"], start_local, end_local, interval_minutes)
    print(f"gridded in {seconds_since(grid_started)}", file=sys.stderr)

    out_path = args.out or (
        f"{site['site_name']} - {metadata_type['type']} - {args.start} to {args.end}.csv"
    )
    table.write_csv(out_path)
    print(
        f"wrote {out_path}: {table.height} rows x {table.width - 1} point column(s)"
        f" -- {seconds_since(started)} all up",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
