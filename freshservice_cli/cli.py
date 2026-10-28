"""Command-line interface for the read-only Freshservice client."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__, writes
from .auth import load_api_key
from .client import (
    ANNOUNCEMENT_STATES,
    BASE_URL_ENV,
    DEFAULT_TIMEOUT,
    FreshserviceClient,
)
from .errors import FreshserviceError

# Ticket sub-resource commands: (action, help, paginated).
TICKET_SUB_RESOURCES = (
    ("conversations", "list a ticket's replies and notes", True),
    ("tasks", "list a ticket's tasks", True),
    ("approvals", "list a ticket's approvals", False),
    ("requested-items", "list the catalog items of a service request", False),
)

# Plain get|list resources: command -> (singular, get method, list method).
# Their list commands take only --page/--per-page.
PAGED_RESOURCES = {
    "groups": ("agent group", "get_group", "list_groups"),
    "workspaces": ("workspace", "get_workspace", "list_workspaces"),
    "roles": ("role", "get_role", "list_roles"),
    "announcements": ("announcement", "get_announcement", "list_announcements"),
    "canned-responses": (
        "canned response",
        "get_canned_response",
        "list_canned_responses",
    ),
}


def _add_pagination(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--page", type=int, default=1, help="page number (default: 1)")
    parser.add_argument(
        "--per-page",
        type=int,
        default=30,
        help="results per page, from 1 to 100 (default: 30)",
    )


def _add_ticket_list_options(parser: argparse.ArgumentParser) -> None:
    _add_pagination(parser)
    parser.add_argument(
        "--include",
        help="comma-separated related resources to embed (for example requester)",
    )
    parser.add_argument(
        "--order-type",
        choices=("asc", "desc"),
        help="sort direction (default is API-defined)",
    )
    parser.add_argument("--filter", dest="filter_name", help="saved filter name")
    parser.add_argument("--requester-id", help="filter by requester ID")
    parser.add_argument("--email", help="filter by requester email")
    parser.add_argument(
        "--updated-since", help="only tickets updated since an ISO timestamp"
    )
    parser.add_argument(
        "--type",
        dest="ticket_type",
        choices=("Incident", "Service Request"),
        help="filter by ticket type",
    )
    parser.add_argument("--workspace-id", help="filter by workspace ID")


def _add_requester_list_options(parser: argparse.ArgumentParser) -> None:
    _add_pagination(parser)
    parser.add_argument("--email", help="filter by email")
    parser.add_argument("--mobile-phone-number", help="filter by mobile")
    parser.add_argument("--work-phone-number", help="filter by work phone")
    parser.add_argument(
        "--include-agents", action="store_true", help="include agents in the results"
    )


def _add_entity_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    resource: str,
    singular: str,
    list_options: Callable[[argparse.ArgumentParser], None] | None = None,
    aliases: tuple[str, ...] = (),
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    parser = subparsers.add_parser(
        resource,
        aliases=aliases,
        help=f"read {resource} from Freshservice",
        description=f"Read {resource} from Freshservice.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    get_parser = actions.add_parser("get", help=f"get one {singular}")
    get_parser.add_argument("id", help=f"{singular} ID")
    if resource == "tickets":
        get_parser.add_argument(
            "--include",
            help="comma-separated related resources to embed",
        )

    list_parser = actions.add_parser("list", help=f"list {resource}")
    if list_options is None:
        _add_pagination(list_parser)
        list_parser.add_argument("--query", help="optional API query")
        list_parser.add_argument("--workspace-id", help="filter by workspace ID")
    else:
        list_options(list_parser)
    return actions


WRITE_NOTE = (
    "WRITE COMMAND: dry run by default - prints the planned request without "
    "loading the API key or calling Freshservice. Add --apply to check the "
    "ticket exists (for update and add-note) and send the request."
)


def _add_write_parser(
    actions: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = actions.add_parser(
        name,
        help=f"{help_text} (write; dry run unless --apply)",
        description=WRITE_NOTE,
    )
    parser.set_defaults(write=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually send the request; without it the command is a dry run",
    )
    return parser


def _status_help() -> str:
    return ", ".join(f"{key}={name}" for key, name in writes.TICKET_STATUSES.items())


def _priority_help() -> str:
    return ", ".join(f"{key}={name}" for key, name in writes.TICKET_PRIORITIES.items())


def _add_ticket_write_commands(
    actions: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    create = _add_write_parser(actions, "create", "create a ticket")
    create.add_argument("--subject", required=True)
    create.add_argument("--description", required=True, help="HTML or plain text")
    create.add_argument("--workspace-id", required=True, help="workspace ID")
    requester = create.add_mutually_exclusive_group(required=True)
    requester.add_argument("--requester-id", help="requester (or agent) ID")
    requester.add_argument(
        "--email", help="requester email; Freshservice may notify this address"
    )
    create.add_argument(
        "--priority", default="1", help=f"{_priority_help()} (default: 1)"
    )
    create.add_argument("--status", default="2", help=f"{_status_help()} (default: 2)")
    create.add_argument(
        "--tag", action="append", default=[], help="ticket tag; repeatable"
    )

    update = _add_write_parser(actions, "update", "update one ticket")
    update.add_argument("id", help="ticket ID")
    update.add_argument("--subject")
    update.add_argument("--status", help=_status_help())
    update.add_argument("--priority", help=_priority_help())
    update.add_argument(
        "--tag",
        action="append",
        help="ticket tag; repeatable; replaces the ticket's whole tag list",
    )

    note = _add_write_parser(actions, "add-note", "add a note to a ticket")
    note.add_argument("id", help="ticket ID")
    note.add_argument("--text", required=True, help="note body, HTML or plain text")
    note.add_argument(
        "--public",
        action="store_true",
        help="make the note visible to the requester (default: private)",
    )


def _dispatch_ticket_write(
    args: argparse.Namespace, client: FreshserviceClient | None
) -> dict[str, Any]:
    base_url = client.base_url if client is not None else args.base_url.rstrip("/")
    preflight: Callable[[Any], dict[str, Any]] | None = None
    if args.action == "create":
        request = writes.plan_create_ticket(
            subject=args.subject,
            description=args.description,
            workspace_id=args.workspace_id,
            requester_id=args.requester_id,
            email=args.email,
            priority=args.priority,
            status=args.status,
            tags=args.tag,
        )
    else:
        if args.action == "update":
            request = writes.plan_update_ticket(
                args.id,
                subject=args.subject,
                status=args.status,
                priority=args.priority,
                tags=args.tag,
            )
        else:
            request = writes.plan_add_note(args.id, args.text, private=not args.public)

        def preflight(c: Any) -> dict[str, Any]:
            return writes.ticket_summary(c, args.id)

    return writes.run(
        client, request, base_url=base_url, apply=args.apply, preflight=preflight
    )


def _add_announcement_list_options(parser: argparse.ArgumentParser) -> None:
    _add_pagination(parser)
    parser.add_argument(
        "--state", choices=ANNOUNCEMENT_STATES, help="filter by announcement state"
    )


def _add_solution_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "solutions",
        help="read knowledge-base categories, folders and articles",
        description="Read Freshservice solution (knowledge base) content.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    categories = actions.add_parser("categories", help="list solution categories")
    _add_pagination(categories)
    categories.add_argument("--workspace-id", help="filter by workspace ID")

    folders = actions.add_parser("folders", help="list the folders of a category")
    folders.add_argument("--category-id", required=True, help="category ID")
    _add_pagination(folders)

    articles = actions.add_parser("articles", help="list the articles of a folder")
    articles.add_argument("--folder-id", required=True, help="folder ID")
    _add_pagination(articles)

    for action in ("category", "folder", "article"):
        detail = actions.add_parser(action, help=f"get one solution {action}")
        detail.add_argument("id", help=f"{action} ID")

    search = actions.add_parser("search", help="search articles by keyword")
    search.add_argument("term", help="search term")
    _add_pagination(search)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freshservice",
        description="Small, read-only-first Freshservice service desk CLI.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(BASE_URL_ENV) or None,
        help=(
            f"Freshservice API base URL, e.g. https://<domain>.freshservice.com/api/v2; "
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

    ticket_actions = _add_entity_commands(
        subparsers,
        resource="tickets",
        singular="ticket",
        list_options=_add_ticket_list_options,
    )
    _add_ticket_write_commands(ticket_actions)
    for action, help_text, paginated in TICKET_SUB_RESOURCES:
        sub_parser = ticket_actions.add_parser(action, help=help_text)
        sub_parser.add_argument("id", help="ticket ID")
        if paginated:
            _add_pagination(sub_parser)

    _add_entity_commands(
        subparsers,
        resource="requesters",
        singular="requester/contact",
        list_options=_add_requester_list_options,
        aliases=("contacts",),
    )

    agent_actions = _add_entity_commands(
        subparsers,
        resource="agents",
        singular="agent",
    )
    agent_actions.add_parser(
        "me",
        help="get the agent that owns the API key",
        description=(
            "Read the agent that owns the API key. Unlike `agents get|list`, "
            "this does not need an agent-admin role."
        ),
    )
    _add_entity_commands(
        subparsers,
        resource="departments",
        singular="department",
        aliases=("companies",),
    )

    filter_parser = subparsers.add_parser(
        "filter-tickets",
        help="filter tickets using the Freshservice query language",
        description="Read tickets matching a Freshservice filter query.",
    )
    filter_parser.add_argument("query")
    _add_pagination(filter_parser)
    filter_parser.add_argument("--workspace-id", help="filter by workspace ID")

    for resource, (singular, _, _) in PAGED_RESOURCES.items():
        resource_actions = _add_entity_commands(
            subparsers,
            resource=resource,
            singular=singular,
            list_options=(
                _add_announcement_list_options
                if resource == "announcements"
                else _add_pagination
            ),
        )
        if resource == "canned-responses":
            resource_actions.add_parser("folders", help="list canned response folders")

    requester_groups = subparsers.add_parser(
        "requester-groups",
        help="read requester groups from Freshservice",
        description=(
            "List requester groups. Group detail and members need an admin "
            "role, so only the list is exposed."
        ),
    )
    requester_group_actions = requester_groups.add_subparsers(
        dest="action", required=True
    )
    _add_pagination(
        requester_group_actions.add_parser("list", help="list requester groups")
    )

    _add_solution_commands(subparsers)

    ticket_fields_parser = subparsers.add_parser(
        "ticket-fields",
        help="read ticket form fields: labels, types and choice IDs",
        description=(
            "Read the ticket form fields, including the status, priority "
            "and custom field choices used in ticket records."
        ),
    )
    ticket_fields_parser.add_argument("--workspace-id", help="workspace ID")

    return parser


def _dispatch(args: argparse.Namespace, client: FreshserviceClient) -> Any:
    if args.resource == "tickets":
        if getattr(args, "write", False):
            return _dispatch_ticket_write(args, client)
        if args.action == "get":
            return client.get_ticket(args.id, include=args.include)
        if args.action == "list":
            return client.list_tickets(
                page=args.page,
                per_page=args.per_page,
                include=args.include,
                order_type=args.order_type,
                filter_name=args.filter_name,
                requester_id=args.requester_id,
                email=args.email,
                updated_since=args.updated_since,
                ticket_type=args.ticket_type,
                workspace_id=args.workspace_id,
            )
        if args.action == "approvals":
            return client.ticket_approvals(args.id)
        if args.action == "requested-items":
            return client.ticket_requested_items(args.id)
        if args.action == "conversations":
            return client.ticket_conversations(
                args.id, page=args.page, per_page=args.per_page
            )
        if args.action == "tasks":
            return client.ticket_tasks(args.id, page=args.page, per_page=args.per_page)

    if args.resource in {"requesters", "contacts"}:
        if args.action == "get":
            return client.get_requester(args.id)
        return client.list_requesters(
            page=args.page,
            per_page=args.per_page,
            email=args.email,
            mobile_phone_number=args.mobile_phone_number,
            work_phone_number=args.work_phone_number,
            include_agents=args.include_agents,
        )

    if args.resource == "agents":
        if args.action == "me":
            return client.get_current_agent()
        if args.action == "get":
            return client.get_agent(args.id)
        return client.list_agents(
            page=args.page,
            per_page=args.per_page,
            query=args.query,
            workspace_id=args.workspace_id,
        )

    if args.resource in {"departments", "companies"}:
        if args.action == "get":
            return client.get_department(args.id)
        return client.list_departments(
            page=args.page,
            per_page=args.per_page,
            query=args.query,
            workspace_id=args.workspace_id,
        )

    if args.resource == "filter-tickets":
        return client.filter_tickets(
            args.query,
            page=args.page,
            per_page=args.per_page,
            workspace_id=args.workspace_id,
        )

    if args.resource in PAGED_RESOURCES:
        _, get_method, list_method = PAGED_RESOURCES[args.resource]
        if args.action == "get":
            return getattr(client, get_method)(args.id)
        if args.action == "folders":
            return client.list_canned_response_folders()
        options = {"page": args.page, "per_page": args.per_page}
        if args.resource == "announcements":
            options["state"] = args.state
        return getattr(client, list_method)(**options)

    if args.resource == "requester-groups":
        return client.list_requester_groups(page=args.page, per_page=args.per_page)

    if args.resource == "solutions":
        return _dispatch_solutions(args, client)

    if args.resource == "ticket-fields":
        return client.ticket_fields(workspace_id=args.workspace_id)

    raise ValueError(f"unknown resource: {args.resource}")


def _dispatch_solutions(args: argparse.Namespace, client: FreshserviceClient) -> Any:
    if args.action == "category":
        return client.get_solution_category(args.id)
    if args.action == "folder":
        return client.get_solution_folder(args.id)
    if args.action == "article":
        return client.get_solution_article(args.id)
    pagination = {"page": args.page, "per_page": args.per_page}
    if args.action == "categories":
        return client.list_solution_categories(
            workspace_id=args.workspace_id, **pagination
        )
    if args.action == "folders":
        return client.list_solution_folders(args.category_id, **pagination)
    if args.action == "articles":
        return client.list_solution_articles(args.folder_id, **pagination)
    return client.search_solution_articles(args.term, **pagination)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: FreshserviceClient | None = None,
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
            "Freshservice API base URL",
            file=sys.stderr,
        )
        return 1
    try:
        if client is None and (apply or not is_write):
            client = FreshserviceClient(
                api_key_loader(),
                base_url=args.base_url,
                timeout=args.timeout,
                allow_writes=apply,
            )
        result = _dispatch(args, client)
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2 if args.pretty else None,
            )
        )
        return 0
    except (FreshserviceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
