#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28", "python-dotenv>=1.0"]
# ///
"""Log in to PEAK and print an offline token.

    uv run scripts/get_token.py

Run this once. It prompts for your username, your password, and a TOTP code if
the account has MFA, posts them to Keycloak, and prints the long-lived refresh
token -- the "offline token". Copy that into .env as OFFLINE_TOKEN_ACCESS, and
the other scripts can call the API without a password from then on.

Two fields make the token long-lived rather than session-length:

    grant_type=password           authenticate as a user, not as an app
    scope=openid offline_access   ask for a refresh token that outlives the login

Where that comes from: PEAK auth is Keycloak, and every realm publishes its own
settings at

    https://login.cimenviro.com/auth/realms/cimenviro/.well-known/openid-configuration

`token_endpoint` there is the URL below, and `grant_types_supported` /
`scopes_supported` list the two fields above. The README has the same request
written out as a curl command.
"""

import getpass

import httpx

# token_endpoint from the discovery document above.
TOKEN_URL = "https://login.cimenviro.com/auth/realms/cimenviro/protocol/openid-connect/token"

# The public client for API access. Not a secret, and it has no client secret --
# the user's own credentials are what authenticate the request.
CLIENT_ID = "api-external"


def login(username: str, password: str, totp: str = "") -> str:
    """Post the password grant and return the offline token.

    The response also carries an `access_token`, valid for 24 h. This script
    ignores it: get_sites.py mints its own access token from the offline token,
    so the only thing worth keeping is the one long-lived value.

    `totp` is the six-digit code from the authenticator app, and is optional --
    leave it out for an account with no MFA.
    """
    form = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "scope": "openid offline_access",
        "username": username,
        "password": password,
    }

    # Send `totp` only when there is a code. An empty field is not the same as no
    # field: a realm that asks for a code reads the blank as a wrong code, and one
    # that does not ask has no use for it either way.
    if totp:
        form["totp"] = totp

    response = httpx.post(TOKEN_URL, data=form, timeout=30)
    # Keycloak answers HTTP 401 "invalid_grant: Invalid user credentials" for an
    # unknown username, a wrong password and a stale TOTP code alike, so the
    # error cannot say which of the three it was.
    if response.status_code != 200:
        raise SystemExit(f"login failed: HTTP {response.status_code} {response.text}")
    return response.json()["refresh_token"]


def main() -> None:
    username = input("PEAK username: ")

    # getpass reads without echoing, so the password stays off the screen and out
    # of scrollback. There is deliberately no --password option: anything passed
    # as an argument lands in shell history and in the process list.
    password = getpass.getpass(f"Password for {username}: ")

    # Optional: press Enter to skip it if the account has no MFA. Codes last 30
    # seconds, so read yours immediately before pressing Enter -- an expired code
    # fails exactly like a wrong password.
    totp = input("TOTP code (press Enter if the account has no MFA): ").strip()

    # Printing the token is the point of this script, but treat it like a
    # password: it authenticates as you until it is revoked. Keep it out of
    # shared terminals, and never commit it.
    print(login(username, password, totp))


if __name__ == "__main__":
    main()
