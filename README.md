# freshworks-cli

A small Python CLI collection for Freshsales CRM and Freshservice. It uses only
the Python standard library at runtime and keeps both command-line interfaces
read-only by default. The only commands that change data are the Freshsales
marketing-list commands, `contacts set-linkedin`, the Freshsales record
writes, and the Freshservice ticket writes. They are dry runs unless you pass
`--apply` (see
[Freshsales marketing-list write commands](#freshsales-marketing-list-write-commands),
[Setting contact LinkedIn aliases](#setting-contact-linkedin-aliases),
[Freshsales record write commands](#freshsales-record-write-commands) and
[Freshservice ticket write commands](#freshservice-ticket-write-commands)).

## Setup

Create a virtual environment if desired, then install the project in editable
mode:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The Freshsales CLI expects its API key in the macOS Keychain under:

```sh
security add-generic-password \
  -a api-key \
  -s freshsales.api-key \
  -U \
  -w
```

The Freshservice CLI uses a separate Keychain entry:

```sh
security add-generic-password \
  -a api-key \
  -s freshservice.api-key \
  -U \
  -w
```

Each CLI also needs your account's API base URL. There is no built-in
default: set it once in your shell profile, or pass `--base-url` on each call.

```sh
export FRESHSALES_BASE_URL="https://<your-domain>.myfreshworks.com/crm/sales/api"
export FRESHSERVICE_BASE_URL="https://<your-domain>.freshservice.com/api/v2"
```

Without a base URL a command stops with an error before reading the
Keychain. API keys are never accepted as command-line arguments and are not
printed.

## Freshsales read commands

```sh
freshsales contacts filters
freshsales contacts list --view-id 4 --page 1
freshsales contacts get 123 --include sales_accounts

freshsales accounts filters
freshsales accounts list --view-id 2
freshsales accounts get 456 --include contacts,deals

freshsales deals filters
freshsales deals list --view-id 1 --sort updated_at --sort-type desc
freshsales deals get 789 --include sales_account,contacts

freshsales search "Acme" --include contact,sales_account,deal --per-page 25
freshsales lookup person@example.com --field email --entities contact

freshsales tasks list --filter overdue        # open, due_today, due_tomorrow, overdue, completed
freshsales tasks get 321 --include owner,targetable
freshsales appointments list --filter past    # upcoming (default) or past
freshsales appointments get 654 --include appointment_attendees

freshsales fields contacts                    # also accounts, deals, sales-activities
freshsales fields deals --include field_group
```

Output is JSON. Add `--pretty` for formatted output. List endpoints use the
Freshsales saved-view IDs returned by each `filters` command. `fields`
returns each field's label, type and dropdown choices, which is how to read
the custom (`cf_*`) fields and choice IDs in contact, account and deal
records.

`tasks list` (default filter `open`) and `appointments list` (default
`upcoming`) use the API's own filters and `--page`. Do not rely on the
appointment filter names alone: in testing, `upcoming` and `past` returned
the same total, sorted oldest first and newest first, including appointments
long past. Check each appointment's `from_date` instead.

## Freshsales campaigns, lists and analytics

```sh
freshsales campaigns list                     # id/name of every campaign
freshsales lists list                         # marketing lists (campaign audiences)
freshsales lists contacts 123 --page 1        # contacts in one marketing list
freshsales contacts activities 456 --limit 50 # one contact's timeline
freshsales selector lead_sources              # any documented /selector table

# 1. Discover which activity types your tenant records for campaign emails
freshsales --pretty campaigns activity-types --list-id 123

# 2. Analysts in the audience who opened the product launch campaign
freshsales campaigns engagement --list-id 123 \
  --action-type open --campaign "launch" --since 2026-09-01 \
  --title analyst --format csv > analyst-opens.csv
```

These `selector` tables were confirmed on a live account: `owners`,
`territories`, `deal_stages`, `deal_pipelines`, `deal_reasons`, `deal_types`,
`deal_payment_statuses`, `currencies`, `lead_sources`, `industry_types`,
`business_types`, `contact_statuses`, `lifecycle_stages`, `campaigns`,
`sales_activity_types`, `sales_activity_outcomes` and
`sales_activity_entity_types` (also listed in `freshsales selector --help`).
If `campaigns list` returns no campaigns (for example when campaigns are sent
from another tool), pass campaign text to `--campaign` instead of an ID.

Freshsales does not publish a campaign-statistics or recipient-events API, so
`campaigns engagement` rebuilds it from documented GET endpoints: it walks a
marketing list (`--list-id`) or saved contact view (`--view-id`), applies the
contact filters (`--title`, repeatable `--match FIELD=REGEX` with dotted
fields such as `custom_field.cf_persona=analyst`), fetches each remaining
contact's `/contacts/<id>/activities`, and keeps activities whose
`action_type` matches `--action-type` (a case-insensitive regex, default
`OPEN`). `--campaign` requires the text to appear in the activity details and
`--since` drops older activities. Output is a JSON report (counts, truncation
flag, contacts with their matching events) or one CSV row per contact.

The `action_type` names used for campaign opens and clicks are not documented,
so run `campaigns activity-types` on a small sample first; it reports counts
and `action_data` key names only, never values. If opens are not recorded in
contact timelines at all (for example when the campaign was sent from
Freshmarketer, or opens tracking is off), export the campaign report from the
UI instead.

Every walk is bounded: `--max-pages` (default 5) caps audience pages and
`--max-contacts` (default 200; 25 for `activity-types`) caps timeline calls,
one API call per contact. When a limit is hit the report sets
`"truncated": true`. Use `--delay` to space calls out under the Freshsales
hourly rate limit.

## Freshsales marketing-list write commands

These commands, together with `contacts set-linkedin` below, are the only
ones that change data in Freshsales. They cover the documented [marketing list API](https://developers.freshworks.com/crm/api/#marketing-lists):

| Command | Request | Effect |
| --- | --- | --- |
| `lists create NAME` | `POST /lists` | Create a list |
| `lists rename ID NAME` | `PUT /lists/ID` | Rename a list |
| `lists add-contacts ID --ids …` | `PUT /lists/ID/add_contacts` | Add contacts to a list |
| `lists remove-contacts ID --ids …` | `PUT /lists/ID/remove_contacts` | Unlink contacts from a list |
| `lists remove-contacts ID --all --confirm ID` | same, body `{"all": true}` | Empty a list |
| `lists move-contacts ID --from-list SRC --ids …` | `PUT /lists/ID/move_contacts` | Move contacts from SRC to ID |
| `lists move-contacts ID --from-list SRC --all --confirm SRC` | same, body `{"from_list_id": SRC}` | Move every contact of SRC |

Removing a contact from a list never deletes the contact. Freshsales has no
list-delete endpoint, and the CLI has no delete, forget, or bulk-destroy
command for any entity.

### How a write runs

1. **Dry run by default.** Without `--apply`, a write command validates its
   input and prints the exact request(s) it would send: method, full URL,
   JSON body and a summary. It does not read the Keychain or contact
   Freshsales, so you can check a plan anywhere.
2. **`--apply` sends it.** Only then is the API key loaded and a client
   created with writes enabled. Before any write, the CLI reads the marketing
   lists and stops if a target list (and `--from-list`) does not exist.
   `lists create` also stops if a list with the same name already exists,
   unless you pass `--allow-duplicate-name`.
3. **Emptying a list needs `--confirm`.** `--all` must be paired with
   `--confirm <ID>`, where ID is the list being emptied: the list itself for
   `remove-contacts`, the `--from-list` for `move-contacts`.
4. **Bounded batches.** Contact IDs are validated as positive integers and
   de-duplicated, then sent in batches of `--batch-size` (default 100, max
   500). One command accepts at most 1,000 IDs.
5. **Nothing fails silently.** The applied report lists each request with its
   response. If a batch fails part-way, the CLI prints the report of the
   batches already sent to stdout, prints the error to stderr, and exits 1.
   Batches are not rolled back, so check the report before re-running.

Contact IDs come from `--ids 1,2,3`, from `--ids-file` (one ID per line with
`#` comments allowed, or any CSV with an `id` column), or from standard input
with `--ids-file -`. `--ids` and `--ids-file` can be combined.

### Example: build an analyst follow-up list

```sh
# 1. Find the analysts who opened the campaign (read-only)
freshsales campaigns engagement --list-id 123 --campaign launch \
  --title analyst --format csv > analyst-opens.csv

# 2. Create the follow-up list: preview, then apply
freshsales lists create "Product launch – analyst openers"
freshsales lists create "Product launch – analyst openers" --apply
#    → note the new list id in the response, e.g. 456

# 3. Add the openers: preview, then apply
freshsales --pretty lists add-contacts 456 --ids-file analyst-opens.csv
freshsales lists add-contacts 456 --ids-file analyst-opens.csv --apply
```

Freshsales applies some list changes asynchronously. `move-contacts` reports
"Updating now…", and a live check showed `remove-contacts` returning
"Contact updated." while `lists contacts` still listed the contact for a few
seconds; additions appeared immediately. Wait a few seconds before checking a
list with `lists contacts`, and do not re-run a write just because the change
is not visible yet.

### Programmatic use

`FreshsalesClient` exposes `plan_create_list`, `plan_rename_list`,
`plan_add_contacts`, `plan_remove_contacts`, `plan_move_contacts` and
`plan_set_contact_linkedin`. They
return a `WriteRequest` built offline, and `client.send(plan)` executes it.
Convenience wrappers (`create_list`, `rename_list`, `add_contacts_to_list`,
`remove_contacts_from_list`, `move_contacts_between_lists`) do both.
`send` raises `WriteDisabledError` before any network call unless the client
was created with `allow_writes=True`.

## Setting contact LinkedIn aliases

`contacts set-linkedin` writes the `linkedin` field of contacts from a
reviewed CSV, and no other field.

```sh
freshsales --pretty contacts set-linkedin --csv review.csv          # dry run
freshsales contacts set-linkedin --csv review.csv --apply           # send
```

The CSV needs `contact_id`, `current_linkedin` and `suggested_linkedin`
columns. Other columns (names, confidence, notes) are ignored, so a review
table can be passed as is. Rows with an empty `suggested_linkedin` are
skipped; remove a row, or clear its suggestion, to leave that contact alone.

- **Aliases only.** `suggested_linkedin` must be the profile alias, the part
  after `linkedin.com/in/` (for example `jane-doe-1a2b3c`). URLs, slashes and
  dots are rejected, so a URL is never stored by mistake. Percent-encoded
  characters are allowed for non-ASCII names.
- **Dry run by default.** Without `--apply` the command prints one
  `PUT /contacts/<id>` per row with the body `{"contact": {"linkedin":
  "<alias>"}}`, without reading the Keychain or calling Freshsales.
- **Stale reviews are refused.** With `--apply`, the CLI first reads every
  contact and compares its `linkedin` value with `current_linkedin`. If any
  contact is missing or has changed since the review, nothing is sent.
- **Bounded.** A CSV may hold at most 200 updates, and a contact ID may appear
  only once. If an update fails part-way, the report lists the updates already
  applied, as for the list commands.

To undo a run, swap the `current_linkedin` and `suggested_linkedin` columns of
the CSV you applied and run it again. A contact whose old value was empty
cannot be cleared this way, because empty suggestions are skipped; clear it
in the Freshsales UI.

## Freshsales record write commands

These commands create or update one record at a time:

| Command | Request |
| --- | --- |
| `contacts create\|update` | `POST /contacts`, `PUT /contacts/ID` |
| `accounts create\|update` | `POST /sales_accounts`, `PUT /sales_accounts/ID` |
| `deals create\|update` | `POST /deals`, `PUT /deals/ID` |
| `tasks create\|update` | `POST /tasks`, `PUT /tasks/ID` |
| `notes create` | `POST /notes` |

```sh
freshsales --pretty deals update 789 --set amount=25000 --set deal_stage_id=<stage id>
freshsales deals update 789 --set amount=25000 --set deal_stage_id=<stage id> --apply

freshsales tasks create --set "title=Follow up" --set due_date=2026-11-05T09:00:00Z \
  --set targetable_type=Deal --set targetable_id=789 --apply
freshsales notes create --set "description=Met at the conference" \
  --set targetable_type=Contact --set targetable_id=123 --apply
```

Fields are set with repeatable `--set FIELD=VALUE` options. Each command's
`--help` lists its writable fields:

- **contacts:** `first_name`, `last_name`, `job_title`, `email`, `city`,
  `country`, `subscription_status`, `owner_id`
- **accounts:** `name`, `website`, `address`, `city`, `zipcode`, `country`,
  `industry_type_id`, `owner_id`
- **deals:** `name`, `amount`, `sales_account_id`, `deal_pipeline_id`,
  `deal_stage_id`, `expected_close`, `owner_id`
- **tasks:** `title`, `description`, `due_date`, `owner_id`, `status`, plus
  `targetable_type` and `targetable_id` on create only
- **notes:** `description`, `targetable_type`, `targetable_id`

Contacts, accounts and deals also take custom fields (`--set cf_name=value`,
with `true`/`false` for checkboxes) and repeatable `--tag` options; on update,
`--tag` replaces the whole tag list. `targetable_type` is `Contact`,
`SalesAccount` or `Deal`. Use `fields <entity>` and `selector <name>` to find
field names and choice IDs.

- **Dry run by default.** Without `--apply`, the command prints the exact
  request without reading the Keychain or calling Freshsales.
- **Checked targets.** With `--apply`, `update` reads the record first, stops
  if it does not exist, and reports the current value of each field it
  changes. `create` checks that the account or record it points to exists,
  and refuses to create a contact whose email already belongs to a contact.
- **Values are validated offline.** Unknown fields, IDs that are not positive
  integers, malformed dates and non-numeric amounts are rejected before
  anything is sent.
- **Your account's required fields.** Freshsales may reject a create that
  leaves out fields your forms mark as required, including custom fields.
  Check them with `freshsales fields <entity>` (the `required` flag) and
  pass them with `--set`.
- **Fields left out on purpose.** Phone numbers are not writable, and there is
  no delete, forget or upsert command.

## Write safety

The CLI creates its API client with writes disabled. The only exception is a
write command run with `--apply`, as described above. The client's
`create_*`, `update_*` and `upsert_*` request builders are not exposed as
commands. Unit tests send writes only to an in-memory recording transport.
The [live write tests](#live-write-tests) are the only code that sends writes
to a real account, and they are opt-in.

The filtered-search API is also deferred because Freshsales documents it as a
`POST`; the initial CLI uses the read-only `GET /search` and `GET /lookup`
endpoints.

## Freshservice read commands

The Freshservice package lives in [`freshservice_cli/`](freshservice_cli/) and
is installed as the `freshservice` command:

```sh
freshservice tickets list --per-page 30 --include requester
freshservice tickets get 123 --include requester,stats,tags   # tags only when included
freshservice filter-tickets "priority:3 AND status:2"
freshservice tickets conversations 123 --per-page 100   # replies and notes
freshservice tickets tasks 123
freshservice tickets approvals 123
freshservice tickets requested-items 123                # service requests

freshservice requesters list --email person@example.com
freshservice requesters get 456
freshservice contacts list --include-agents

freshservice ticket-fields                    # labels and choice IDs for tickets

freshservice agents me
freshservice agents list --query "name:'Ada'"
freshservice departments list --query "name:'Finance'"

freshservice groups list                      # agent groups; also groups get ID
freshservice requester-groups list
freshservice workspaces list
freshservice roles list
freshservice announcements list --state active   # active, archived or scheduled
freshservice canned-responses list
freshservice canned-responses folders

freshservice solutions categories             # knowledge base
freshservice solutions folders --category-id 12
freshservice solutions articles --folder-id 34
freshservice solutions article 56
freshservice solutions search "vpn"
```

`agents get|list` need an API key whose agent role can manage agents;
otherwise Freshservice answers 403. `agents me` reads the agent that owns the
key and works with any role. Freshservice has no `/companies` route: companies
(the MSP-mode name) are departments in the API, so `companies` is kept as an
alias of `departments`.

`groups`, `workspaces`, `roles`, `announcements` and `canned-responses` also
have `get ID`. `requester-groups` is list-only, because group details and
members need an admin role. `groups list` does not take `--workspace-id`,
because that filter needs access to every workspace.

What a key can read depends on its agent's role and the Freshservice plan.
Resources such as problems, changes, assets and contracts often need
higher-privileged roles, and none of them have commands yet.

Freshservice list calls accept `--page` and `--per-page` (1–100). Ticket
listing also supports the documented requester, email, status/filter, type,
workspace, updated-since, ordering, and embedding parameters. Use
`tickets conversations` to read a full thread: `--include conversations`
on `tickets get` embeds only the first ten. Ticket time entries and
satisfaction responses are not exposed.

## Freshservice ticket write commands

These are the only Freshservice commands that change data. Each one changes
a single ticket.

| Command | Request | Effect |
| --- | --- | --- |
| `tickets create --subject … --description … --workspace-id … (--requester-id … \| --email …)` | `POST /tickets` | Create a ticket |
| `tickets update ID [--subject …] [--status …] [--priority …] [--tag …]` | `PUT /tickets/ID` | Change those fields only |
| `tickets add-note ID --text … [--public]` | `POST /tickets/ID/notes` | Add a note (private by default) |

```sh
freshservice --pretty tickets update 123 --status 4          # dry run
freshservice tickets update 123 --status 4 --apply           # send
```

- **Dry run by default.** Without `--apply`, the command prints the exact
  request without reading the Keychain or calling Freshservice.
- **Checked target.** With `--apply`, `update` and `add-note` first read the
  ticket and stop if it does not exist. The report shows its subject, status,
  priority and tags before the change, so you can see which tags
  `update --tag` replaced.
- **Validated values.** Status is 2 (Open), 3 (Pending), 4 (Resolved) or
  5 (Closed). Priority is 1 (Low) to 4 (Urgent); `create` defaults to Low and
  Open. Tags are at most 32 characters, at most 10 per ticket, and
  `update --tag` replaces the ticket's whole tag list.
- **Notifications.** Freshservice may email the requester, and the agents
  and groups its automations choose, when a ticket is created, updated or
  gets a public note. Use `--requester-id` with your own agent ID
  (`freshservice agents me`) for trials.

There is no delete command. Only the opt-in live test suite deletes tickets,
and only the ones it created (see [Live write tests](#live-write-tests)).

## Tests

The unit tests are offline: they use mocked Keychain subprocesses and an
in-memory HTTP transport with fixture responses.

```sh
python -m unittest discover -s tests -v
```

Run the same checks before pushing; the full set requires 100% line and
branch coverage:

```sh
python -m coverage run --branch --source=freshsales_cli,freshservice_cli \
  -m unittest discover -s tests
python -m coverage report --fail-under=100
ruff check . && ruff format --check .
```

The GitHub Actions workflow `.github/workflows/ci.yml` runs these checks on
Python 3.11–3.14, with ruff pinned to 0.17.3. It is disabled in this
repository, so nothing runs on push. Re-enable it with
`gh workflow enable CI`. CI never runs the live write tests.

### Live write tests

`tests_live/` holds opt-in tests that run the write commands against the real
accounts. They are skipped unless `FRESHWORKS_LIVE_WRITE=1` is set, and the
command above never collects them.

```sh
# Both suites read FRESHSALES_BASE_URL / FRESHSERVICE_BASE_URL (see Setup).

# Freshservice: one ticket in the given workspace, requested by the key's own agent
FRESHWORKS_LIVE_WRITE=1 FRESHSERVICE_LIVE_WORKSPACE_ID=<id> \
  python -m unittest discover -s tests_live -t . -p 'test_freshservice_live.py' -v

# Freshsales: one account, contact, deal, task and note owned by the given user.
# FRESHSALES_LIVE_ACCOUNT_FIELDS adds fields your account form requires.
FRESHWORKS_LIVE_WRITE=1 FRESHSALES_LIVE_OWNER_ID=<user id> \
  FRESHSALES_LIVE_ACCOUNT_FIELDS='cf_region=EMEA;cf_screening_check=false' \
  python -m unittest discover -s tests_live -t . -p 'test_freshsales_live.py' -v
```

Each run:

1. **Labels everything.** Each record gets a run tag
   (`clitest-<UTC minute>-<random>`) where the entity has tags, and a
   `[CLI-TEST]` prefix in its name, subject, title or description.
2. **Stays small.** At most three records of each kind are created per run;
   the suites create one of each.
3. **Logs before acting.** Each record ID is written to the local ledger
   `tests_live/.live-ledger.json` (git-ignored) as soon as it is created.
4. **Checks every step** with a GET.
5. **Deletes what it created, then verifies.** Children are deleted first,
   then each record is read again to confirm it is gone. A record is
   deleted only if it is in the run's ledger and still carries the run tag or
   the prefix. Anything that cannot be verified fails the run.

Verification differs by product. A deleted Freshsales contact, account,
deal or task returns 404 at once, and a deleted note no longer appears on
its parent record. A deleted Freshservice ticket goes to the trash: the
API still returns it, with `deleted: true`, and the harness checks that
flag. The trashed ticket remains until it is purged from the Freshservice
trash.

The test contact has an unsubscribed `@example.com` address. Use
`FRESHSALES_LIVE_ACCOUNT_FIELDS` to switch off any screening or compliance
checkbox on the test account, so creating it starts no review. The test
ticket's requester is
the agent who owns the API key, so any notification goes to that agent. The
API has no general-purpose delete: only this harness deletes, and only its
own records.

If a run is interrupted, delete its leftovers with the same checks:

```sh
FRESHWORKS_LIVE_WRITE=1 python -m tests_live.cleanup
```

The API references are available at [Freshsales CRM](https://developers.freshworks.com/crm/api/)
and [Freshservice API v2](https://api.freshservice.com/v2/).
