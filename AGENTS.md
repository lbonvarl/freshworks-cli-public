# Agent guidance for `freshworks-cli`

## Purpose and scope

This repository is a small, self-contained Python CLI collection for
Freshworks accounts:

- `freshsales_cli/` implements the Freshsales CRM CLI (`freshsales`).
- `freshservice_cli/` implements the Freshservice service-desk CLI
  (`freshservice`).
- `tests/` contains offline unit tests for both packages.
- `tests_live/` contains the opt-in live write tests and their harness.

The project intentionally uses only the Python standard library at runtime.
Keep the two product clients separate and preserve the public JSON response
shape rather than introducing a lossy domain model.

## Safety and authorization

Read-only behavior is the default and is a core design requirement. Do not add
production write calls, bulk operations, deletes, or migrations without an
explicit user request and a reviewed safety design.

- The CLIs expose GET-backed commands only, with reviewed exceptions, all
  requested explicitly by the user: the Freshsales marketing-list writes
  (`lists create|rename|add-contacts|remove-contacts|move-contacts`),
  `contacts set-linkedin`, which changes only the contact `linkedin` field
  from a reviewed CSV and refuses to run if a contact changed since the
  review, the Freshsales single-record writes in `freshsales_cli/records.py`
  (`contacts|accounts|deals|tasks create|update`, `notes create`), and the
  Freshservice `tickets create|update|add-note` commands in
  `freshservice_cli/writes.py`, which change one ticket per command.
- Every new write command must follow the pattern in
  `freshsales_cli/writes.py`: the request is planned offline as a
  `WriteRequest`, it is a dry run by default (no Keychain read, no network),
  it needs `--apply` to execute, it preflights its targets with GET calls,
  destructive "all" variants need `--confirm <id>`, ID input is validated,
  de-duplicated, capped and batched, and a partial failure prints what was
  already applied. Only `--apply` may create a client with
  `allow_writes=True`.
- Record writes change one record per command, and only fields in the
  `RECORD_SPECS` allowlists (plus `cf_*` custom fields and tags for
  contacts, accounts and deals). Adding a field to an allowlist is a reviewed
  change. Phone numbers stay out. The `create_*`, `update_*` and `upsert_*`
  builders in `FreshsalesClient` stay unexposed. Do not widen `set-linkedin`
  to other fields. Do not add delete, forget, upsert, or bulk-destroy CLI
  commands. Deleting is allowed only in the live test harness, as described
  below.
- `FreshserviceClient` rejects non-GET requests unless explicitly opted in;
  the CLI opts in only for a ticket write run with `--apply`.
- Unit tests must use an in-memory recording/fake transport.
- Live write tests are allowed only through the opt-in harness in
  `tests_live/`. Its rules:
  - Run it only with `FRESHWORKS_LIVE_WRITE=1` and the user's go-ahead.
  - Create at most three records per entity per run. Give each the run tag
    and the `[CLI-TEST]` prefix, and write it to the ledger before using it.
  - Use an unsubscribed `@example.com` contact email. Keep any screening or
    compliance checkbox off, through `FRESHSALES_LIVE_ACCOUNT_FIELDS`. Make
    the key's own agent the ticket requester.
  - Delete only ledger records of the same run that still carry the marker,
    and verify each delete with a GET.
  - Deleting is allowed only in the harness, never in a CLI command.
  - If a run is interrupted, use `python -m tests_live.cleanup`.
- When exercising a live account, use the smallest useful GET and redact or
  summarize output. Never print API keys, authorization headers, cookies,
  attachments, JWTs, or unredacted production snapshots.

## Credentials and endpoints

API keys are loaded from the macOS Keychain with `security
find-generic-password`; they must not be accepted as CLI arguments or logged.

- Freshsales: account `api-key`, service `freshsales.api-key`, token header
  authentication, base URL from `--base-url` or `FRESHSALES_BASE_URL`
  (`https://<domain>.myfreshworks.com/crm/sales/api`).
- Freshservice: account `api-key`, service `freshservice.api-key`, Basic auth
  using `<api-key>:X`, base URL from `--base-url` or `FRESHSERVICE_BASE_URL`
  (`https://<domain>.freshservice.com/api/v2`).

There is no built-in default domain: never hard-code a real account's
hostname. Without a base URL, the CLI stops before reading the Keychain. Keep
Keychain loading injectable in tests.

## CLI surface

Maintain JSON output as the default and `--pretty` as the formatting option.

Freshsales currently supports contacts (including `contacts activities`),
accounts, deals, `tasks get|list`, `appointments get|list`, `search`,
`lookup`, and `fields <entity>` (field
definitions from `/settings/<entity>/fields`), with saved-view list
operations and GET detail operations. It also supports `campaigns list|engagement|
activity-types`, `lists list|contacts`, and `selector <name>`, plus the
dry-run-by-default list and `contacts set-linkedin` writes described under
"Safety and authorization". Campaign
analytics live in `freshsales_cli/analytics.py`; they compose GET calls
client-side and every audience walk must stay bounded by explicit
`--max-pages`/`--max-contacts` limits.

Freshservice currently supports:

- `tickets get|list|conversations|tasks|approvals|requested-items`,
  `filter-tickets`, and `ticket-fields`;
- `requesters get|list` (with `contacts` as an alias);
- `agents get|list|me` (`get|list` need an agent-admin role; `me` works
  with any role);
- `departments get|list` (with `companies` as an alias; Freshservice has no
  `/companies` route);
- `groups|workspaces|roles|announcements|canned-responses get|list`,
  `canned-responses folders`, and `requester-groups list`;
- `solutions categories|folders|articles|category|folder|article|search`.

Many other Freshservice resources (problems, changes, assets, contracts,
SLA policies, and others) need higher-privileged roles. Add a command for
one only after confirming with a live GET that a key can read it.

Keep API-specific filtering and pagination explicit. Freshservice list APIs
use `page` and `per_page` (1–100); avoid silently fetching every page or
embedding expensive related resources.

## Implementation directions

- Prefer `urllib` and `argparse` over a framework or new dependency while the
  command set remains small.
- Keep HTTP transport injectable so tests remain deterministic and offline.
- Validate IDs, required text, pagination, and absolute HTTP(S) base URLs at
  the client boundary.
- URL-encode path IDs and query parameters through the standard library.
- Convert expected credential, network, API, and response errors into concise
  CLI stderr messages without dumping response bodies.
- Add or update unit tests for every new route, parameter, auth behavior, and
  safety guard before implementation is considered complete.

## Verification checklist

From the repository root, run:

```sh
python -m unittest discover -s tests -v
python -m coverage run --branch --source=freshsales_cli,freshservice_cli \
  -m unittest discover -s tests
python -m coverage report --fail-under=100
ruff check .
ruff format --check .
python -m pip install -e .
python -m freshsales_cli --version
python -m freshservice_cli --version
```

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs the same
offline checks, but it is disabled in this repository. Run the checklist
locally before every push. Do not re-enable the workflow or add
new ones unless the user asks. Keep its ruff pin (0.17.3) in step with the
local ruff.

For a live smoke test, use a read-only GET only and report a redacted shape or
count. The repository is public: never commit raw CRM or helpdesk exports,
real record IDs, account hostnames, or details of a specific account's
configuration.
Local `sample_outputs/` snapshots are ignored intentionally.

## Change workflow

Keep changes narrow, inspect the diff, and preserve unrelated user changes.
Update `README.md` and the relevant package README when the CLI surface,
authentication, endpoint defaults, or safety behavior changes. Before pushing,
confirm the branch is clean, tests and lint pass, and no credentials or raw
production payloads are staged.
