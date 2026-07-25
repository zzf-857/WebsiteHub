# WebHub API

FastAPI is WebHub's only business backend. During the Windows LAN MVP it listens on
`127.0.0.1:8100` and is reached by browsers through the Next.js same-origin proxy.

Initialize or upgrade the database from the repository root:

```powershell
uv run --project services/api webhub-db upgrade
```

Then start the API:

```powershell
uv run --project services/api uvicorn webhub.main:app --reload --host 127.0.0.1 --port 8100
```

`pnpm dev:api` performs the explicit upgrade before starting Uvicorn. Application startup and
readiness only verify that the database is at the current Alembic head. They never stamp, replace,
or silently adopt an unversioned database. Use `webhub-db current` and `webhub-db check` to inspect
an existing database before migration.

The identity API exposes register, login, logout, current-account, password-change, and
account-preference endpoints under `/api/auth/*`. Session cookies are opaque and HttpOnly; only
their SHA-256 hashes are persisted, while passwords use Argon2id.

Create a read-only browser bookmark import preview:

```powershell
uv run --project services/api webhub-bookmarks-preview <bookmarks.html> `
  --output-dir <new-temp-output-directory>
```

The preview pipeline performs no network requests, Provider calls, or business-data writes.
