"""Read and delete adapters for the live harness.

``read`` returns the record as a dict, or None when the API no longer
returns it (404, or a record the API flags as deleted). ``delete`` sends the
DELETE. Both are used only by ``harness.Tracker``, which checks ownership
first and verifies the result.
"""

from __future__ import annotations

from typing import Any

from freshsales_cli.client import FreshsalesClient
from freshsales_cli.errors import ApiError as SalesApiError
from freshservice_cli.client import FreshserviceClient
from freshservice_cli.errors import ApiError as ServiceApiError

from .harness import Adapter, Entry

SALES_PATHS = {
    "contacts": ("/contacts", "contact"),
    "accounts": ("/sales_accounts", "sales_account"),
    "deals": ("/deals", "deal"),
    "tasks": ("/tasks", "task"),
}
PARENT_PATHS = {
    "contacts": "/contacts",
    "accounts": "/sales_accounts",
    "deals": "/deals",
}


def _gone(record: Any) -> bool:
    return not isinstance(record, dict) or bool(
        record.get("deleted") or record.get("is_deleted")
    )


def freshsales_adapters(client: FreshsalesClient) -> dict[tuple[str, str], Adapter]:
    def reader(entity: str) -> Any:
        path, key = SALES_PATHS[entity]

        def read(entry: Entry) -> dict[str, Any] | None:
            try:
                payload = client._request("GET", f"{path}/{entry.id}")
            except SalesApiError as exc:
                if exc.status_code == 404:
                    return None
                raise
            record = (payload or {}).get(key)
            return None if _gone(record) else record

        return read

    def deleter(path: str) -> Any:
        return lambda entry: client._request("DELETE", f"{path}/{entry.id}")

    def read_note(entry: Entry) -> dict[str, Any] | None:
        # Notes have no GET route: look for the note on its parent record.
        parent_entity, parent_id = str(entry.parent).split(":")
        try:
            payload = client._request(
                "GET",
                f"{PARENT_PATHS[parent_entity]}/{parent_id}",
                params={"include": "notes"},
            )
        except SalesApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        for note in (payload or {}).get("notes") or []:
            if note.get("id") == entry.id:
                return note
        return None

    adapters = {
        ("freshsales", entity): Adapter(reader(entity), deleter(path))
        for entity, (path, _) in SALES_PATHS.items()
    }
    adapters[("freshsales", "notes")] = Adapter(read_note, deleter("/notes"))
    return adapters


def freshservice_adapters(
    client: FreshserviceClient,
) -> dict[tuple[str, str], Adapter]:
    def read_ticket(entry: Entry) -> dict[str, Any] | None:
        try:
            # Tags are only returned when asked for.
            payload = client._request(
                "GET", f"/tickets/{entry.id}", params={"include": "tags"}
            )
        except ServiceApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        record = (payload or {}).get("ticket")
        return None if _gone(record) else record

    return {
        ("freshservice", "tickets"): Adapter(
            read_ticket, lambda entry: client._request("DELETE", f"/tickets/{entry.id}")
        )
    }
