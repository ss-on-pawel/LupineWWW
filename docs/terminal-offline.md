# Terminal: local storage and offline operation

Open `/inventory/terminal/` online, log in, choose a session and terminal, then
press Start. The device stores the working session and locations in IndexedDB.
The memory status must confirm that reopening offline is ready. The browser must
support HTTPS service workers, IndexedDB and Web Locks (current Chrome/Edge).
Keep one terminal tab open per browser profile.

An offline scan and its new location/counter state commit in one transaction.
The interface confirms a scan only after transaction completion. A failed write
stops scanning, retains uncommitted events in memory and offers Retry and JSON
export. Do not close the page before retrying/exporting uncommitted events.

After reconnecting, pending records from all sessions of the original account
are sent in batches of 100. Only individual server acknowledgements containing
the client scan ID and raw scan ID mark a record as synced. A missing reply is
retried with the same client ID, using the existing server uniqueness constraint.
A closed/rejected old session stays in the queue but does not block other sessions.
Confirmed records remain on the device for export; this version does not prune
the journal. The pending count includes all sessions and any legacy records.

IndexedDB schema v3 has dedicated indexes for pending records, owner/session/
terminal groups and counts. Normal scanning, counters and sync never load the
full journal: sync reads at most 100 pending records per batch, and counts use
index.count(). The room dictionary is stored separately from the frequently
updated working state, so a scan does not rewrite thousands of room definitions.
Upgrading v1/v2 builds indexes once inside the upgrade transaction and preserves
both pending records and receipts. A large existing journal may make this one-time
preparation take longer; do not close the tab while it is preparing storage.

Export reads at most 500 records at a time into serialized Blob parts instead of
loading all journal objects into JavaScript memory. Scanning pauses for a consistent
export snapshot and resumes afterwards. Downloading a very large file still needs
space for the resulting file; records are never removed automatically.


Expired authentication does not erase local records. Log back in using the
dedicated link and the same account; automatic sync then resumes. A different
account cannot reassign or send the saved queue. Account switching/reassignment
is outside this stage. Old v1 records without an owner are preserved and included
in JSON export but never silently assigned or sent as another user.

The service worker caches only a generic screen and its client JavaScript.
It never caches authenticated pages, login forms or API responses. The generic
screen is public; local session/scan data is loaded from this browser's IndexedDB.
It does not grant server access. The context API still requires Django login and
issues a CSRF token for the normal protected POST endpoints.

The browser is asked for persistent storage, and lack of permission or high
storage usage is shown. This is not a substitute for export/synchronization:
clearing site data, private browsing, device loss or storage hardware failure can
still remove local data. The JSON export contains the journal, server receipts,
working state and events not yet committed. No passwords or authentication
tokens are exported. Automated import of backup files is outside this stage.

## Development checks

Backend regression tests use the normal Django test runner:

```text
python manage.py test inventory.tests.TerminalScannerTests inventory.tests.TerminalOfflineTests
```

The browser test requires Playwright with Chromium, plus the application's
installed Python dependencies. It runs a mock server on loopback and an isolated
browser profile; no production database or credentials are used:

```text
node tests/browser/terminal_offline.cjs
```

Optional environment variables: `PYTHON_EXE`, `PLAYWRIGHT_MODULE` (an absolute path
to an existing Playwright package), and `CHROMIUM_EXECUTABLE_PATH`. The test prints
the temporary artifacts directory. It covers database upgrade, offline refresh,
tab and browser restart, rapid scanning, aborted transactions and retry, login
expiry, account mismatch, closed sessions, lost server acknowledgements,
idempotent replay, export, cache isolation and parallel-tab exclusion. The load
case adds 100,000 confirmed records, 10,000 pending scans and 5,000 locations, then
checks 200 new writes, indexed sync, a complete 110,306-record export and v2-to-v3
migration. Instrumentation rejects unbounded journal reads and room-dictionary
rewrites during scanning. Reported timings measure the test computer and loopback
mock server, not a promise about a particular scanner or production network.

The client is served by Django at `/inventory/terminal/client.js`; no new package,
database migration or collectstatic is required. When changing the cached screen
or client in a future release, increment `TERMINAL_SHELL_CACHE` in inventory/views.py.
Service-worker updates take effect after old terminal tabs close; they do not
force a reload or delete IndexedDB scans.
