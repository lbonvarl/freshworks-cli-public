"""Command-line interface for the read-only Freshsales client."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__, analytics, records, writes
from .auth import load_api_key
from .client import (
    APPOINTMENT_FILTERS,
    BASE_URL_ENV,
    DEFAULT_TIMEOUT,
    FIELD_ENTITIES,
    TASK_FILTERS,
    FreshsalesClient,
)
from .errors import FreshsalesError, PartialWriteError

DEFAULT_SEARCH_ENTITIES = "contact,sales_account,deal"

# /selector tables confirmed by a live GET. Not enforced: other documented
# names are still passed through.
KNOWN_SELECTORS = (
    "owners",
    "territories",
    "deal_stages",
    "deal_pipelines",
    "deal_reasons",
    "deal_types",
    "deal_payment_statuses",
    "currencies",
    "lead_sources",
    "industry_types",
    "business_types",
    "contact_statuses",
    "lifecycle_stages",
    "campaigns",
    "sales_activity_types",
    "sales_activity_outcomes",
    "sales_activity_entity_types",
)


class RawText(str):
    """Command output that is printed verbatim instead of JSON-encoded."""


def _add_common_entity_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    resource: str,
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    parser = subparsers.add_parser(
        resource,
        help=f"read {resource} from Freshsales",
        description=f"Read {resource} from Freshsales.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    get_parser = actions.add_parser("get", help=f"get one {resource[:-1]}")
    get_parser.add_argument("id", help=f"{resource[:-1]} ID")
    get_parser.add_argument(
        "--include",
        help="comma-separated related resources to embed",
    )

    actions.add_parser("filters", help=f"list saved {resource} views")

    list_parser = actions.add_parser("list", help=f"list {resource} from a view")
    list_parser.add_argument("--view-id", required=True, help="saved view ID")
    list_parser.add_argument(
        "--page", type=int, default=1, help="page number (default: 1)"
    )
    list_parser.add_argument("--sort", help="API sort field")
    list_parser.add_argument(
        "--sort-type",
        choices=("asc", "desc"),
        help="sort direction",
    )
    list_parser.add_argument(
        "--include",
        help="comma-separated related resources to embed",
    )
    return actions


def _add_activity_entity_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    resource: str,
    filters: tuple[str, ...],
    include_example: str,
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    singular = resource[:-1]
    parser = subparsers.add_parser(
        resource,
        help=f"read {resource} from Freshsales",
        description=f"Read {resource} from Freshsales.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    get_parser = actions.add_parser("get", help=f"get one {singular}")
    get_parser.add_argument("id", help=f"{singular} ID")
    get_parser.add_argument(
        "--include", help=f"comma-separated related resources, e.g. {include_example}"
    )

    list_parser = actions.add_parser("list", help=f"list {resource} by filter")
    list_parser.add_argument(
        "--filter",
        dest="filter_name",
        choices=filters,
        default=filters[0],
        help=f"which {resource} to list (default: {filters[0]})",
    )
    list_parser.add_argument(
        "--page", type=int, default=1, help="page number (default: 1)"
    )
    list_parser.add_argument(
        "--include", help=f"comma-separated related resources, e.g. {include_example}"
    )
    return actions


def _add_record_write_commands(
    actions: argparse._SubParsersAction[argparse.ArgumentParser], entity: str
) -> None:
    spec = records.RECORD_SPECS[entity]
    singular = entity[:-1]
    fields = ", ".join(spec.fields)
    if spec.custom_fields:
        fields += ", cf_* custom fields (true/false for checkboxes)"
    note = (
        "WRITE COMMAND: dry run by default - prints the planned request "
        "without loading the API key or calling Freshsales. Add --apply to "
        "check the records it refers to exist and send it. Writable fields: "
        f"{fields}."
    )
    commands = [("create", f"create one {singular}")]
    if spec.updatable:
        commands.append(("update", f"update one {singular}"))
    for action, help_text in commands:
        parser = _add_write_parser(actions, action, help_text, note)
        if action == "update":
            parser.add_argument("id", help=f"{singular} ID")
        parser.add_argument(
            "--set",
            dest="assignments",
            action="append",
            default=[],
            metavar="FIELD=VALUE",
            help="field to set; repeatable",
        )
        if spec.custom_fields:
            parser.add_argument(
                "--tag",
                action="append",
                help=(
                    "tag; repeatable"
                    + ("; replaces the whole tag list" if action == "update" else "")
                ),
            )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _add_audience_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--list-id", help="marketing list ID (see `lists list`)")
    source.add_argument("--view-id", help="saved contact view ID")


def _add_limit_arguments(
    parser: argparse.ArgumentParser, *, max_pages: int, max_contacts: int
) -> None:
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=max_pages,
        help=f"audience pages to read at most (default: {max_pages})",
    )
    parser.add_argument(
        "--max-contacts",
        type=_positive_int,
        default=max_contacts,
        help=(
            "contacts (after filters) whose timeline is fetched at most; each "
            f"costs one API call (default: {max_contacts})"
        ),
    )
    parser.add_argument(
        "--activity-limit",
        type=_positive_int,
        default=analytics.DEFAULT_ACTIVITY_LIMIT,
        help=(
            "activities requested per contact "
            f"(default: {analytics.DEFAULT_ACTIVITY_LIMIT})"
        ),
    )
    parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=0.0,
        help="seconds to wait between timeline calls, to respect rate limits",
    )


def _add_campaign_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "campaigns",
        help="read campaigns and campaign engagement",
        description=(
            "Read campaigns and rebuild campaign engagement from contact "
            "timelines. All commands are GET-only."
        ),
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list campaign IDs and names")

    engagement = actions.add_parser(
        "engagement",
        help="contacts in an audience whose timeline has matching email events",
        description=(
            "Walk a marketing list or saved view, fetch each contact's "
            "activities, and keep contacts with activities whose action_type "
            "matches --action-type (and optional campaign/date filters)."
        ),
    )
    _add_audience_arguments(engagement)
    engagement.add_argument(
        "--action-type",
        default=analytics.DEFAULT_ACTION_PATTERN,
        help=(
            "case-insensitive regex on activity action_type "
            f"(default: {analytics.DEFAULT_ACTION_PATTERN!r}); "
            "run `campaigns activity-types` to discover the names in use"
        ),
    )
    engagement.add_argument(
        "--campaign",
        help="case-insensitive text that must appear in the activity details",
    )
    engagement.add_argument(
        "--since", help="only activities at or after this ISO date/timestamp"
    )
    engagement.add_argument(
        "--title",
        help="case-insensitive regex on the contact job_title, e.g. 'analyst'",
    )
    engagement.add_argument(
        "--match",
        action="append",
        default=[],
        metavar="FIELD=REGEX",
        help=(
            "extra contact filter; FIELD may be dotted "
            "(e.g. custom_field.cf_persona=analyst). Repeatable."
        ),
    )
    engagement.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="json report (default) or one CSV row per engaged contact",
    )
    _add_limit_arguments(
        engagement,
        max_pages=analytics.DEFAULT_MAX_PAGES,
        max_contacts=analytics.DEFAULT_MAX_CONTACTS,
    )

    census = actions.add_parser(
        "activity-types",
        help="count activity types across a small audience sample",
        description=(
            "Sample an audience and count the action_type values found in "
            "contact timelines. Only action_data key names are shown."
        ),
    )
    _add_audience_arguments(census)
    _add_limit_arguments(census, max_pages=1, max_contacts=25)


def _add_list_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "lists",
        help="read and manage marketing lists",
        description=(
            "Read and manage Freshsales marketing lists. Write commands are "
            "dry runs unless --apply is given."
        ),
    )
    actions = parser.add_subparsers(dest="action", required=True)
    list_parser = actions.add_parser("list", help="list marketing lists")
    list_parser.add_argument(
        "--page", type=int, default=1, help="page number (default: 1)"
    )
    contacts_parser = actions.add_parser(
        "contacts", help="list the contacts in a marketing list"
    )
    contacts_parser.add_argument("id", help="marketing list ID")
    contacts_parser.add_argument(
        "--page", type=int, default=1, help="page number (default: 1)"
    )

    write_note = (
        "WRITE COMMAND: dry run by default - prints the planned request(s) "
        "without loading the API key or calling Freshsales. Add --apply to "
        "check the target list(s) exist and send the request(s)."
    )

    create = _add_write_parser(actions, "create", "create a marketing list", write_note)
    create.add_argument("name", help="name of the new list")
    create.add_argument(
        "--allow-duplicate-name",
        action="store_true",
        help="create even if a list with the same name already exists",
    )

    rename = _add_write_parser(actions, "rename", "rename a marketing list", write_note)
    rename.add_argument("id", help="marketing list ID")
    rename.add_argument("name", help="new list name")

    add = _add_write_parser(
        actions, "add-contacts", "add contacts to a marketing list", write_note
    )
    add.add_argument("id", help="destination marketing list ID")
    _add_ids_arguments(add, allow_all=False)

    remove = _add_write_parser(
        actions,
        "remove-contacts",
        "remove contacts from a marketing list",
        write_note + " Contacts are only unlinked from the list, never deleted. --all "
        "empties the list and also requires --confirm <list ID>. Freshsales may "
        "take a few seconds before `lists contacts` reflects the removal.",
    )
    remove.add_argument("id", help="marketing list ID")
    _add_ids_arguments(remove, allow_all=True)

    move = _add_write_parser(
        actions,
        "move-contacts",
        "move contacts from one marketing list to another",
        write_note + " --all moves every contact of --from-list and also requires "
        "--confirm <from-list ID>. Freshsales processes moves asynchronously.",
    )
    move.add_argument("id", help="destination marketing list ID")
    move.add_argument("--from-list", required=True, help="source marketing list ID")
    _add_ids_arguments(move, allow_all=True)


def _add_write_parser(
    actions: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    description: str,
) -> argparse.ArgumentParser:
    parser = actions.add_parser(
        name,
        help=f"{help_text} (write; dry run unless --apply)",
        description=description,
    )
    parser.set_defaults(write=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually send the request(s); without it the command is a dry run",
    )
    return parser


def _add_ids_arguments(parser: argparse.ArgumentParser, *, allow_all: bool) -> None:
    parser.add_argument("--ids", help="comma-separated contact IDs")
    parser.add_argument(
        "--ids-file",
        help=(
            "file with one contact ID per line, or a CSV with an 'id' column "
            "(e.g. `campaigns engagement --format csv` output); '-' for stdin"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=writes.DEFAULT_BATCH_SIZE,
        help=(
            f"contact IDs per request, 1-{writes.MAX_BATCH_SIZE} "
            f"(default: {writes.DEFAULT_BATCH_SIZE}); at most "
            f"{writes.MAX_IDS_PER_COMMAND} IDs per command"
        ),
    )
    if allow_all:
        parser.add_argument(
            "--all",
            dest="all_contacts",
            action="store_true",
            help="act on every contact in the list instead of --ids/--ids-file",
        )
        parser.add_argument(
            "--confirm",
            metavar="LIST_ID",
            help="required with --all: the ID of the list being emptied",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freshsales",
        description="Small, read-only-first Freshsales CRM CLI.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(BASE_URL_ENV) or None,
        help=(
            f"Freshsales API base URL, e.g. https://<domain>.myfreshworks.com/crm/sales/api; "
            f"defaults to the {BASE_URL_ENV} environment variable"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="resource", required=True)
    contact_actions = _add_common_entity_commands(subparsers, resource="contacts")
    activities_parser = contact_actions.add_parser(
        "activities", help="list one contact's activity timeline"
    )
    activities_parser.add_argument("id", help="contact ID")
    activities_parser.add_argument(
        "--include", help="comma-separated related resources, e.g. user"
    )
    activities_parser.add_argument(
        "--limit", type=_positive_int, help="maximum number of activities"
    )
    set_linkedin = _add_write_parser(
        contact_actions,
        "set-linkedin",
        "set the linkedin alias of contacts from a reviewed csv",
        "WRITE COMMAND: dry run by default - prints the planned request(s) "
        "without loading the API key or calling Freshsales. Add --apply to "
        "check each contact still has its reviewed current_linkedin value "
        "and send the update(s). Only the linkedin field is changed.",
    )
    set_linkedin.add_argument(
        "--csv",
        required=True,
        help=(
            "csv with contact_id, current_linkedin and suggested_linkedin "
            "columns; rows with an empty suggested_linkedin are skipped; "
            f"at most {writes.MAX_CONTACT_UPDATES} updates; '-' for stdin"
        ),
    )
    _add_record_write_commands(contact_actions, "contacts")
    for resource in ("accounts", "deals"):
        _add_record_write_commands(
            _add_common_entity_commands(subparsers, resource=resource), resource
        )
    task_actions = _add_activity_entity_commands(
        subparsers,
        resource="tasks",
        filters=TASK_FILTERS,
        include_example="owner,targetable",
    )
    _add_record_write_commands(task_actions, "tasks")
    notes_parser = subparsers.add_parser(
        "notes",
        help="add notes to contacts, accounts or deals",
        description="Add notes. Notes can be created but not read back here.",
    )
    _add_record_write_commands(
        notes_parser.add_subparsers(dest="action", required=True), "notes"
    )
    _add_activity_entity_commands(
        subparsers,
        resource="appointments",
        filters=APPOINTMENT_FILTERS,
        include_example="targetable,appointment_attendees",
    )
    _add_campaign_commands(subparsers)
    _add_list_commands(subparsers)

    fields_parser = subparsers.add_parser(
        "fields",
        help="read field definitions: labels, types and dropdown choices",
        description=(
            "Read the field definitions of an entity, including custom "
            "(cf_*) fields and the choice IDs used in records."
        ),
    )
    fields_parser.add_argument("entity", choices=tuple(FIELD_ENTITIES))
    fields_parser.add_argument(
        "--include", help="comma-separated extras, e.g. field_group"
    )

    selector_parser = subparsers.add_parser(
        "selector",
        help="read a /selector lookup table (owners, campaigns, lead_sources, ...)",
        description=(
            "Read a /selector lookup table. Names confirmed on a live "
            f"account: {', '.join(KNOWN_SELECTORS)}."
        ),
    )
    selector_parser.add_argument("name", help="selector name, e.g. lead_sources")

    search_parser = subparsers.add_parser(
        "search",
        help="search names and email addresses across entities",
    )
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--include",
        default=DEFAULT_SEARCH_ENTITIES,
        help=(f"comma-separated entities (default: {DEFAULT_SEARCH_ENTITIES})"),
    )
    search_parser.add_argument(
        "--per-page",
        type=int,
        default=10,
        help="number of results, from 1 to 100 (default: 10)",
    )

    lookup_parser = subparsers.add_parser(
        "lookup",
        help="exact-match lookup by field",
    )
    lookup_parser.add_argument("query")
    lookup_parser.add_argument(
        "--field", required=True, help="field to match, e.g. email"
    )
    lookup_parser.add_argument(
        "--entities",
        required=True,
        help="comma-separated entities to search",
    )
    return parser


def _dispatch(args: argparse.Namespace, client: FreshsalesClient) -> Any:
    if (
        getattr(args, "write", False)
        and args.action in ("create", "update")
        and args.resource in records.RECORD_SPECS
    ):
        return _dispatch_record_write(args, client)
    if args.resource == "contacts":
        if args.action == "set-linkedin":
            return _dispatch_contact_write(args, client)
        if args.action == "get":
            return client.get_contact(args.id, include=args.include)
        if args.action == "filters":
            return client.contact_filters()
        if args.action == "activities":
            return client.contact_activities(
                args.id, include=args.include, limit=args.limit
            )
        return client.list_contacts(
            args.view_id,
            page=args.page,
            sort=args.sort,
            sort_type=args.sort_type,
            include=args.include,
        )

    if args.resource == "accounts":
        if args.action == "get":
            return client.get_account(args.id, include=args.include)
        if args.action == "filters":
            return client.account_filters()
        return client.list_accounts(
            args.view_id,
            page=args.page,
            sort=args.sort,
            sort_type=args.sort_type,
            include=args.include,
        )

    if args.resource == "deals":
        if args.action == "get":
            return client.get_deal(args.id, include=args.include)
        if args.action == "filters":
            return client.deal_filters()
        return client.list_deals(
            args.view_id,
            page=args.page,
            sort=args.sort,
            sort_type=args.sort_type,
            include=args.include,
        )

    if args.resource == "tasks":
        if args.action == "get":
            return client.get_task(args.id, include=args.include)
        return client.list_tasks(
            filter_name=args.filter_name, page=args.page, include=args.include
        )

    if args.resource == "appointments":
        if args.action == "get":
            return client.get_appointment(args.id, include=args.include)
        return client.list_appointments(
            filter_name=args.filter_name, page=args.page, include=args.include
        )

    if args.resource == "campaigns":
        if args.action == "list":
            return client.campaigns()
        audience = analytics.Audience(list_id=args.list_id, view_id=args.view_id)
        limits = analytics.Limits(
            max_pages=args.max_pages,
            max_contacts=args.max_contacts,
            activity_limit=args.activity_limit,
            delay=args.delay,
        )
        if args.action == "activity-types":
            return analytics.activity_type_census(client, audience, limits)
        contact_filters = [analytics.FieldMatch.parse(item) for item in args.match]
        if args.title:
            contact_filters.insert(
                0, analytics.FieldMatch.parse(f"job_title={args.title}")
            )
        filters = analytics.EngagementFilters(
            action_pattern=args.action_type,
            campaign=args.campaign,
            since=args.since,
            contact_filters=tuple(contact_filters),
        )
        report = analytics.campaign_engagement(client, audience, filters, limits)
        if args.format == "csv":
            return RawText(analytics.engagement_csv(report))
        return report

    if args.resource == "lists":
        if getattr(args, "write", False):
            return _dispatch_list_write(args, client)
        if args.action == "list":
            return client.marketing_lists(page=args.page)
        return client.marketing_list_contacts(args.id, page=args.page)

    if args.resource == "fields":
        return client.fields(args.entity, include=args.include)

    if args.resource == "selector":
        return client.selector(args.name)

    if args.resource == "search":
        return client.search(
            args.query,
            include=args.include,
            per_page=args.per_page,
        )

    if args.resource == "lookup":
        return client.lookup(
            args.query,
            field=args.field,
            entities=args.entities,
        )

    raise ValueError(f"unknown resource: {args.resource}")


def _dispatch_record_write(
    args: argparse.Namespace, client: FreshsalesClient | None
) -> dict[str, Any]:
    base_url = client.base_url if client is not None else args.base_url.rstrip("/")
    values = records.parse_assignments(args.assignments)
    tags = getattr(args, "tag", None)
    if args.action == "create":
        request = records.plan_create(args.resource, values, tags)

        def preflight(c: Any) -> dict[str, Any]:
            return records.check_create_references(c, request)

    else:
        request = records.plan_update(args.resource, args.id, values, tags)

        def preflight(c: Any) -> dict[str, Any]:
            return records.check_update_target(c, args.resource, args.id, request)

    return writes.run(
        client, [request], base_url=base_url, apply=args.apply, preflight=preflight
    )


def _dispatch_contact_write(
    args: argparse.Namespace, client: FreshsalesClient | None
) -> dict[str, Any]:
    base_url = client.base_url if client is not None else args.base_url.rstrip("/")
    rows = writes.read_linkedin_updates(args.csv)
    requests = writes.plan_set_linkedin(rows)

    def preflight(c: Any) -> dict[str, Any]:
        return writes.verify_linkedin_unchanged(c, rows)

    return writes.run(
        client, requests, base_url=base_url, apply=args.apply, preflight=preflight
    )


def _dispatch_list_write(
    args: argparse.Namespace, client: FreshsalesClient | None
) -> dict[str, Any]:
    base_url = client.base_url if client is not None else args.base_url.rstrip("/")
    preflight: Callable[[Any], dict[str, Any] | None] | None

    if args.action == "create":
        requests = [FreshsalesClient.plan_create_list(args.name)]

        def preflight(c: Any) -> None:
            if not args.allow_duplicate_name:
                writes.ensure_name_is_free(c, args.name)

    elif args.action == "rename":
        requests = [FreshsalesClient.plan_rename_list(args.id, args.name)]

        def preflight(c: Any) -> dict[str, Any]:
            return writes.resolve_lists(c, {"list": args.id})

    else:
        ids = writes.read_contact_ids(args.ids, args.ids_file)
        roles = {"list": args.id}
        if args.action == "add-contacts":
            requests = writes.plan_add(args.id, ids, batch_size=args.batch_size)
        elif args.action == "remove-contacts":
            requests = writes.plan_remove(
                args.id,
                ids,
                remove_all=args.all_contacts,
                confirm=args.confirm,
                batch_size=args.batch_size,
            )
        else:
            requests = writes.plan_move(
                args.id,
                args.from_list,
                ids,
                move_all=args.all_contacts,
                confirm=args.confirm,
                batch_size=args.batch_size,
            )
            roles["from_list"] = args.from_list

        def preflight(c: Any) -> dict[str, Any]:
            return writes.resolve_lists(c, roles)

    return writes.run(
        client, requests, base_url=base_url, apply=args.apply, preflight=preflight
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client: FreshsalesClient | None = None,
    api_key_loader: Callable[[], str] = load_api_key,
) -> int:
    """Run the CLI and return a process-style exit code.

    Supplying ``client`` is intended for unit tests. Normal invocation loads
    the API key from Keychain and creates a client with writes disabled. The
    only exception is a write command run with ``--apply``; a write command
    without ``--apply`` is a dry run that needs neither the key nor a client.
    """

    args = build_parser().parse_args(argv)
    is_write = getattr(args, "write", False)
    apply = is_write and getattr(args, "apply", False)
    if client is None and not args.base_url:
        print(
            f"error: set {BASE_URL_ENV} or pass --base-url with your "
            "Freshsales API base URL",
            file=sys.stderr,
        )
        return 1
    try:
        if client is None and (apply or not is_write):
            client = FreshsalesClient(
                api_key_loader(),
                base_url=args.base_url,
                timeout=args.timeout,
                allow_writes=apply,
            )
        result = _dispatch(args, client)
        if isinstance(result, RawText):
            sys.stdout.write(result)
            return 0
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2 if args.pretty else None,
            )
        )
        return 0
    except PartialWriteError as exc:
        print(
            json.dumps(
                exc.report, ensure_ascii=False, indent=2 if args.pretty else None
            )
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (FreshsalesError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
