# CLAUDE.md

A tutorial for Python beginners at a customer, who extend it themselves: standalone
scripts showing how to authenticate against the PEAK API and call one endpoint.
`README.md` is the getting-started guide.

**A customer reads this repo.** No CIM tenant, client, site or account name appears
anywhere in it: README, this file, docstrings, `.env.example`, comments, commit
messages. Use placeholders. Users here have no 2FA; do not document TOTP or MFA.

**Keep it minimal.** Flat scripts in `scripts/`, one function per step, no package,
no shared module, no CLI framework, no caching, no pre-commit. Each script reads top
to bottom on its own, and duplicating the token POST across scripts is deliberate —
a reader should not have to follow an import to understand one file. Before adding a
helper, an abstraction, a flag or a second way to do something, ask whether a beginner
reading the file needs it. Usually the answer is no.

**Every plan or script opens with the audience constraint** and states how the design
honours it. A line-count estimate that later triples is a scope signal: stop and check
before continuing.

## Tooling

- `uv` only — `uv sync`, `uv run scripts/…`, `uv add <pkg>`. Never `pip install`,
  and don't tell anyone to activate `.venv`. `uv sync` fetches Python too.
- `uv run ruff check .` and `uv run ruff format .` before committing. There are
  no git hooks in this repo — run them yourself.
- Python 3.13 target (`target-version = "py313"`), so `except (A, B):` keeps its
  parentheses.

## Secrets

The offline token is a long-lived credential — it authenticates as the user until
revoked.

- Never print it, never write it into a file in this repo, never put it in
  `.env.example`. Same for the password: no `--password` flag, and nothing reads
  a password from `argv`.
- `get_token.py` prints the token by design, because the user has to copy it
  somewhere. That is the only place a credential is written to stdout.
- The user keeps it in `.env` as `OFFLINE_TOKEN_ACCESS`. `.env` is gitignored.
  Nothing in this repo writes it to disk for them.

## Endpoint knowledge lives in api-reference.md

`api-reference.md` records what was verified against the live API — pagination
behaviour, filters that are silently ignored, endpoints that return 501. Read it
before adding an endpoint, and add to it when you verify something new (with the
date).

Do not guess field names: the live Swagger JSON is authoritative and listed there
(`https://api.cimenviro.com/swagger.json`). Fetch it rather than inferring a
schema from one response.
