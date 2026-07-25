# Browser Bookmark Import Contract

## Contents

1. Data ownership
2. State machine
3. Staging entities
4. Parser and identity rules
5. Preview and confirmation
6. Recovery and idempotency
7. Performance gates

## 1. Data Ownership

Bind every job, source object, staging row, classification result, cache entry, preview, and commit
to `user_id`. Resolve the account from the authenticated session. Reject cross-account IDs as not
found. Use compound foreign keys or equivalent database constraints so repository mistakes cannot
create cross-account relationships.

Store uploaded sources under a server-generated job directory. Never trust the browser filename as
a path. Check free space and an account-scoped staging quota before parsing because normalized
staging can be much larger than the source export. Do not place source exports, previews,
URL-bearing logs, or staging databases in Git.

## 2. State Machine

```text
RECEIVING
  -> QUEUED_PARSE
  -> PARSING
  -> PARSE_PREVIEW_READY
  -> QUEUED_CLASSIFICATION
  -> CLASSIFYING
  -> FINAL_PREVIEW_READY
  -> COMMITTING
  -> COMPLETED | COMPLETED_WITH_ERRORS
```

Allow `CANCEL_REQUESTED -> CANCELLED`, retryable failure back to the relevant queued state, terminal
failure, and expiry. Persist state transitions. Use a worker lease and heartbeat. Recover expired
leases on service startup.

The parse preview is the first approval boundary. It shows source quality, duplicate policy,
unsupported items, classification scope, and estimated Provider budget. The final preview is the
second approval boundary and shows every business-data change.

## 3. Staging Entities

- `BookmarkImportJob`: account, source hash/size/format, parser and normalizer versions, state,
  progress, budget, lease, error, preview version, and timestamps.
- `BookmarkImportFolder`: unique source folder ID, parent ID, title, order, depth, full display path,
  and proposed taxonomy mapping. Do not key folders only by their display path.
- `BookmarkOccurrence`: every exported anchor with source order, raw title, raw URL, folder ID,
  timestamps, validation state, and warnings.
- `BookmarkCandidate`: one strict normalized identity linked to one or more occurrences. Store
  proposed action: `create`, `skip_existing`, `merge_missing_metadata`, `reject`, or `needs_review`.
- `ClassificationBatch`: deterministic input hash, taxonomy/model/skill versions, attempt state,
  token and cost accounting, and validated structured response.
- `ImportCommit`: final snapshot hash, confirmation-token hash, idempotency key, progress, and result.
- `SiteImportOrigin`: committed Site linkage back to job, occurrence, and source folder.

Use cursor pagination for folders, occurrences, candidates, and commit results. Never return a large
job as one JSON response.

## 4. Parser And Identity Rules

Recognize the Netscape marker by content rather than extension. Decode incrementally, tolerate the
unclosed `DT` and `P` elements emitted by browsers, and never use an XML parser. Default guardrails:

- source: at most 512 MiB locally; production quota may choose a lower account limit;
- bookmarks: at most 500,000;
- folders: at most 100,000;
- folder depth: at most 64;
- URL: at most 16,384 characters;
- title: at most 1,024 characters.

Strict URL identity may lowercase scheme/IDNA host, remove a default port, add an empty root path,
and normalize the host encoding. Preserve path case, query order, repeated query keys, and fragment.
Do not merge HTTP with HTTPS. Any looser similarity result is a review hint only.

Allow storing HTTP(S) private or loopback targets, but mark them `export_metadata_only`. The safe
fetcher must independently resolve and validate every DNS result and redirect at request time.
Do not place those targets' hostnames, titles, or folder labels in an external classification batch;
use deterministic local classification and editable preview fallback instead.

## 5. Preview And Confirmation

Do not mutate Site, Category, Tag, Space, or index tables during parsing or classification. The final
preview binds:

- account and import job;
- selected candidate IDs and actions;
- taxonomy version and affected Site versions;
- normalized payload hash and preview version;
- expiry and single-use nonce.

Consume the confirmation token once. Commit with an idempotency key and short write transactions.
Existing Sites default to `skip_existing`; never silently overwrite them.

## 6. Recovery And Idempotency

Hash the source during upload and detect repeated imports within the same account. Parse from the
start into a new `parse_run_id`; atomically make a complete run current. Do not resume from arbitrary
HTML byte offsets.

Persist classification batches so completed calls are not repeated. Cache only within one account
and include input, taxonomy, model, prompt, and skill versions in the cache key. Apply cancellation
at chunk or batch boundaries. Resume a partial commit by its idempotency key and per-candidate state.

## 7. Performance Gates

- Provided 2,541-bookmark sample: parse and stage in under 5 seconds on the reference Windows host.
- Synthetic 100,000 bookmarks: parse and stage in under 120 seconds.
- Favicon-heavy input near the configured upload quota: remain within the parser memory gate.
- Peak additional Python memory: under 128 MiB and independent of source-file byte size.
- Cursor page over 100,000 staged candidates: P95 under 300 ms.
- Progress heartbeat: at least every 2 seconds.
- SQLite write-lock target: under 250 ms per transaction batch.

Test arbitrary chunk boundaries, duplicate folder paths, duplicate URL occurrences, title conflicts,
query/fragment identity, malformed browser HTML, resource limits, cancellation, recovery, account
isolation, confirmation replay, and DNS/redirect SSRF protections.
