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
