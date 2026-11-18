# mailbackup

[![CI](https://github.com/chad3814/imap-backup/actions/workflows/ci.yml/badge.svg)](https://github.com/chad3814/imap-backup/actions/workflows/ci.yml)
[![Docker](https://github.com/chad3814/imap-backup/actions/workflows/docker.yml/badge.svg)](https://github.com/chad3814/imap-backup/actions/workflows/docker.yml)

Content-addressed IMAP backup to S3, with **point-in-time mbox restore**. Every
five minutes it fetches new mail, appends a small JSONL record per folder, and
uploads only what changed.

It was built for a mailbox where messages are constantly **moved** between
folders (filters, triage, archive rules). A move changes the message's UID and
folder, but not its bytes — so a move costs two small log lines and **zero
message bytes uploaded**.

```
IMAP ── UID SEARCH + FETCH ──► SHA-256 content store ──► S3
                                  messages/ab/abcdef….eml
       └── folder membership ──► numbered JSONL log ───► S3
                                  mboxlogs/INBOX-00000000.jsonl
```

## Why not git / git-remote-s3?

`git-remote-s3` creates a **complete, self-contained bundle on every push**
(`git bundle create <sha>.bundle <ref>`), so each push re-uploads the entire
tree — hundreds of MB every five minutes for a large mailbox, and worse as it
grows. History squashing does not help: the bundle size tracks the tree, not the
diff.

Git LFS does make it viable (a 100 KB mbox produced a 688-byte git bundle plus
one LFS object), and that remains an option if you want git semantics. This
project takes the simpler route: the JSONL log *is* the revision history, and
message bytes are stored once, content-addressed. No git, no LFS, no bundle
math.

## What is stored

```
s3://<bucket>/<prefix>/
  state.json                                  commit marker + per-folder UID state
  messages/<2 hex>/<sha256>.eml               exact RFC 822 bytes, immutable
  mboxlogs/INBOX-00000000.jsonl               numbered, self-contained log
  mboxlogs/INBOX-00000001.jsonl
  mboxlogs/Sent%20Items-00000000.jsonl
```

* Each folder is a numbered sequence of files: `INBOX-00000000.jsonl`,
  `INBOX-00000001.jsonl`, and so on. The folder part is URL-quoted, so
  `Read/Reddit` becomes `Read%2FReddit-00000000.jsonl`.
* **Every file starts with a `reset` record that lists the whole folder**, then
  contains only `add` / `rm` operations after that baseline. A file is
  therefore self-contained, and a mirror restore reads exactly one file.
* A file holds at most `MAX_OPS_PER_FILE` (default 5,000) records in total —
  the reset baseline plus add/rm operations. When it is full, on a UIDVALIDITY
  change, or when `SNAPSHOT_EVERY_SECONDS`
  elapses, the current file is closed and the next one starts with a fresh
  baseline. Closing a file early is exactly how a snapshot is taken.
* `<cycle>` inside records is a UTC timestamp with microseconds, e.g.
  `20261118T180354.877573Z`. It sorts lexicographically and is used for
  point-in-time restores.
* A message blob is named by the **SHA-256 of its raw bytes**. Two identical
  copies in different folders share one blob. Two messages with the same
  `Message-ID` but different bytes are stored separately (the `Message-ID` is
  kept only as searchable metadata).

### Event records

```json
{"v":1,"t":"reset","c":"20261118T180354.877573Z","f":"INBOX","uv":987654321,"items":[[42,"9f2c…",["\\Seen"],"<x@y>",1234]]}
{"v":1,"t":"add","c":"20261118T180400.000001Z","f":"INBOX","u":43,"k":"ab12…","fl":["\\Seen"],"m":"<z@y>","s":999}
{"v":1,"t":"rm","c":"20261118T180500.000002Z","f":"INBOX","u":42,"k":"9f2c…"}
```

* `reset` is the baseline. Its `items` are every message in the folder at that
  cycle. It is always the first line of a file.
* `add` / `rm` are the deltas after the baseline. `u` is the IMAP UID at that
  moment, `k` the content hash, `fl` the flags at fetch time, `m` the
  Message-ID, `s` the size.
* A UIDVALIDITY change starts a **new numbered file** with a fresh reset
  baseline; the old file remains for restores before that point.
* A mirror restore picks the newest file whose reset is at or before the target
  time and applies that file's operations up to the cutoff. It never replays
  older files.

`state.json` records the last **committed** cycle, per-folder UIDVALIDITY /
last UID, and the active file sequence and operation count. Objects are uploaded
before `state.json`, and restore never reads past `last_cycle`, so an
interrupted upload is never half-restored. On startup the active file is also
compared against the remote object size (appends only grow it), so a local file
whose upload was lost is re-uploaded rather than silently skipped.

## Quick start

### 1. Bucket and IAM

```bash
aws s3 mb s3://my-mail-archive --region us-east-1
aws s3api put-bucket-versioning --bucket my-mail-archive \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket my-mail-archive \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Minimal IAM policy (no deletes are ever issued):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListPrefix",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::my-mail-archive",
      "Condition": {"StringLike": {"s3:prefix": ["mail/*"]}}
    },
    {
      "Sid": "Objects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::my-mail-archive/mail/*"
    }
  ]
}
```

`verify` only needs `ListBucket` + `GetObject`; backup needs `PutObject` too.
If you enable `S3_SSE=aws:kms`, also allow `kms:GenerateDataKey` and
`kms:Decrypt` on the key.

### 2. Configure

```bash
cp .env.example .env
mkdir -p secrets
printf '%s' 'your-imap-app-password' > secrets/imap_password.txt
chmod 600 secrets/imap_password.txt
$EDITOR .env        # host, user, bucket, prefix, AWS credentials
```

The password is **not** in the environment. `docker-compose.yml` mounts the
file as a Compose secret and sets `IMAP_PASSWORD_FILE=/run/secrets/imap_password`.

### 3. Run

The image is published to `ghcr.io/chad3814/imap-backup`, so the fastest start
pulls it instead of building:

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

To build from the checkout instead:

```bash
docker compose build
docker compose up -d
```

See [Container image](#container-image) for tags and architecture support.

The first cycle seeds the whole mailbox (a 663 MB / 7,130-message mailbox took
minutes, not hours, with the exporter this is based on). Later cycles fetch only
new UIDs. The healthcheck has a 5-minute start period to accommodate the seed.

### 4. Restore

```bash
# Latest committed state
docker compose run --rm -v "$PWD/restore:/out" mailbackup restore --out /out

# A specific point in time (whole seconds / dates are inclusive)
docker compose run --rm -v "$PWD/restore:/out" mailbackup restore \
  --out /out --at 2026-11-18T12:00:00Z

# One folder only
docker compose run --rm -v "$PWD/restore:/out" mailbackup restore \
  --out /out --folder INBOX

# Everything ever seen in each folder, including moved/deleted messages
# (replays every numbered file, so it is slower than a mirror restore)
docker compose run --rm -v "$PWD/restore:/out" mailbackup restore \
  --out /out --keep-removed

# Only for recovery: include files uploaded after the last committed cycle
docker compose run --rm -v "$PWD/restore:/out" mailbackup restore \
  --out /out --include-uncommitted

# Health of the backup itself
docker compose run --rm mailbackup verify
docker compose run --rm mailbackup list
```

`restore` writes one `.mbox` per folder plus `manifest.json` (cutoff, counts,
sizes, SHA-256 of each mbox). Downloaded blobs are checked against their
SHA-256; `verify` checks that every referenced blob exists but does not re-hash
the whole archive. Mbox files are generated from the raw messages at
restore time, with `From ` separator lines, `Status`/`X-Status` from the stored
flags, CRLF normalisation, and mboxrd `>From` escaping. Import them with Apple
Mail (File → Import Mailboxes) or Thunderbird (ImportExportTools NG).

## Container image

`ghcr.io/chad3814/imap-backup` is built for `linux/amd64` and `linux/arm64` by
[`.github/workflows/docker.yml`](.github/workflows/docker.yml) and stored in the
GitHub Container Registry:

| Trigger | Tags pushed |
|---|---|
| push to `main` | `latest`, `main`, `sha-<commit>` |
| tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `sha-<commit>` |
| pull request | none — the image is built but not pushed |

Pin deployments to a release tag or `sha-<commit>`; `latest` moves with `main`.
The package is private by default, so run `docker login ghcr.io` before pulling
(or make the package public in the repository's package settings).

## How the tricky cases behave

### A message moves from INBOX to Read

IMAP gives it a new UID in Read and removes it from INBOX. The active files get:

```text
mboxlogs/INBOX-00000000.jsonl:  {"t":"rm","u":42,"k":"9f2c…"}
mboxlogs/Read-00000000.jsonl:   {"t":"add","u":900,"k":"9f2c…"}
```

(If Read did not have a file yet, the cycle starts one whose reset baseline
lists its current messages, including the moved one.)

The blob already exists, so **nothing is re-uploaded**. (The destination copy
is re-read over IMAP to compute and confirm its hash; the S3 cost is zero.)
Restore before the move shows it in INBOX; after the move it is only in Read;
`--keep-removed` shows it in both.

### A message is deleted

Only an `rm` record is written. The blob is never deleted, so `--at` before the
deletion and `--keep-removed` still contain it. This is deliberate: a backup
should not lose mail because someone emptied Trash.

### UIDVALIDITY changes

A new numbered file starts with a reset baseline of the current messages; the
old file remains for earlier restores. Blobs are deduplicated by hash, so the
new baseline is metadata-only. No silent mixing of old and new UID spaces.

### Flags

Flags are captured when a message is first stored and again when it arrives in a
new folder after a move. A message that stays in place and is later marked read
keeps its original `Status`. That is a known limitation of an append-only log
(see below).

### Folders

By default every selectable folder is backed up **except** Trash, Junk and All
Mail (detected by special-use flags and common names). Use
`IMAP_ALL_FOLDERS=true` or `IMAP_FOLDERS=INBOX,Sent Items` to change that. A move
into an excluded folder looks like a deletion to the mirror view; the blob is
still retained.

Nested folders such as `Read/Root` are treated as opaque names, not as local
directories:

* the log file is `mboxlogs/Read%2FRoot-00000000.jsonl` — URL-quoted and flat,
  so no `Read/` directory is created;
* every event still records the real name (`"f":"Read/Root"`), and `state.json`
  is keyed by it;
* restore writes `Read_Root.mbox` and records the real folder name in
  `manifest.json`; if a real folder `Read_Root` also exists, the second output
  becomes `Read_Root-2.mbox`;
* a `\Noselect` parent (`Read`) is skipped while the selectable child
  (`Read/Root`) is backed up; a selectable parent and child are independent
  folders;
* `IMAP_FOLDERS=Read/Root` or `restore --folder Read/Root` selects exactly that
  folder.

`..`, leading dots, `%`, spaces, quotes and other path characters are encoded in
filenames; the IMAP `SELECT` name is quoted when IMAP requires it (`Read/Root`
itself is a valid atom).

## Configuration

All settings are environment variables, so `docker-compose.yml` / `.env`
controls the container. CLI flags override them for one-off runs.

| Variable | Default | Meaning |
|---|---|---|
| `IMAP_HOST` | required | IMAP server |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_USER` | required | Full email address |
| `IMAP_PASSWORD_FILE` | — | File containing the password (Compose secret) |
| `IMAP_PASSWORD` | — | Alternative for local/dev use |
| `IMAP_SSL` | `true` | Use TLS |
| `IMAP_FOLDERS` | all but junk/trash | Comma-separated folder list |
| `IMAP_ALL_FOLDERS` | `false` | Include Trash/Junk/All Mail |
| `IMAP_BATCH_SIZE` | `50` | UIDs per FETCH |
| `IMAP_TIMEOUT` | `60` | Socket timeout (seconds) |
| `IMAP_RETRIES` | `3` | Reconnects per batch |
| `DATA_DIR` | `./data` | Local working/cache directory (`/data` in Docker) |
| `BACKEND` | `s3` | `s3`, `dir`, or `null` |
| `S3_BUCKET` | required for s3 | Bucket name |
| `S3_PREFIX` | `""` | Key prefix inside the bucket |
| `AWS_REGION` | SDK default | Region |
| `S3_ENDPOINT_URL` | — | S3-compatible endpoint (path-style is used) |
| `S3_STORAGE_CLASS` | — | e.g. `STANDARD_IA`, `GLACIER_IR` |
| `S3_SSE` | — | `AES256` or `aws:kms` |
| `S3_KMS_KEY_ID` | — | KMS key for SSE-KMS |
| `INTERVAL_SECONDS` | `300` | Cycle interval |
| `MAX_OPS_PER_FILE` | `5000` | Records per numbered file (reset + add/rm) |
| `SNAPSHOT_EVERY_SECONDS` | `604800` | Close the active file and start a new baseline this often |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DRY_RUN` | `false` | Count only |

AWS credentials come from the normal SDK chain: environment variables,
`AWS_PROFILE` with a mounted `~/.aws`, or an instance/task role. The example
compose file passes the standard environment variables from `.env`; for
production, an IAM role or a mounted credentials file is preferable to keys in
`.env`.

## Operations

* **Only new data is uploaded.** A cycle with no new mail uploads only the tiny
  `state.json` (plus the new baseline file when the snapshot interval elapses).
  A move uploads no message bytes. Each new message is uploaded once and then
  referenced by hash forever. The active file is re-uploaded only when it
  changes, and it is capped at 5,000 records (reset + deltas).
* **Logs.** `docker compose logs -f` shows per-folder `+added -removed`, upload
  counts, and the cycle summary.
* **Health.** `python -m mailbackup health --max-age 900` checks the age of the
  last *completed* (fully uploaded) cycle. The compose healthcheck runs it every
  minute.
* **Restart safety.** The local volume is a cache/write-ahead area. If it is
  lost, the next start lists remote blobs, downloads the logs and state, and
  resumes without re-fetching old mail. Locally written files that were not yet
  uploaded are queued and retried.
* **Single writer.** Do not run two containers against the same bucket prefix.
* **Storage growth.** Message blobs are never deleted, by design. Use S3
  lifecycle rules to move `messages/` to a cheaper class after 30–90 days; keep
  `state.json` and `mboxlogs/` in Standard (they are small and needed for every
  restore). S3 Versioning (and optionally Object Lock) protects the archive
  against accidental overwrite/delete.
* **Cost.** The initial seed is the size of the mailbox once. After that,
  `new mail only`; a 100 KB message costs one 100 KB PUT and a few hundred bytes
  of log. Each new numbered file repeats the folder baseline, so at the defaults
  you can expect roughly one copy of the folder metadata per week (or per 5,000
  operations, whichever comes first).

## Limitations

* **In-place flag changes are not tracked.** A message that is marked read or
  flagged after its initial `add` keeps the old `Status` until it moves. Moves
  recapture flags. Adding a periodic `UID FETCH 1:* (FLAGS)` diff would be a
  small future extension.
* **Folder renames and deletions leave the old folder view.** The backup
  retains the last known contents of a folder that disappears; it does not
  delete history. A renamed folder is treated as a new folder (blobs dedupe, so
  the extra cost is metadata only).
* **Excluded folders hide moves.** A move to Trash/Junk looks like a removal
  from the visible mirror, though the blob is retained.
* **Server-side header rewrites on move.** If a server changes the raw bytes
  when moving a message, the hash changes: the destination copy is stored as a
  new blob and the source is removed. No mail is lost, but a move is no longer
  free. Standard IMAP servers preserve the message.
* **One mbox per folder.** Mailboxes with a single folder larger than ~2 GB may
  exceed what some clients accept in an mbox. Restore by folder, or split with
  `--at`/`--limit` for a partial export.
* **No OAuth.** Authentication is IMAP `LOGIN` with a password/app password.
* **No message deletion.** Blobs are retained forever; that is the point of an
  archive. Nothing implements pruning.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/ruff check mailbackup tests
```

CI runs the same commands on every push to `main` and every pull request: ruff
plus the unit tests on Python 3.12, 3.13 and 3.14
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

The 100 tests cover event replay (moves, resets, baselines, point-in-time),
mbox parsing/escaping, per-folder reconciliation, restart/bootstrap, S3 via
moto, and a wire-level run of the real `imaplib` client against a local mini
IMAP server. There is no git remote involved.

## CLI reference

```
python -m mailbackup backup        # one cycle
python -m mailbackup daemon        # loop every INTERVAL_SECONDS
python -m mailbackup restore       # mbox files at a point in time
python -m mailbackup verify        # every referenced blob exists
python -m mailbackup list          # folder counts at a cutoff
python -m mailbackup health        # age of the last completed cycle
python -m mailbackup version
```

`restore` options: `--out` (required), `--at`, `--folder` (repeatable),
`--keep-removed`, `--include-uncommitted`, `--limit`, `--jobs`,
`--allow-missing`.
