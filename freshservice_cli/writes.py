"""Safety layer for the Freshservice ticket write commands.

The commands are ``tickets create``, ``tickets update`` and
``tickets add-note``. Each one changes a single ticket and follows the same
flow as the Freshsales write commands:

1. **Plan** – validate the input and build a :class:`WriteRequest` offline.
2. **Dry run (default)** – print the planned request and stop. No API key is
   loaded and no network call is made.
3. **Apply (``--apply``)** – read the target ticket with a GET (for updates
   and notes) and refuse to continue if it is missing, then send the request.

There is deliberately no delete command. Deleting is only done by the opt-in
live test harness in ``tests_live/``, and only for tickets it created.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import FreshserviceError

TICKET_STATUSES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
TICKET_PRIORITIES = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
MAX_SUBJECT_LENGTH = 255
MAX_TAG_LENGTH = 32
MAX_TAGS = 10
MAX_TEXT_LENGTH = 20000


@dataclass(frozen=True)
class WriteRequest:
    """A planned mutating request, built without touching the network.

    Dry runs print these, and ``--apply`` sends the same objects, so what is
    previewed is exactly what is sent.
    """

    method: str
    path: str
    body: Mapping[str, Any]
    summary: str

    def describe(self, base_url: str) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": f"{base_url.rstrip('/')}{self.path}",
            "body": dict(self.body),
            "summary": self.summary,
        }


# Validation -----------------------------------------------------------------
def _positive_id(value: int | str, label: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"{label} must be a positive integer: {value!r}")
    return int(text)


def _text(value: str, label: str, max_length: int) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return value


def _choice(value: int | str, choices: Mapping[int, str], label: str) -> int:
    number = _positive_id(value, label)
    if number not in choices:
        allowed = ", ".join(f"{key} ({name})" for key, name in choices.items())
        raise ValueError(f"{label} must be one of: {allowed}")
    return number


def _tags(tags: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        tag = _text(tag, "tag", MAX_TAG_LENGTH)
        if tag not in cleaned:
            cleaned.append(tag)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags are allowed")
    return cleaned


# Planning -------------------------------------------------------------------
def plan_create_ticket(
    *,
    subject: str,
    description: str,
    workspace_id: int | str,
    requester_id: int | str | None = None,
    email: str | None = None,
    priority: int | str = 1,
    status: int | str = 2,
    tags: Sequence[str] = (),
) -> WriteRequest:
    if (requester_id is None) == (email is None):
        raise ValueError("pass exactly one of requester id or email")
    body: dict[str, Any] = {
        "subject": _text(subject, "subject", MAX_SUBJECT_LENGTH),
        "description": _text(description, "description", MAX_TEXT_LENGTH),
        "workspace_id": _positive_id(workspace_id, "workspace id"),
        "priority": _choice(priority, TICKET_PRIORITIES, "priority"),
        "status": _choice(status, TICKET_STATUSES, "status"),
    }
    if requester_id is not None:
        body["requester_id"] = _positive_id(requester_id, "requester id")
    else:
        body["email"] = _text(str(email), "email", 254)
    if tags:
        body["tags"] = _tags(tags)
    return WriteRequest("POST", "/tickets", body, f"create ticket {body['subject']!r}")


def plan_update_ticket(
    ticket_id: int | str,
    *,
    subject: str | None = None,
    status: int | str | None = None,
    priority: int | str | None = None,
    tags: Sequence[str] | None = None,
) -> WriteRequest:
    target = _positive_id(ticket_id, "ticket id")
    body: dict[str, Any] = {}
    if subject is not None:
        body["subject"] = _text(subject, "subject", MAX_SUBJECT_LENGTH)
    if status is not None:
        body["status"] = _choice(status, TICKET_STATUSES, "status")
    if priority is not None:
        body["priority"] = _choice(priority, TICKET_PRIORITIES, "priority")
    if tags is not None:
        body["tags"] = _tags(tags)
    if not body:
        raise ValueError("nothing to update; pass at least one field to change")
    return WriteRequest(
        "PUT",
        f"/tickets/{target}",
        body,
        f"update {', '.join(body)} of ticket {target}",
    )


def plan_add_note(
    ticket_id: int | str, text: str, *, private: bool = True
) -> WriteRequest:
    target = _positive_id(ticket_id, "ticket id")
    body = {"body": _text(text, "note", MAX_TEXT_LENGTH), "private": private}
    kind = "private" if private else "public"
    return WriteRequest(
        "POST",
        f"/tickets/{target}/notes",
        body,
        f"add a {kind} note to ticket {target}",
    )


# Preflight ------------------------------------------------------------------
def ticket_summary(client: Any, ticket_id: int | str) -> dict[str, Any]:
    """GET the ticket and return its id, subject, status, priority and tags.

    Raises if the ticket does not exist, so nothing is sent to a wrong ID.
    Tags are included because ``update --tag`` replaces the whole list; the
    API only returns them when asked for.
    """

    payload = client.get_ticket(ticket_id, include="tags") or {}
    ticket = payload.get("ticket") if isinstance(payload, Mapping) else None
    if not isinstance(ticket, Mapping):
        raise FreshserviceError(f"ticket {ticket_id} was not found")
    return {
        key: ticket.get(key) for key in ("id", "subject", "status", "priority", "tags")
    }


# Execution ------------------------------------------------------------------
def run(
    client: Any,
    request: WriteRequest,
    *,
    base_url: str,
    apply: bool,
    preflight: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dry-run or apply one request and return a JSON-ready report."""

    report: dict[str, Any] = {
        "dry_run": not apply,
        "requests": [request.describe(base_url)],
    }
    if not apply:
        report["next_step"] = "re-run with --apply to send this request"
        return report
    if client is None:
        raise ValueError("a client is required to apply writes")
    if preflight is not None:
        report["target"] = preflight(client)
    report["results"] = [{"request": 0, "response": client.send(request)}]
    return report
