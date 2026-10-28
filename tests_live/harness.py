"""Shared safety harness for the opt-in live write tests.

The live suites create a handful of clearly labelled test records through the
real CLI commands, check them with GET calls, and delete them again. This is
the only code in the repository that deletes anything, and it is fenced in:

* Nothing runs unless ``FRESHWORKS_LIVE_WRITE=1`` is set.
* Every record gets the run tag (``clitest-<UTC minute>-<random>``) where the
  entity has tags, and the ``[CLI-TEST]`` prefix in its name, title, subject
  or description.
* At most ``MAX_PER_ENTITY`` records of each kind may be created per run.
* Each created record is written to a local ledger *before* anything else is
  done with it, so an interrupted run can be cleaned up later with
  ``python -m tests_live.cleanup``.
* ``Tracker.delete`` only deletes a record that is in this run's ledger and
  still carries the run tag or the prefix, then reads it again to verify it is
  gone. Any record it cannot verify makes the run fail loudly. "Gone" means a
  404 for Freshsales, and ``deleted: true`` for Freshservice tickets, which
  are moved to the trash rather than purged.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIVE_FLAG = "FRESHWORKS_LIVE_WRITE"
NAME_PREFIX = "[CLI-TEST]"
MAX_PER_ENTITY = 3
DEFAULT_LEDGER = Path(__file__).with_name(".live-ledger.json")


def live_enabled() -> bool:
    return os.environ.get(LIVE_FLAG) == "1"


def new_run_tag() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    return f"clitest-{stamp}-{secrets.token_hex(2)}"


def base_url(env: str) -> str:
    """Return the API base URL from ``env``; the live suites have no default."""

    value = os.environ.get(env)
    if not value:
        raise RuntimeError(f"set {env} to the API base URL of the account to test")
    return value


def ledger_path() -> Path:
    return Path(os.environ.get("FRESHWORKS_LIVE_LEDGER") or DEFAULT_LEDGER)


@dataclass
class Entry:
    product: str  # "freshsales" or "freshservice"
    entity: str  # contacts, accounts, deals, tasks, notes, tickets
    id: int
    run_tag: str
    # Notes cannot be read on their own; they are verified through a parent.
    parent: str | None = None
    status: str = "created"  # created -> deleted (verified) or failed

    @property
    def key(self) -> str:
        return f"{self.product}:{self.entity}:{self.id}"


class Ledger:
    """A small JSON file listing every live test record and its state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ledger_path()

    def load(self) -> list[Entry]:
        if not self.path.exists():
            return []
        return [Entry(**item) for item in json.loads(self.path.read_text())]

    def save(self, entries: list[Entry]) -> None:
        self.path.write_text(json.dumps([asdict(e) for e in entries], indent=2))

    def upsert(self, entry: Entry) -> None:
        entries = [e for e in self.load() if e.key != entry.key]
        entries.append(entry)
        self.save(entries)


class VerificationError(AssertionError):
    """A test record could not be proven to belong to the run or be gone."""


# Ownership and existence checks ---------------------------------------------
def carries_marker(record: dict[str, Any], run_tag: str) -> bool:
    """True when the record has the run tag or the [CLI-TEST] prefix."""

    tags = record.get("tags") or []
    if run_tag in tags:
        return True
    for key in ("name", "subject", "title", "description", "display_name"):
        value = record.get(key)
        if isinstance(value, str) and value.startswith(NAME_PREFIX):
            return True
    return False


@dataclass
class Adapter:
    """How to read and delete one entity in one product."""

    read: Callable[[Entry], dict[str, Any] | None]
    delete: Callable[[Entry], Any]


class Tracker:
    """Registers created records and deletes them with verification."""

    def __init__(
        self,
        run_tag: str,
        adapters: dict[tuple[str, str], Adapter],
        ledger: Ledger | None = None,
    ) -> None:
        self.run_tag = run_tag
        self.adapters = adapters
        self.ledger = ledger or Ledger()
        self.entries: list[Entry] = []

    def register(
        self, product: str, entity: str, record_id: Any, *, parent: str | None = None
    ) -> Entry:
        count = sum(
            1 for e in self.entries if (e.product, e.entity) == (product, entity)
        )
        if count >= MAX_PER_ENTITY:
            raise VerificationError(
                f"refusing to track more than {MAX_PER_ENTITY} {entity} per run"
            )
        entry = Entry(product, entity, int(record_id), self.run_tag, parent)
        self.ledger.upsert(entry)
        self.entries.append(entry)
        return entry

    def delete(self, entry: Entry) -> str:
        """Delete one record of this run and verify it is gone.

        Returns ``"deleted"`` or ``"already gone"``. Raises
        VerificationError if the record does not carry the run marker or is
        still readable after the delete.
        """

        if entry.run_tag != self.run_tag and entry not in self.entries:
            raise VerificationError(f"{entry.key} is not from this run")
        adapter = self.adapters[(entry.product, entry.entity)]
        before = adapter.read(entry)
        if before is None:
            entry.status = "deleted"
            self.ledger.upsert(entry)
            return "already gone"
        if not carries_marker(before, entry.run_tag):
            entry.status = "failed"
            self.ledger.upsert(entry)
            raise VerificationError(
                f"{entry.key} has neither tag {entry.run_tag} nor the "
                f"{NAME_PREFIX} prefix; refusing to delete it"
            )
        adapter.delete(entry)
        if adapter.read(entry) is not None:
            entry.status = "failed"
            self.ledger.upsert(entry)
            raise VerificationError(f"{entry.key} is still readable after delete")
        entry.status = "deleted"
        self.ledger.upsert(entry)
        return "deleted"

    def cleanup(self) -> dict[str, str]:
        """Delete every record of this run, children first; never stop early."""

        results: dict[str, str] = {}
        errors: list[str] = []
        for entry in reversed(self.entries):
            if entry.status == "deleted":
                results[entry.key] = "deleted"
                continue
            try:
                results[entry.key] = self.delete(entry)
            except Exception as exc:  # noqa: BLE001 - try every record, then raise
                results[entry.key] = f"FAILED: {exc}"
                errors.append(entry.key)
        if errors:
            raise VerificationError(
                f"cleanup could not verify {', '.join(errors)}; run "
                "`python -m tests_live.cleanup` and check the ledger"
            )
        return results
