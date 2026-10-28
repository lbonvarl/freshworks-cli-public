"""Single-record writes: create and update contacts, accounts, deals, tasks
and notes.

Each command changes one record and follows the flow in ``writes.py``: the
request is planned offline, it is a dry run unless ``--apply`` is given, and
``--apply`` checks with GET calls that the record (for updates) and every
record it points to (for creates) exist before anything is sent.

Fields are set with ``--set FIELD=VALUE`` and must be in the entity's
allowlist below; values are converted to the type the API expects. Contacts,
accounts and deals also accept custom fields (``cf_*``) and ``--tag``.
Phone numbers and other fields that would let Freshsales contact someone
are deliberately not in the allowlists; ``email`` is, because contact forms
commonly require it. There is no delete command: deleting is only done
by the opt-in live test harness in ``tests_live/``, for records it created.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .client import WriteRequest
from .errors import ApiError, FreshsalesError

MAX_TEXT_LENGTH = 10000
MAX_TAGS = 10
MAX_TAG_LENGTH = 64
CUSTOM_FIELD_PATTERN = re.compile(r"cf_[a-z0-9_]{1,60}")
TARGETABLE_TYPES = ("Contact", "SalesAccount", "Deal")


@dataclass(frozen=True)
class RecordSpec:
    """How one entity is written: API route, JSON envelope and fields."""

    path: str
    envelope: str
    # field name -> value kind (see _convert)
    fields: Mapping[str, str]
    required: tuple[str, ...] = ()
    updatable: bool = True
    custom_fields: bool = False
    # fields that may be set on create but never changed by an update
    create_only: tuple[str, ...] = field(default=())


RECORD_SPECS: dict[str, RecordSpec] = {
    "contacts": RecordSpec(
        "/contacts",
        "contact",
        {
            "first_name": "text",
            "last_name": "text",
            "job_title": "text",
            "email": "email",
            "city": "text",
            "country": "text",
            "subscription_status": "int",
            "owner_id": "id",
        },
        custom_fields=True,
    ),
    "accounts": RecordSpec(
        "/sales_accounts",
        "sales_account",
        {
            "name": "text",
            "website": "text",
            "address": "text",
            "city": "text",
            "zipcode": "text",
            "country": "text",
            "industry_type_id": "id",
            "owner_id": "id",
        },
        required=("name",),
        custom_fields=True,
    ),
    "deals": RecordSpec(
        "/deals",
        "deal",
        {
            "name": "text",
            "amount": "number",
            "sales_account_id": "id",
            "deal_pipeline_id": "id",
            "deal_stage_id": "id",
            "expected_close": "date",
            "owner_id": "id",
        },
        required=("name",),
        custom_fields=True,
    ),
    "tasks": RecordSpec(
        "/tasks",
        "task",
        {
            "title": "text",
            "description": "text",
            "due_date": "datetime",
            "owner_id": "id",
            "status": "int",
            "targetable_type": "targetable",
            "targetable_id": "id",
        },
        required=("title", "due_date", "targetable_type", "targetable_id"),
        create_only=("targetable_type", "targetable_id"),
    ),
    "notes": RecordSpec(
        "/notes",
        "note",
        {
            "description": "text",
            "targetable_type": "targetable",
            "targetable_id": "id",
        },
        required=("description", "targetable_type", "targetable_id"),
        updatable=False,
    ),
}

# How preflight reads one record of each entity.
READERS = {
    "contacts": "get_contact",
    "accounts": "get_account",
    "deals": "get_deal",
    "tasks": "get_task",
}
TARGETABLE_READERS = {
    "Contact": "get_contact",
    "SalesAccount": "get_account",
    "Deal": "get_deal",
}
RECORD_KEYS = {
    "contacts": "contact",
    "accounts": "sales_account",
    "deals": "deal",
    "tasks": "task",
}


# Input ----------------------------------------------------------------------
def parse_assignments(items: Sequence[str]) -> dict[str, str]:
    """Turn repeated ``FIELD=VALUE`` arguments into a dict."""

    values: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError(f"expected FIELD=VALUE, got {item!r}")
        if name in values:
            raise ValueError(f"field {name!r} is set more than once")
        values[name] = value.strip()
    return values


def _spec(entity: str) -> RecordSpec:
    if entity not in RECORD_SPECS:
        raise ValueError(f"cannot write {entity!r} records")
    return RECORD_SPECS[entity]


def _convert(name: str, kind: str, value: str) -> Any:
    if kind in ("text", "email"):
        if not value:
            raise ValueError(f"{name} must not be empty")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"{name} must be at most {MAX_TEXT_LENGTH} characters")
        if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError(f"{name} must be an email address: {value!r}")
        return value
    if kind == "id":
        if not value.isdigit() or int(value) < 1:
            raise ValueError(f"{name} must be a positive integer: {value!r}")
        return int(value)
    if kind == "int":
        if not value.isdigit():
            raise ValueError(f"{name} must be a non-negative integer: {value!r}")
        return int(value)
    if kind == "number":
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"{name} must be a number: {value!r}") from None
        if not math.isfinite(number):
            raise ValueError(f"{name} must be a finite number: {value!r}")
        return int(number) if number.is_integer() else number
    if kind == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise ValueError(f"{name} must be a YYYY-MM-DD date: {value!r}") from None
    if kind == "datetime":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"{name} must be an ISO date or timestamp: {value!r}"
            ) from None
        return value
    if value not in TARGETABLE_TYPES:  # kind == "targetable"
        raise ValueError(f"{name} must be one of: {', '.join(TARGETABLE_TYPES)}")
    return value


def _custom_value(value: str) -> Any:
    # Checkbox custom fields take booleans; everything else is sent as text.
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _tags(tags: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            raise ValueError("tag must not be empty")
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tag must be at most {MAX_TAG_LENGTH} characters")
        if tag not in cleaned:
            cleaned.append(tag)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags are allowed")
    return cleaned


def _payload(
    entity: str,
    spec: RecordSpec,
    values: Mapping[str, str],
    tags: Sequence[str] | None,
    *,
    updating: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    for name, value in values.items():
        if name in spec.fields:
            if updating and name in spec.create_only:
                raise ValueError(f"{name} cannot be changed after the record exists")
            payload[name] = _convert(name, spec.fields[name], value)
        elif spec.custom_fields and CUSTOM_FIELD_PATTERN.fullmatch(name):
            custom[name] = _custom_value(value)
        else:
            allowed = ", ".join(spec.fields)
            extra = " and custom cf_* fields" if spec.custom_fields else ""
            raise ValueError(
                f"{entity} field {name!r} is not writable; allowed: {allowed}{extra}"
            )
    if custom:
        payload["custom_field"] = custom
    if tags is not None:
        if not spec.custom_fields:
            raise ValueError(f"{entity} have no tags")
        payload["tags"] = _tags(tags)
    return payload


# Planning -------------------------------------------------------------------
def plan_create(
    entity: str, values: Mapping[str, str], tags: Sequence[str] | None = None
) -> WriteRequest:
    spec = _spec(entity)
    payload = _payload(entity, spec, values, tags or None, updating=False)
    missing = [name for name in spec.required if name not in payload]
    if missing:
        raise ValueError(f"creating {entity} requires: {', '.join(missing)}")
    if not payload:
        raise ValueError("nothing to create; pass --set FIELD=VALUE")
    label = payload.get("name") or payload.get("title") or entity[:-1]
    return WriteRequest(
        "POST",
        spec.path,
        {spec.envelope: payload},
        f"create {entity[:-1]} {label!r}",
    )


def plan_update(
    entity: str,
    record_id: int | str,
    values: Mapping[str, str],
    tags: Sequence[str] | None = None,
) -> WriteRequest:
    spec = _spec(entity)
    if not spec.updatable:
        raise ValueError(f"{entity} cannot be updated")
    target = _convert("record id", "id", str(record_id).strip())
    payload = _payload(entity, spec, values, tags, updating=True)
    if not payload:
        raise ValueError("nothing to update; pass --set FIELD=VALUE or --tag")
    changed = sorted(payload.get("custom_field", {})) + [
        name for name in payload if name != "custom_field"
    ]
    return WriteRequest(
        "PUT",
        f"{spec.path}/{target}",
        {spec.envelope: payload},
        f"update {', '.join(changed)} of {entity[:-1]} {target}",
    )


# Preflight ------------------------------------------------------------------
def _read(client: Any, method: str, record_id: Any, label: str) -> Mapping[str, Any]:
    try:
        payload = getattr(client, method)(record_id) or {}
    except ApiError as exc:
        if exc.status_code == 404:
            raise FreshsalesError(f"{label} {record_id} was not found") from exc
        raise
    if not isinstance(payload, Mapping) or not payload:
        raise FreshsalesError(f"{label} {record_id} was not found")
    return payload


def check_update_target(
    client: Any, entity: str, record_id: int | str, request: WriteRequest
) -> dict[str, Any]:
    """GET the record and return the current value of every changed field."""

    payload = _read(client, READERS[entity], record_id, entity[:-1])
    record = payload.get(RECORD_KEYS[entity])
    if not isinstance(record, Mapping):
        raise FreshsalesError(f"{entity[:-1]} {record_id} was not found")
    changes = request.body[_spec(entity).envelope]
    current_custom = record.get("custom_field") or {}
    current: dict[str, Any] = {}
    for name in changes:
        if name == "custom_field":
            for custom_name in changes[name]:
                current[custom_name] = current_custom.get(custom_name)
        else:
            current[name] = record.get(name)
    return {"id": record.get("id"), "current": current}


def check_create_references(client: Any, request: WriteRequest) -> dict[str, Any]:
    """Make sure every record the new one points to exists.

    A new contact's email must not belong to an existing contact, so a
    create never silently duplicates or merges a real person.
    """

    ((envelope, payload),) = request.body.items()
    checked: dict[str, Any] = {}
    if payload.get("sales_account_id"):
        _read(client, "get_account", payload["sales_account_id"], "account")
        checked["sales_account_id"] = payload["sales_account_id"]
    if payload.get("targetable_type"):
        kind = payload["targetable_type"]
        _read(client, TARGETABLE_READERS[kind], payload["targetable_id"], kind)
        checked["targetable"] = f"{kind} {payload['targetable_id']}"
    if envelope == "contact" and payload.get("email"):
        found = client.lookup(payload["email"], field="email", entities="contact")
        if _lookup_has_contacts(found):
            raise ValueError(
                f"a contact with email {payload['email']!r} already exists"
            )
        checked["email_is_new"] = True
    return checked


def _lookup_has_contacts(found: Any) -> bool:
    if not isinstance(found, Mapping):
        return False
    contacts = found.get("contacts")
    if isinstance(contacts, Mapping):
        contacts = contacts.get("contacts")
    return bool(contacts)
