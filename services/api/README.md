# WebHub API

FastAPI is WebHub's only business backend. During the Windows LAN MVP it listens on
`127.0.0.1:8100` and is reached by browsers through the Next.js same-origin proxy.

Initialize or upgrade the database from the repository root:

```powershell
uv run --project services/api webhub-db upgrade
```

Then start the API:

```powershell
uv run --project services/api uvicorn webhub.main:app --reload --no-proxy-headers `
  --host 127.0.0.1 --port 8100
```

`pnpm dev:api` performs the explicit upgrade before starting Uvicorn. Application startup and
readiness only verify that the database is at the current Alembic head. They never stamp, replace,
or silently adopt an unversioned database. Use `webhub-db current` and `webhub-db check` to inspect
an existing database before migration.

Keep Uvicorn on loopback with `--no-proxy-headers`, and start Next.js through the repository's
`pnpm dev:web` or `pnpm start` scripts. The custom website server replaces inbound forwarding
headers with the real socket address before Next handles the request. WebHub only trusts that
single-hop original host and client address when the API peer is loopback; non-loopback clients
cannot make forwarding headers trusted. Bypassing the custom website server invalidates this trust
contract and is unsupported. The website rejects request hosts outside the detected local machine
names and addresses; add intentional aliases through `WEBHUB_ALLOWED_HOSTS`.

Login throttling atomically reserves both a client-and-account bucket and a client-wide bucket
before password verification. Its bounded state is intentionally process-local for the Windows
MVP. Run exactly one API worker; restart clears the throttling window, and multi-worker deployment
requires a shared limiter before it is supported.

The identity API exposes register, login, logout, current-account, password-change, and
account-preference endpoints under `/api/auth/*`. Session cookies are opaque and HttpOnly; only
their SHA-256 hashes are persisted, while passwords use Argon2id.

The account-scoped bookmark backend currently includes immutable source snapshots, jobs, parse
runs, current-run pointers, chunk checkpoints, staging facts, crash-recoverable file intake, and a
strict validator for untrusted classification JSON. The classification validator is only an input
boundary: no persistent classification worker or real LLM integration is complete. Read-only
preview APIs are available at:

- `GET /api/bookmark-imports/{job_id}/preview`
- `GET /api/bookmark-imports/{job_id}/preview/folders`
- `GET /api/bookmark-imports/{job_id}/preview/candidates`
- `GET /api/bookmark-imports/{job_id}/preview/occurrences`

The collection endpoints use bounded account- and run-bound keyset cursors. They never return the
complete import in one response. Final confirmation and permanent Site/source commit are not yet
implemented.

## Raw bookmark upload contract

`POST /api/bookmark-imports` and `GET /api/bookmark-imports/{job_id}` are connected to the tested
intake core and pass their API contract suite. Browsers use the website rewrite paths
`/api/backend/bookmark-imports` and `/api/backend/bookmark-imports/{job_id}`.

The POST body is the raw Chrome/Edge/Netscape bookmark HTML file. JSON, Base64, and multipart
uploads are not accepted. Required request data is:

- a valid WebHub session cookie;
- a trusted `Origin`;
- an `Idempotency-Key` between 16 and 512 characters;
- `Content-Type: text/html` (parameters allowed) or `application/octet-stream`.

Example through the supported website proxy:

```powershell
curl.exe -i -X POST http://localhost:3100/api/backend/bookmark-imports `
  -H "Origin: http://localhost:3100" `
  -H "Content-Type: text/html" `
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" `
  -H "Cookie: webhub_session=<session-token>" `
  --data-binary "@C:\path\to\bookmarks.html"
```

A new job returns `201`; an exact account/key/content replay returns `200`; a reused key with
different content returns `409`. A new key for an already seen source creates a new job and sets
`same_source_warning`. Responses expose the job ID, public state/version, replay flag, and warning
only. They must not expose a snapshot ID, digest, temporary/final path, or `storage_key`.

The status GET returns the public job state, job/preview versions, `{completed,total}` progress,
public failure code, and timestamps with `Cache-Control: no-store`. Unknown and cross-account job
IDs both return `404`.

## Bookmark upload admission

FastAPI applies admission before consuming the request body and continues checking storage while
the body streams. The defaults are:

- 4 active uploads across the process;
- 1 active upload per account;
- 6 admission attempts per account in each 60-second window;
- 10,000 tracked account windows, which bounds in-memory limiter state rather than registrations;
- 2 GiB per account across published source files and incoming temporary files, including crash
  leftovers;
- 512 MiB of disk space kept free, with free space checked again after each 8 MiB streamed.

Concurrency or rate rejection returns `429`, account quota rejection returns `413`, and
insufficient/unknown disk capacity returns `507`. Concurrency, rate, and tracked-window state are
process-local. The current Windows MVP startup contract supports exactly one API worker; a
multi-worker deployment would apply independent counters and is unsupported until admission uses
shared coordination.

The upload and intake kernels enforce the byte limit while streaming even when `Content-Length` is
absent. The 1.6 MB Chrome/Edge fixture passes both the Python kernel and direct FastAPI API tests.
For the website path, the custom server intercepts only `POST /api/backend/bookmark-imports` and
streams it directly to FastAPI, bypassing Next's 10 MiB request clone. Tests pass for the real
fixture with exact bytes and digest, preserved request/response headers, a 12 MiB streamed body,
and both declared-length and chunked limit violations. All other `/api/backend/*` paths still use
the Next rewrite. The website proxy's 512 MiB per-request ceiling is separate from FastAPI's
512 MiB reserved-free-space policy; neither is evidence that a 512 MiB file capacity gate has been
run. FastAPI remains loopback-only; exposing it to the LAN is not a supported workaround.

If an account owner loses access, reset the password from the host machine:

```powershell
uv run --project services/api webhub-account reset-password <username>
```

The command reads and confirms the password through hidden interactive prompts. It never accepts a
password argument, and a successful reset revokes every existing session for that account.

Create a read-only browser bookmark import preview:

```powershell
uv run --project services/api webhub-bookmarks-preview <bookmarks.html> `
  --output-dir <new-temp-output-directory>
```

The preview pipeline performs no network requests, Provider calls, or business-data writes.
