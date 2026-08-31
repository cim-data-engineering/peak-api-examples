# PEAK API reference

The tools in this repo talk to the PEAK platform at `https://api.cimenviro.com`.  
Rather than vendoring swagger snapshots (which go stale), fetch the **live**  
OpenAPI/Swagger JSON below. An AI client can pull these on demand to get the  
current schema for any endpoint.

| Service           | Swagger JSON                                                                                                 | Covers                                                                                                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Core**          | [https://api.cimenviro.com/swagger.json](https://api.cimenviro.com/swagger.json)                             | Primary platform data & operations: sites, levels, zones, meters, indoor environment, history.                                                                              |
| **Tickets**       | [https://api.cimenviro.com/tickets/swagger.json](https://api.cimenviro.com/tickets/swagger.json)             | Alert & action tickets, related activity, and metrics.                                                                                                                      |
| **Tasks**         | [https://api.cimenviro.com/tasks/swagger.json](https://api.cimenviro.com/tasks/swagger.json)                 | Rules, templates, template categories, and task history.                                                                                                                    |
| **Users**         | [https://api.cimenviro.com/users/swagger.json](https://api.cimenviro.com/users/swagger.json)                 | Users, clients, permissions (Keycloak-backed). **The only source of client *names*** — `GET /users/clients` returns `client_id`, `client_name`, `is_active`, `customer_id`. |
| **Notifications** | [https://api.cimenviro.com/notifications/swagger.json](https://api.cimenviro.com/notifications/swagger.json) | Event notifications via webhook subscriptions.                                                                                                                              |

## Clients: id ↔ name

The core data API (and the GraphQL `sites` query) only ever expose client **ids** —  
a site carries `clients: [19]`, never a name. To match a client by **name** (e.g.  
excluding the bucket that inactive buildings are parked in), resolve names via the  
**users** service, which is the only endpoint that returns them:

```
GET /users/clients?first=0&max=500        # all clients: {client_id, client_name, is_active, customer_id}
GET /users/clients?customer_id=1          # scope to a customer
GET /users/clients?client_name=Abacus     # by name
```

Verified 2026-08-20:

- **`client_name` matches the whole name, case-insensitively.** `Abacus` and
  `abacus` both return the one client; the prefix `Abac` returns none. Filter
  client-side for partial names.
- `response_metadata.record_count` is the total (319 clients visible to the account
  tested), and `first` / `max` page the list.
- A name that does not exist returns HTTP 200 with an empty `clients` array, not a
  404 — so "no match" and "no permission" look the same.

`scripts/get_sites.py` uses this to exclude a bucket of sites by client name: the id
differs per customer, the name does not.

## Users & preferences (users service)

The user endpoints:

```
GET   /users/users?customer_id= | client_id= | contractor_id=   # users for an entity
GET   /users/users/{id}?include_entity=true                     # user by id (+ its entity)
GET   /users/users/{id}/preferences                             # incl. site_notification_options
PATCH /users/users/{id}/preferences                             # deep-merges; empty body; null clears a key
GET   /users/permissions/current-user                           # caller's effective permissions
GET   /users/permissions/{customers|clients}/{id}               # role -> {users, groups} mapping
```

There is no per-user permissions endpoint — a user's roles come from their entity's  
permission mapping (contractor entities have none).

### Notifications: action vs alert are two separate resources

Per-site notifications are split across two services:

- **Action / ticket** notifications — users service, `preferences.site_notification_options`:  
a map of `site_id → "ALL_NOTIFICATIONS" | "ASSIGNED" | "NO_NOTIFICATIONS"`. PATCH  
merges per key (pass only the sites you want to change; `null` clears a key).  

- **Alert** notifications — notifications service:

```
GET  /notifications/sites-notifications/preferences?user_id=&type=alert&start_index=0
POST /notifications/sites-notifications/preferences    # upsert: include row id to update, omit to create
```

A row is `{ user_id, site_id, type: "alert", value }` with value  
  `"ALL_NOTIFICATIONS" | "NO_NOTIFICATIONS"`.

⚠️ The notifications per-id endpoints are **not implemented server-side**:  
`GET`/`DELETE /preferences/{id}` → 404, `PUT` → 501, collection `DELETE` → 405.  
So alert-preference rows can be created/toggled but **not deleted**;  
`NO_NOTIFICATIONS` is the "off" state.

## Sites: pagination and filters (`GET /sites`, core)

Verified against the live endpoint, 2026-08-20:

- **Send `limit`, and keep it small.** The gateway gives up on any request still
running at 30 s with HTTP 504 `Endpoint request timed out`. With neither `limit` nor
`start_index` the endpoint tries to return every matching site in one response —
fine for an account seeing 57 sites, a 504 for one seeing 549. `limit=100` also
504s; `limit=25` is reliable. **Do not retry a 504 — reduce the page size instead**:
the same request will just time out again. A large account therefore costs one
request per 25 sites (22 requests, ~2 min for 549).
- **`start_index` switches on offset paging** and is the only way to get  
`response_metadata.record_count` (the total) — but it caps the page at **25**  
unless `limit` is also sent.
- **`cursor` is keyset paging on `site_id`, exclusive** — pass the last id you saw.  
It is rejected with HTTP 400 unless `order_by_site_id=true` is also set:  
`Cannot query sites using cursor when order_by_site_id is not set to true.`
- **Array filters repeat the key**: `site_ids=411&site_ids=412`. The bracket form  
`site_ids[]=411` is **silently ignored** — the response is every site, which  
looks like a filter that matched everything. Comma-joined values 400.
- **`site_name` is an exact match**, not a substring search. Filter client-side  
for partial names.
- `client_id` is null on most site records; the client ids live in the `clients`  
array. Names still come only from the users service.
- **The comfort band has no unit on the site record.** `thermal_comfort_min_temp` /  
`_max_temp` are bare numbers, and both scales appear across USA sites (20-23.3  
and 67-75), so the unit is not inferable from `/sites`. It lives on the thermal  
comfort score responses (`ThermalComfortSiteScore.unit` / `unit_id`).

`scripts/get_sites.py` does the cursor loop.

## History export: the chain from names to samples

Verified against the live endpoints, 2026-08-21, building
`scripts/get_history.py`.

### Paging: send `limit` **and** `start_index` together, always

This is not the `/sites` behaviour above — the other collection endpoints
(`/equipment`, `/favourites`, `/zones`, `/zone_names`, `/levels`, `/collectors`,
`/metadata`) get it wrong three different ways:

- **`limit` alone is ignored.** Asked for 10 of 1,061 metadata records, got all
  1,061.
- **`start_index` alone caps the page at 25**, whatever `limit` said.
- **Neither returns 25 records and no total.** `GET /zones?site_id=411` answered
  with 25 of 885 zones, `response_metadata: null`, HTTP 200. The failure mode is
  a short list that looks complete — it showed up as blank level names in a CSV
  header, not as an error.

With both sent, paging behaves and `response_metadata.record_count` carries the
true total. `limit=500` on `/zones` returned 206 KB in 1.2 s.

### Exact-match lookups, and one silent drop

- **`GET /metadata_types?type=` is exact, and plurality is per-name.**
  `Air Handling Units` works and `Air Handling Unit` returns `[]`, but `Chiller`
  and `Boiler` are singular. Of the 97 names visible, 22 end in `s` — read the
  list rather than guessing.
- **`GET /metadata?type_id=&metadata_names=a&metadata_names=b` drops names it
  does not recognise** — HTTP 200, fewer records, no message. Diff the request
  against the response or a typo becomes a missing column. The record carries
  `unit` (`°C`, `°F`, `l/s`, `On/Off`), so no `/metadata_units` call is needed.
- **`GET /favourites` is not guaranteed to return `site_id`** even though the
  schema lists it, so filter by site server-side. Unfiltered, one site's
  favourites are 18,220 records / 6 MB / 13 s.
- Join favourites to equipment on **`canonical_equipment_id`**, not
  `equipment_id`. They are equal for standalone equipment; canonical points at
  the parent otherwise, and is what the platform's trend tooling uses.

### `GET /history`: two hard limits and an off-by-one

- **URL length.** `fav_ids` go in the query string, so the batch size is really a
  URL budget: 100 ids ≈ 2.2 KB works, 250 ≈ 5.5 KB works, **500 ≈ 11 KB is
  rejected by CloudFront with `414 The request could not be satisfied`** — an
  HTML body, not the JSON envelope, because the API never sees it.
- **Row count.** 31 favourites × 15 days = 44,609 rows in 12.9 s, roughly linear.
  The 30 s gateway limit applies, and as above a 504 must not be retried — ask
  for less.
- **`end` is inclusive by default.** With `end` set to a timestamp matching a
  sample exactly, the default returned that row; `end_exclusive=true` returned 0
  rows. Send `end_exclusive=true` or consecutive windows double-count the sample
  on the boundary.
- **`ts` is UTC with milliseconds** (`2026-08-19T05:00:41.561Z`) and samples do
  **not** land on grid boundaries — observed `00:00:43.602` and `23:45:18.458` on
  a 15-minute point. Anything that puts two points on one row has to snap them to
  a grid first.

### `collection_interval` is `PT15M` or `null`

`GET /collectors?site_id=` returns the polling interval as an ISO-8601 duration.
Across 209 collectors in this account: 140 `PT15M`, 69 `null`. Nothing else — so
a parser for `PT<n>M` / `PT<n>H` with a 15-minute default covers the observed
value space. `null` means the collector does not say, not that it does not poll.

`scripts/get_history.py` does all of the above.

## Metadata & favourites lifecycle: remap, replace, delete

Verified against the live API, 2026-08-28, while cleaning up duplicate
metadata on a customer account.

### `GET /favourites` is active-only by default — and so is the count

Without an `is_active` parameter, `GET /favourites` (REST **and** the GraphQL
`favourites` query) returns only active favourites, and
`GET /metadata/{id}/favourites/count` counts only active ones. Decommissioned
points are invisible unless `is_active=false` is queried explicitly — one
metadata showed 139 favourites everywhere while 841 inactive ones sat behind
it. Anything doing a "nothing references this" check must query both states.

### `POST /favourites` is the UI's edit path, with two validation gates

The body is an array of `FavouriteUpsert`; a present `fav_id` makes it an
update (this is how a favourite is remapped to different metadata). Two gates,
both batch-atomic (the whole array is rejected before anything writes):

- every row needs `device_object_id` **or** `identifier` — BACnet favourites
  carry the former, Haystack/SmartConnector/etc. the latter; pass through
  whichever the favourite has or the API answers
  `Favourite requires either a device object or an identifier`.
- a row with `is_active: false` is refused outright —
  `Deactivating a favourite is only permitted via DELETE` — **even when the
  favourite is already inactive**, so this path cannot remap decommissioned
  points at all. Use metadata replace (below) for those.

### `POST /metadata/{id}/replace` moves everything, then deletes the source

Body `{"source_metadata_id": N, "target_metadata_id": M}`. It re-points every
reference — active and inactive favourites included — **and deletes the source
metadata as part of the call**. There is no dry-run and no undo, so treat it
as remap+delete in one shot. This is the only API route that moves inactive
favourites.

### `DELETE /metadata/{id}` can 409 on references you cannot see

`409 Metadata is still referenced by other favourites` can come back when
every count visible to the calling account — active and inactive — is zero.
The referencing favourites exist outside the account's site grant (or as
soft-deleted rows); `/replace` fails with the same 409 in that case. Only an
account that can see the referencing rows can finish the job.

### The tasks service collection is `GET /tasks`, not `/tasks/tasks`

The service base path already ends in `/tasks`, so `GET /tasks/tasks` parses
the second segment as a `task_id` and answers
`{"errors":{"task_id":"(not (integer? \"tasks\"))"}}`. Rule filters like
`metadata_id=` / `fav_ids=` go on `GET /tasks` directly.

### `PUT /metadata/{id}` silently no-ops privileged fields — and, for service accounts, everything

Verified 2026-08-28 while attempting ownership transfer and un-sharing:

- **`customer_id` in the body is ignored** by every token tried: the owning
  tenant's service account (HTTP 200, record unchanged, `last_modified_at`
  bumped) and another tenant's service account (HTTP 200 with
  `"data": {"metadata": null}` — a scoped no-op). Ownership transfer is a
  database change, not an API operation.
- **`shared` and `commissionable` are also silently dropped** for the service
  account — a PUT that flips them returns 200 echoing the unchanged record.
  The whole update no-ops; nothing distinguishes success from ignore except
  re-reading the record. Regular user logins cannot even see the sharing flag
  in the UI: metadata sharing is admin-tier.
- The same account **can** `DELETE /metadata/{id}` — delete and update sit
  behind different permission checks.

Practical rule: after any metadata PUT, re-read the record and diff — a 200
proves nothing about what was applied.

### Corrections and additions from the merge run (2026-08-28, later)

- **"Sharing is admin-tier" was too strong.** The precise rule observed: a
  *tenant* service account's metadata PUTs no-op entirely, even on its own
  records — but a **customer-1 (CIM) account can edit CIM-owned metadata,
  `shared` included** (verified: flipped `shared` false→true on two CIM
  records via PUT). Ownership (`customer_id`) stays DB-only for every token
  tried.
- **A tenant cannot point a favourite at unshared metadata it cannot see** —
  the target resolves as not-found and the upsert is rejected. Share the
  target first.
- **`GET /favourites` synthesizes an `identifier` for BACnet favourites**
  ("binary-input 861") that is NOT stored on the row (`identifier` is NULL in
  the database when `device_object_id` is set). Echoing it back into
  `POST /favourites` trips identifier validation on some sites
  ("not a valid Haystack identifier"). Send only the favourite's real anchor:
  `device_object_id` when present, else `identifier`.
