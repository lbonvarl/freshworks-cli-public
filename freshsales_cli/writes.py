"""Safety layer for the Freshsales write commands.

These are the marketing-list commands and ``contacts set-linkedin``, which
changes only the ``linkedin`` field of the contacts in a reviewed CSV.

Every write command follows the same flow:

1. **Plan** – validate the input and build :class:`WriteRequest` objects
   offline. Large ID sets are split into batches; a hard per-command cap
   stops runaway input.
2. **Dry run (default)** – print the planned requests and stop. No API key
   is loaded and no network call is made.
3. **Apply (``--apply``)** – resolve the target lists with GET calls and
   refuse to continue if one is missing (or, for ``create``, if a list with
   the same name already exists), then send the batches one by one. If a
   batch fails, the report of what was already sent is printed before the
   error so nothing happens silently.

Destructive "all contacts" operations additionally require ``--confirm``
with the ID of the list being emptied.
"""

from __future__ import annotations

import csv
import io
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, TextIO

from .client import FreshsalesClient, WriteRequest
from .errors import FreshsalesError, PartialWriteError

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
MAX_IDS_PER_COMMAND = 1000
MAX_LIST_LOOKUP_PAGES = 20
# Each contact update is one PUT plus one preflight GET.
MAX_CONTACT_UPDATES = 200


class WriteClient(Protocol):
    base_url: str

    def marketing_lists(self, *, page: int = 1) -> Any: ...

    def send(self, request: WriteRequest) -> Any: ...


# Input ----------------------------------------------------------------------
def read_contact_ids(
    ids: str | None,
    ids_file: str | None,
    *,
    stdin: TextIO | None = None,
    opener: Callable[[str], TextIO] | None = None,
) -> list[str]:
    """Collect contact IDs from ``--ids`` and/or ``--ids-file``.

    ``--ids-file`` accepts one ID per line (``#`` comments allowed) or a CSV
    with an ``id`` column, such as ``campaigns engagement --format csv``
    output. ``-`` reads standard input.
    """

    collected: list[str] = []
    if ids:
        collected.extend(item.strip() for item in ids.split(",") if item.strip())
    if ids_file:
        if ids_file == "-":
            text = (stdin or sys.stdin).read()
        else:
            try:
                if opener is not None:
                    with opener(ids_file) as handle:
                        text = handle.read()
                else:
                    with open(ids_file, encoding="utf-8") as handle:
                        text = handle.read()
            except OSError as exc:
                raise ValueError(f"could not read ids file: {exc.strerror}") from exc
        collected.extend(_parse_ids_text(text))
    return collected


def _parse_ids_text(text: str) -> list[str]:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []
    if "," in lines[0] or not lines[0].isdigit():
        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        fields = [name.strip().lower() for name in reader.fieldnames or []]
        if "id" not in fields:
            raise ValueError("ids file CSV must have an 'id' column")
        key = (reader.fieldnames or [])[fields.index("id")]
        return [str(row.get(key) or "").strip() for row in reader]
    return lines


# Planning -------------------------------------------------------------------
def _checked_ids(contact_ids: Sequence[str]) -> list[int]:
    ids = FreshsalesClient._contact_ids(contact_ids)
    if len(ids) > MAX_IDS_PER_COMMAND:
        raise ValueError(
            f"{len(ids)} contact ids exceeds the per-command cap of "
            f"{MAX_IDS_PER_COMMAND}; split the input into smaller runs"
        )
    return ids


def _batches(ids: Sequence[int], batch_size: int) -> list[list[int]]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    return [list(ids[i : i + batch_size]) for i in range(0, len(ids), batch_size)]


def _require_confirmation(confirm: str | None, list_id: int | str) -> None:
    if confirm is None or str(confirm).strip() != str(list_id).strip():
        raise ValueError(
            f"this empties list {list_id}; re-run with --confirm {list_id} "
            "to acknowledge"
        )


def plan_add(
    list_id: str, contact_ids: Sequence[str], *, batch_size: int
) -> list[WriteRequest]:
    ids = _checked_ids(contact_ids)
    return [
        FreshsalesClient.plan_add_contacts(list_id, batch)
        for batch in _batches(ids, batch_size)
    ]


def plan_remove(
    list_id: str,
    contact_ids: Sequence[str],
    *,
    remove_all: bool,
    confirm: str | None,
    batch_size: int,
) -> list[WriteRequest]:
    if remove_all:
        if contact_ids:
            raise ValueError("use either contact ids or --all, not both")
        _require_confirmation(confirm, list_id)
        return [FreshsalesClient.plan_remove_contacts(list_id, remove_all=True)]
    ids = _checked_ids(contact_ids)
    return [
        FreshsalesClient.plan_remove_contacts(list_id, batch)
        for batch in _batches(ids, batch_size)
    ]


def plan_move(
    list_id: str,
    from_list_id: str,
    contact_ids: Sequence[str],
    *,
    move_all: bool,
    confirm: str | None,
    batch_size: int,
) -> list[WriteRequest]:
    if move_all:
        if contact_ids:
            raise ValueError("use either contact ids or --all, not both")
        _require_confirmation(confirm, from_list_id)
        return [
            FreshsalesClient.plan_move_contacts(list_id, from_list_id, move_all=True)
        ]
    ids = _checked_ids(contact_ids)
    return [
        FreshsalesClient.plan_move_contacts(list_id, from_list_id, batch)
        for batch in _batches(ids, batch_size)
    ]


# Preflight ------------------------------------------------------------------
def fetch_lists(client: WriteClient) -> list[Mapping[str, Any]]:
    lists: list[Mapping[str, Any]] = []
    for page in range(1, MAX_LIST_LOOKUP_PAGES + 1):
        payload = client.marketing_lists(page=page) or {}
        items = [
            item for item in payload.get("lists") or [] if isinstance(item, Mapping)
        ]
        lists.extend(items)
        meta = payload.get("meta")
        total = meta.get("total_pages") if isinstance(meta, Mapping) else None
        if not items or total is None or page >= int(total):
            return lists
    raise FreshsalesError(
        f"more than {MAX_LIST_LOOKUP_PAGES} pages of marketing lists; "
        "refusing to guess whether the target list exists"
    )


def _list_label(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": item.get("id"), "name": item.get("name") or item.get("list_name")}


def resolve_lists(
    client: WriteClient, list_ids: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Return ``{role: {"id", "name"}}`` or raise if any list is missing."""

    by_id = {str(item.get("id")): item for item in fetch_lists(client)}
    resolved: dict[str, dict[str, Any]] = {}
    for role, list_id in list_ids.items():
        item = by_id.get(str(int(str(list_id).strip())))
        if item is None:
            raise ValueError(f"{role.replace('_', ' ')} {list_id} was not found")
        resolved[role] = _list_label(item)
    return resolved


def ensure_name_is_free(client: WriteClient, name: str) -> None:
    wanted = name.strip().casefold()
    for item in fetch_lists(client):
        label = _list_label(item)
        if str(label["name"] or "").strip().casefold() == wanted:
            raise ValueError(
                f"a marketing list named {label['name']!r} already exists "
                f"(id {label['id']}); pass --allow-duplicate-name to create anyway"
            )


# Contact LinkedIn updates ---------------------------------------------------
LINKEDIN_COLUMNS = ("contact_id", "current_linkedin", "suggested_linkedin")


def read_linkedin_updates(
    path: str,
    *,
    stdin: TextIO | None = None,
    opener: Callable[[str], TextIO] | None = None,
) -> list[dict[str, str]]:
    """Read ``contact_id,current_linkedin,suggested_linkedin`` rows from a CSV.

    ``current_linkedin`` is the value the reviewer saw; the apply step refuses
    to run if Freshsales no longer holds it. Rows with an empty
    ``suggested_linkedin`` are skipped, so a review table can be passed as is.
    """

    if path == "-":
        text = (stdin or sys.stdin).read()
    else:
        try:
            if opener is not None:
                with opener(path) as handle:
                    text = handle.read()
            else:
                with open(path, encoding="utf-8-sig") as handle:
                    text = handle.read()
        except OSError as exc:
            raise ValueError(f"could not read csv file: {exc.strerror}") from exc

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [
        name for name in LINKEDIN_COLUMNS if name not in (reader.fieldnames or [])
    ]
    if missing:
        raise ValueError(f"csv must have columns {', '.join(LINKEDIN_COLUMNS)}")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in reader:
        suggested = (row.get("suggested_linkedin") or "").strip()
        if not suggested:
            continue
        contact_id = (row.get("contact_id") or "").strip()
        if contact_id in seen:
            raise ValueError(f"contact {contact_id} appears more than once in the csv")
        seen.add(contact_id)
        rows.append(
            {
                "contact_id": contact_id,
                "current_linkedin": (row.get("current_linkedin") or "").strip(),
                "suggested_linkedin": suggested,
            }
        )
    if not rows:
        raise ValueError("the csv has no rows with a suggested_linkedin value")
    if len(rows) > MAX_CONTACT_UPDATES:
        raise ValueError(
            f"{len(rows)} contact updates exceeds the per-command cap of "
            f"{MAX_CONTACT_UPDATES}; split the csv into smaller runs"
        )
    return rows


def plan_set_linkedin(rows: Sequence[Mapping[str, str]]) -> list[WriteRequest]:
    return [
        FreshsalesClient.plan_set_contact_linkedin(
            row["contact_id"], row["suggested_linkedin"]
        )
        for row in rows
    ]


def verify_linkedin_unchanged(
    client: Any, rows: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """GET every contact and refuse if its linkedin differs from the review.

    All contacts are checked before anything is sent, so a stale review table
    blocks the whole run instead of half-applying it.
    """

    changed: list[str] = []
    for row in rows:
        payload = client.get_contact(row["contact_id"]) or {}
        contact = payload.get("contact") if isinstance(payload, Mapping) else None
        if not isinstance(contact, Mapping):
            raise FreshsalesError(f"contact {row['contact_id']} was not found")
        current = str(contact.get("linkedin") or "").strip()
        if current != row["current_linkedin"]:
            changed.append(row["contact_id"])
    if changed:
        raise ValueError(
            "linkedin changed since the review for contact(s) "
            f"{', '.join(changed)}; re-export the review table and try again"
        )
    return {"contacts_checked": len(rows)}


# Execution ------------------------------------------------------------------
def run(
    client: WriteClient | None,
    requests: Sequence[WriteRequest],
    *,
    base_url: str,
    apply: bool,
    preflight: Callable[[WriteClient], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Dry-run or apply ``requests`` and return a JSON-ready report."""

    report: dict[str, Any] = {
        "dry_run": not apply,
        "requests": [request.describe(base_url) for request in requests],
    }
    if not apply:
        report["next_step"] = "re-run with --apply to send these requests"
        return report
    if client is None:
        raise ValueError("a client is required to apply writes")

    if preflight is not None:
        targets = preflight(client)
        if targets:
            report["targets"] = targets

    report["results"] = []
    for index, request in enumerate(requests):
        try:
            response = client.send(request)
        except FreshsalesError as exc:
            report["failed_request"] = index
            raise PartialWriteError(
                f"request {index + 1} of {len(requests)} failed "
                f"({len(report['results'])} already applied): {exc}",
                report,
            ) from exc
        report["results"].append({"request": index, "response": response})
    return report
