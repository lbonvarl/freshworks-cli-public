"""Delete live-test records left behind by an interrupted run.

Usage: ``FRESHWORKS_LIVE_WRITE=1 python -m tests_live.cleanup``, with
``FRESHSALES_BASE_URL`` and/or ``FRESHSERVICE_BASE_URL`` set for the products
that have leftovers.

Reads the ledger, and for every record not yet verified as deleted applies
the same checks as the live suites: the record must still carry its run tag
or the [CLI-TEST] prefix, and it must be unreadable after the delete.
Prints one line per record (key and outcome only).
"""

from __future__ import annotations

import sys

from freshsales_cli.auth import load_api_key as load_sales_key
from freshsales_cli.client import BASE_URL_ENV as SALES_URL_ENV
from freshsales_cli.client import FreshsalesClient
from freshservice_cli.auth import load_api_key as load_service_key
from freshservice_cli.client import BASE_URL_ENV as SERVICE_URL_ENV
from freshservice_cli.client import FreshserviceClient

from .adapters import freshsales_adapters, freshservice_adapters
from .harness import (
    LIVE_FLAG,
    Ledger,
    Tracker,
    VerificationError,
    base_url,
    live_enabled,
)


def main() -> int:
    if not live_enabled():
        print(f"set {LIVE_FLAG}=1 to clean up live test records", file=sys.stderr)
        return 2
    ledger = Ledger()
    pending = [entry for entry in ledger.load() if entry.status != "deleted"]
    if not pending:
        print("ledger is clean")
        return 0
    adapters = {}
    if any(entry.product == "freshsales" for entry in pending):
        adapters.update(
            freshsales_adapters(
                FreshsalesClient(
                    load_sales_key(),
                    base_url=base_url(SALES_URL_ENV),
                    allow_writes=True,
                )
            )
        )
    if any(entry.product == "freshservice" for entry in pending):
        adapters.update(
            freshservice_adapters(
                FreshserviceClient(
                    load_service_key(),
                    base_url=base_url(SERVICE_URL_ENV),
                    allow_writes=True,
                )
            )
        )
    failed = 0
    for run_tag in sorted({entry.run_tag for entry in pending}):
        tracker = Tracker(run_tag, adapters, ledger)
        tracker.entries = [entry for entry in pending if entry.run_tag == run_tag]
        try:
            results = tracker.cleanup()
        except VerificationError as exc:
            failed += 1
            print(f"{run_tag}: {exc}", file=sys.stderr)
            continue
        for key, outcome in results.items():
            print(f"{key}: {outcome}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
