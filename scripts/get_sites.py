#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28", "python-dotenv>=1.0"]
# ///
"""List the active sites, minus the ones parked in one client bucket.

    uv run scripts/get_sites.py

Four steps, each in its own function below:

  1. read the offline token from .env and swap it for a short-lived access token
     (grant_type=refresh_token -- get_token.py makes the offline token)
  2. look up the client id for EXCLUDE_CLIENT_NAME, because only the users
     service knows client *names*
  3. page through GET /sites with is_active=true, following the cursor until the
     API stops returning sites
  4. drop any site that has that client id in its `clients` list, and print the
     rest

Where the field names come from: the live Swagger JSON is authoritative -- do
not guess them.

  core service   https://api.cimenviro.com/swagger.json        -> GET /sites
  users service  https://api.cimenviro.com/users/swagger.json  -> GET /users/clients

`api-reference.md` in this repo records the behaviour the schema does not: how
paging works, and which filters the server ignores instead of rejecting.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

TOKEN_URL = "https://login.cimenviro.com/auth/realms/cimenviro/protocol/openid-connect/token"
CLIENT_ID = "api-external"
API_URL = "https://api.cimenviro.com"

# This is used to filter sites using the "client" hack
EXCLUDE_CLIENT_NAME = "Inactive Sites"

# Sites per request. 25 is what the API returns by default; Requests that exceed
# 30 seconds will fail due to a gateway error (504). The request should not be
# automatically retried, reduce the page size instead
PAGE_SIZE = 25


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
    after the collection ("sites", "clients").
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


def get_client_id(access_token: str, client_name: str) -> int | None:
    """Step 2: find a client's id from its name, or None if there is no match.

    A site record carries client *ids* only (`clients: [19]`), never names, and
    the ids differ per customer. The users service is the only place names live:
    GET /users/clients returns client_id, client_name, is_active and customer_id.

    `client_name` matches the whole name, case-insensitively -- a prefix like
    "Inactive" returns nothing (verified 2026-08-20).
    """
    clients = api_get(access_token, "/users/clients", {"client_name": client_name})["clients"]
    if not clients:
        return None
    return clients[0]["client_id"]


def get_all_sites(access_token: str, is_active: bool) -> list[dict]:
    """Step 3: every site the account can see, one page at a time.

    Paging is by cursor: `cursor` is the last site_id already seen and is
    exclusive, so passing it asks for "the next sites after this id". Two rules
    come with it:

      * `order_by_site_id=true` is required, otherwise the request is rejected
        with HTTP 400 "Cannot query sites using cursor when order_by_site_id is
        not set to true".
      * a short page means the end -- fewer sites came back than were asked for,
        so there is nothing after them.
    """
    sites: list[dict] = []
    cursor = None
    while True:
        params = {"is_active": is_active, "order_by_site_id": True, "limit": PAGE_SIZE}
        if cursor is not None:
            params["cursor"] = cursor
        page = api_get(access_token, "/sites", params)["sites"]
        sites.extend(page)
        if len(page) < PAGE_SIZE:
            return sites
        cursor = page[-1]["site_id"]


def main() -> None:
    load_dotenv()
    offline_token = os.environ.get("OFFLINE_TOKEN_ACCESS")
    if not offline_token:
        raise SystemExit("no OFFLINE_TOKEN_ACCESS in .env -- run get_token.py to make one")

    access_token = get_access_token(offline_token)

    exclude_id = get_client_id(access_token, EXCLUDE_CLIENT_NAME)
    if exclude_id is None:
        # Not an error: a customer may simply not have a bucket by that name.
        print(
            f"note: no client named {EXCLUDE_CLIENT_NAME!r} -- excluding nothing", file=sys.stderr
        )

    sites = get_all_sites(access_token, is_active=True)

    # Step 4: a site's `clients` list holds the client ids it is assigned to.
    # (The `client_id` field on the same record is null on most sites, which is
    # why the list is what gets checked.)
    kept = [site for site in sites if exclude_id not in (site["clients"] or [])]

    for site in kept:
        print(f"{site['site_id']:>6}  {site['site_name']}")
    print(
        f"{len(kept)} active site(s), {len(sites) - len(kept)} excluded as {EXCLUDE_CLIENT_NAME!r}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
