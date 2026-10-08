"""
AUSTRO AI - Automated PostgreSQL backup (one-shot job).

Production logical backup of the externally managed PostgreSQL instance. The
script is intentionally a ONE-SHOT process: it never loops, never schedules
itself and never performs restore drills. It is driven by
`scripts/backup_scheduler.py` (or a host cron / systemd timer) and is never
invoked from the Telegram update loop.

Stages, in order (each aborts with a non-zero exit code on failure):

  1. duplicate-run lock      O_EXCL lock file, stale lock reclaim
  2. row-count snapshot      per-table counts via psycopg2
  3. pg_dump                 plain SQL; credentials via PGPASSFILE only
  4. gzip + manifest         streaming compression, atomic publish
  5. integrity verification  gzip round-trip + SHA-256 of content and archive
  6. retention pruning       reached ONLY after stage 5 succeeded

Security: the database password is written to a private 0600 `.pgpass` file and
handed to `pg_dump` through `PGPASSFILE`. It is never placed in command-line
arguments and never logged. `--no-password` makes `pg_dump` fail fast instead of
blocking on an interactive credential prompt.

Retention policy: the newest `BACKUP_DAILY_KEEP` (default 7) verified backups
are always kept, plus one recovery point per calendar month (UTC) for the most
recent `BACKUP_MONTHLY_KEEP` (default 30) months, where a month's recovery point
is the newest backup of that month. Archives are only ever deleted together with
their manifest, only after a new backup has been created AND verified, and the
newest backup is never deleted. Artifacts whose stamp cannot be parsed are never
deleted automatically.

Usage:
    python scripts/pg_backup.py [outdir]
    python scripts/pg_backup.py --outdir /var/backups/austro --engine postgresql

env: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, PG_BIN (optional),
     BACKUP_OUTDIR, BACKUP_DAILY_KEEP, BACKUP_MONTHLY_KEEP,
     BACKUP_ENCRYPTION_KEY_FILE, BACKUP_ENCRYPTION_ENABLED

Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

BACKUP_PREFIX = "austro_ai_backup_"
ARCHIVE_SUFFIX = ".sql.gz"
MANIFEST_SUFFIX = ".manifest.json"
DAILY_KEEP = 7
MONTHLY_KEEP = 30
LOCK_NAME = ".backup.lock"
STALE_LOCK_SECONDS = 3600
PGDUMP_TIMEOUT = 1800
CHUNK = 1024 * 1024

# Encryption configuration
BACKUP_ENCRYPTION_KEY_FILE = "BACKUP_ENCRYPTION_KEY_FILE"
BACKUP_ENCRYPTION_ENABLED = "BACKUP_ENCRYPTION_ENABLED"
_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_ARCHIVE_FORMAT_VERSION = 1


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log(stage: str, message: str) -> None:
    print(f"[{utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] {stage}: {message}", flush=True)


def pg_bin_dir() -> str:
    return os.environ.get("PG_BIN", "")


def find_tool(name: str) -> Path:
    env = os.environ.get("PG_BIN", "")
    candidates = []
    if env:
        candidates.append(Path(env) / (name + ".exe"))
        candidates.append(Path(env) / name)
    candidates.append(Path(".") / name)
    candidates.append(Path(name))  # on PATH
    for c in candidates:
        if c.exists():
            return c
    raise RuntimeError(
        f"Could not find {name}. Set PG_BIN to the PostgreSQL bin directory "
        f"(e.g. C:\\...\\pgsql\\bin)."
    )


def dsn_env() -> Dict[str, str]:
    return {
        "PGHOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PGPORT": os.environ.get("DB_PORT", "5432"),
        "PGDATABASE": os.environ.get("DB_NAME", "austro_ai"),
        "PGUSER": os.environ.get("DB_USER", "austro"),
    }


def _first_lines(text: Optional[str], limit: int = 400) -> str:
    """Sanitised, truncated diagnostic for logs (never includes credentials)."""
    if not text:
        return "(no output)"
    cleaned = " ".join(text.split())
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def _discard(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# duplicate-run protection
# --------------------------------------------------------------------------- #
def acquire_lock(
    outdir: Path,
    *,
    now: Optional[dt.datetime] = None,
    stale_seconds: int = STALE_LOCK_SECONDS,
) -> Optional[Path]:
    """Create an exclusive lock file. Returns None when another run holds it."""
    outdir.mkdir(parents=True, exist_ok=True)
    lock = outdir / LOCK_NAME
    now = (now or utcnow()).astimezone(dt.timezone.utc)
    for _ in range(2):  # one retry after reclaiming a stale lock
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = (now - dt.datetime.fromtimestamp(
                    lock.stat().st_mtime, dt.timezone.utc
                )).total_seconds()
            except OSError:
                return None
            if age < stale_seconds:
                return None
            try:
                lock.unlink()  # stale lock left behind by a killed run
            except OSError:
                return None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} started_utc={now.isoformat()}\n")
        return lock
    return None


def release_lock(lock: Optional[Path]) -> None:
    if lock is None:
        return
    try:
        lock.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# stage 2 - row counts
# --------------------------------------------------------------------------- #
def default_connect(**kwargs):
    import psycopg2  # imported lazily so the module stays import-safe

    return psycopg2.connect(**kwargs)


def snapshot_row_counts(
    dsn: Dict[str, str], connect: Callable[..., object]
) -> Tuple[List[str], Dict[str, int]]:
    """Per-table row counts at backup time (feeds the restore-drill manifest)."""
    conn = connect(
        host=dsn["PGHOST"],
        port=dsn["PGPORT"],
        dbname=dsn["PGDATABASE"],
        user=dsn["PGUSER"],
        password=os.environ.get("DB_PASSWORD", ""),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' "
                "ORDER BY table_name"
            )
            tables = [row[0] for row in cur.fetchall()]
            counts: Dict[str, int] = {}
            for table in tables:
                try:
                    cur.execute(f'SELECT count(*) FROM "{table}"')
                    counts[table] = int(cur.fetchone()[0])
                except Exception:  # noqa: BLE001 - unreadable table is not fatal
                    counts[table] = -1
    finally:
        conn.close()
    return tables, counts


# --------------------------------------------------------------------------- #
# stage 3 - pg_dump with secure authentication
# --------------------------------------------------------------------------- #
def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "\\n")


def write_pgpass(directory: Path, host: str, port: str, database: str,
                 user: str, password: str) -> Path:
    """Write a 0600 libpq password file for host:port:database:user."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".pgpass"
    line = ":".join(
        _pgpass_escape(part) for part in (host, port, database, user, password)
    )
    path.write_text(line + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best effort; no-op on some Windows filesystems
    except OSError:
        pass
    return path


def run_pg_dump(
    raw_path: Path,
    *,
    dsn: Dict[str, str],
    password: str,
    tool: Optional[Path] = None,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    timeout: int = PGDUMP_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run pg_dump. The password reaches libpq through PGPASSFILE only."""
    tool = tool or find_tool("pg_dump")
    runner = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix="austro-pgpass-") as tmp:
        pgpass = write_pgpass(
            Path(tmp), dsn["PGHOST"], dsn["PGPORT"], dsn["PGDATABASE"],
            dsn["PGUSER"], password,
        )
        env = {**os.environ, **dsn, "PGPASSFILE": str(pgpass),
               "PGCLIENTENCODING": "UTF8"}
        env.pop("PGPASSWORD", None)
        argv = [
            str(tool), "-h", dsn["PGHOST"], "-p", dsn["PGPORT"],
            "-U", dsn["PGUSER"], "-d", dsn["PGDATABASE"],
            "--no-owner", "--no-acl", "--clean", "--if-exists",
            "--no-password", "-f", str(raw_path),
        ]
        return runner(argv, env=env, capture_output=True, text=True, timeout=timeout)


def pg_dump_version(tool: Optional[Path] = None,
                    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> str:
    """Best-effort version string recorded in the manifest (PG16 evidence)."""
    runner = runner or subprocess.run
    try:
        proc = runner([str(tool or find_tool("pg_dump")), "--version"],
                      capture_output=True, text=True, timeout=30)
        return (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001 - purely informational
        return ""


# --------------------------------------------------------------------------- #
# stage 4 - compression + manifest
# --------------------------------------------------------------------------- #
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Encryption / decryption (AES-256-GCM, key from secret file)
# --------------------------------------------------------------------------- #
_TRUTHY = {"1", "true", "yes", "on"}


def encryption_required() -> bool:
    """True when BACKUP_ENCRYPTION_ENABLED demands encryption (fail closed)."""
    return str(os.environ.get(BACKUP_ENCRYPTION_ENABLED) or "").strip().lower() in _TRUTHY


def _load_encryption_key(required: bool = False) -> Optional[bytes]:
    """Load the AES-256 key from BACKUP_ENCRYPTION_KEY_FILE.

    The key is only ever read from that file: never from the environment, never
    from argv, and it is never logged or persisted anywhere. Returns None when
    no key is configured and encryption is not required. When `required` is set
    a missing/unreadable/wrong-sized key raises instead of silently degrading
    to an unencrypted backup.
    """
    key_file = os.environ.get(BACKUP_ENCRYPTION_KEY_FILE)
    if not key_file:
        if required:
            raise RuntimeError(
                f"{BACKUP_ENCRYPTION_ENABLED} is set but "
                f"{BACKUP_ENCRYPTION_KEY_FILE} is not configured"
            )
        return None
    key_path = Path(key_file)
    if not key_path.is_file():
        if required:
            raise RuntimeError(f"encryption key file not found: {key_file}")
        log("warn", f"encryption key file not found: {key_file}")
        return None
    try:
        raw = key_path.read_bytes()
    except OSError as exc:
        if required:
            raise RuntimeError(f"could not read encryption key file: {exc}") from exc
        log("warn", f"could not read encryption key file {key_file}: {exc}")
        return None
    # Accept a raw 32-byte key, or a text file whose only extra byte is a
    # trailing newline from a secret store / `echo` invocation.
    for candidate in (raw, raw.strip()):
        if len(candidate) == _AES_KEY_BYTES:
            return candidate
    message = (
        f"encryption key file {key_file} must contain exactly "
        f"{_AES_KEY_BYTES} bytes (got {len(raw)})"
    )
    if required:
        raise RuntimeError(message)
    log("warn", message)
    return None


def _gcm(key: bytes, nonce: bytes, tag: Optional[bytes] = None):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != _AES_KEY_BYTES:
        raise ValueError(f"AES-256-GCM requires a {_AES_KEY_BYTES}-byte key")
    if len(nonce) != _GCM_NONCE_BYTES:
        raise ValueError(f"AES-GCM nonce must be {_GCM_NONCE_BYTES} bytes")
    mode = modes.GCM(nonce) if tag is None else modes.GCM(nonce, tag)
    return Cipher(algorithms.AES(key), mode)


class _EncryptingWriter:
    """File-like sink that pipes writes into a GCM encryptor."""

    def __init__(self, handle, encryptor) -> None:
        self._handle = handle
        self._encryptor = encryptor

    def write(self, data) -> int:
        if not data:
            return 0
        self._handle.write(self._encryptor.update(data))
        return len(data)

    def flush(self) -> None:
        self._handle.flush()


def _gzip_stream(raw: Path, archive: Path) -> None:
    """Stream-gzip the dump with flat memory usage."""
    with open(raw, "rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            dst.write(chunk)


def _gzip_encrypt_stream(raw: Path, archive: Path, key: bytes) -> None:
    """gzip then AES-256-GCM encrypt in bounded memory.

    Container layout (stable, versioned):
        b"\\x01" || nonce(12) || ciphertext || tag(16)
    The version byte distinguishes an encrypted container from a plain gzip
    member, which lets the restore path auto-detect either form.
    """
    nonce = os.urandom(_GCM_NONCE_BYTES)
    encryptor = _gcm(key, nonce).encryptor()
    with open(raw, "rb") as src, open(archive, "wb") as dst:
        dst.write(bytes([_ARCHIVE_FORMAT_VERSION]))
        dst.write(nonce)
        with gzip.GzipFile(fileobj=_EncryptingWriter(dst, encryptor),
                           mode="wb", compresslevel=6) as gz:
            for chunk in iter(lambda: src.read(CHUNK), b""):
                gz.write(chunk)
        tail = encryptor.finalize()
        if tail:
            dst.write(tail)
        dst.write(encryptor.tag)


def compress_archive(raw: Path, archive: Path,
                     key: Optional[bytes] = None) -> Tuple[str, str, int, int]:
    """Stream the dump into an archive, optionally AES-256-GCM encrypting it.

    Returns (content_sha256, archive_sha256, archive_bytes, content_bytes).
    `content_sha256` is always the SHA-256 of the original uncompressed SQL -
    that is the contract consumed by scripts/pg_restore_drill.py and by the
    manifest's `checksum_sha256`. Memory stays bounded: the dump is never
    materialised in full, so production-sized dumps are safe.
    """
    if key is None:
        key = _load_encryption_key(required=encryption_required())

    content = hashlib.sha256()
    content_bytes = 0
    with open(raw, "rb") as src:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            content.update(chunk)
            content_bytes += len(chunk)

    if key is None:
        _gzip_stream(raw, archive)
    else:
        _gzip_encrypt_stream(raw, archive, key)

    return (content.hexdigest(), file_sha256(archive),
            archive.stat().st_size, content_bytes)


def archive_is_encrypted(archive: Path) -> bool:
    """True when the archive uses the encrypted container format."""
    try:
        with open(archive, "rb") as handle:
            return handle.read(1) == bytes([_ARCHIVE_FORMAT_VERSION])
    except OSError:
        return False


def _iter_encrypted_sql(archive: Path, key: bytes):
    """Yield the original SQL from an encrypted archive, bounded memory.

    The GCM tag is read from the tail of the file so the ciphertext body can be
    streamed and decrypted incrementally. A wrong key or any modification makes
    `finalize()` raise InvalidTag, so tampering cannot pass unnoticed.
    """
    size = archive.stat().st_size
    header = 1 + _GCM_NONCE_BYTES
    if size < header + _GCM_TAG_BYTES:
        raise ValueError("encrypted archive is truncated")
    with open(archive, "rb") as handle:
        if handle.read(1) != bytes([_ARCHIVE_FORMAT_VERSION]):
            raise ValueError("unsupported encrypted archive format version")
        nonce = handle.read(_GCM_NONCE_BYTES)
        handle.seek(size - _GCM_TAG_BYTES)
        tag = handle.read(_GCM_TAG_BYTES)
        handle.seek(header)
        try:
            decryptor = _gcm(key, nonce, tag).decryptor()
        except ValueError as exc:
            raise ValueError(f"invalid encryption key: {exc}") from exc
        inflater = zlib.decompressobj(_GZIP_WBITS)
        inflate_error: Optional[zlib.error] = None
        remaining = size - _GCM_TAG_BYTES - header
        while remaining > 0:
            block = handle.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            plain = decryptor.update(block)
            if plain:
                try:
                    out = inflater.decompress(plain)
                except zlib.error as exc:
                    # Wrong key or tampered ciphertext. Keep going so the GCM
                    # tag below still produces the authoritative verdict.
                    inflate_error = exc
                    break
                if out:
                    yield out
        try:
            plain = decryptor.finalize()
        except Exception as exc:  # noqa: BLE001 - InvalidTag: wrong key or tampered
            raise ValueError(
                f"archive decryption failed (wrong key or tampered archive): {exc}"
            ) from exc
        if inflate_error is not None:
            raise ValueError(
                "archive decryption failed (wrong key or tampered archive): "
                f"{inflate_error}"
            )
        if plain:
            try:
                out = inflater.decompress(plain)
            except zlib.error as exc:
                raise ValueError(
                    f"archive decompression failed (wrong key or tampered archive): {exc}"
                ) from exc
            if out:
                yield out
        try:
            out = inflater.flush()
        except zlib.error as exc:
            raise ValueError(
                f"archive decompression failed (wrong key or tampered archive): {exc}"
            ) from exc
        if out:
            yield out


def _iter_archive_sql(archive: Path, key: Optional[bytes] = None):
    """Yield the original SQL bytes from a plain or encrypted archive."""
    if archive_is_encrypted(archive):
        if key is None:
            key = _load_encryption_key(required=True)
        yield from _iter_encrypted_sql(archive, key)
        return
    with open(archive, "rb") as probe:
        magic = probe.read(2)
    if magic != b"\x1f\x8b":
        raise ValueError(
            f"unrecognised archive format (magic={magic!r}); expected gzip or "
            f"encrypted container version {_ARCHIVE_FORMAT_VERSION}"
        )
    with gzip.open(archive, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            yield chunk


class _DecryptingStream(io.RawIOBase):
    """Read-only file-like view over an iterator of plaintext SQL chunks."""

    def __init__(self, chunks) -> None:
        self._chunks = chunks
        self._buf = b""
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:  # type: ignore[override]
        while not self._buf and not self._done:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                self._done = True
        if not self._buf:
            return 0
        size = min(len(target), len(self._buf))
        target[:size] = self._buf[:size]
        self._buf = self._buf[size:]
        return size


def open_archive_sql(archive: Path, key: Optional[bytes] = None):
    """Return a binary file object over the original SQL inside the archive.

    Handles both the plain gzip and the encrypted container, so the restore
    drill works without knowing in advance which form it received.
    """
    return io.BufferedReader(_DecryptingStream(_iter_archive_sql(archive, key)))


def build_manifest(*, stamp: str, moment: dt.datetime, archive_name: str,
                   content_sha256: str, archive_sha256: str, archive_bytes: int,
                   content_bytes: int, tables: List[str], counts: Dict[str, int],
                   dump_version: str, daily_keep: int, monthly_keep: int,
                   encryption_key_version: str = "v1",
                   encryption_enabled: bool = False) -> Dict:
    manifest = {
        # `checksum_sha256` is the SHA-256 of the DECOMPRESSED SQL: it is the
        # contract consumed by scripts/pg_restore_drill.py and must not change.
        "backup": archive_name,
        "checksum_sha256": content_sha256,
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "content_bytes": content_bytes,
        "created_utc": stamp,
        "created_utc_iso": moment.astimezone(dt.timezone.utc).isoformat(),
        "engine": "postgresql",
        "pg_dump_version": dump_version,
        "tables": len(tables),
        "row_counts": counts,
        "retention": {"daily_keep": daily_keep, "monthly_keep": monthly_keep},
        "total_rows": sum(v for v in counts.values() if v > 0),
    }

    if encryption_enabled:
        # Metadata only. The key, the nonce and the tag all stay out of the
        # manifest: the nonce/tag live in the container header/tail.
        manifest["encryption"] = {
            "enabled": True,
            "algo": "aes-256-gcm",
            "key_version": encryption_key_version,
        }

    return manifest


# --------------------------------------------------------------------------- #
# stage 5 - integrity
# --------------------------------------------------------------------------- #
def verify_archive(archive: Path, content_sha256: str, archive_sha256: str,
                   key: Optional[bytes] = None,
                   expect_encrypted: Optional[bool] = None) -> None:
    """Raise AssertionError unless the archive is intact and byte-faithful.

    Works for both plain gzip and encrypted archives; for an encrypted archive
    the key must be available or loadable, otherwise this fails closed.
    """
    if file_sha256(archive) != archive_sha256:
        raise AssertionError("archive sha256 mismatch after publish")

    encrypted = archive_is_encrypted(archive)
    if expect_encrypted is not None and encrypted != expect_encrypted:
        raise AssertionError(
            f"archive encryption mismatch: expected {expect_encrypted}, got {encrypted}"
        )
    if encrypted and key is None:
        key = _load_encryption_key(required=True)

    content = hashlib.sha256()
    for chunk in _iter_archive_sql(archive, key):
        content.update(chunk)
    if content.hexdigest() != content_sha256:
        raise AssertionError("gzip round-trip sha256 mismatch")


# --------------------------------------------------------------------------- #
# stage 6 - retention
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackupRecord:
    stamp: str
    created_utc: Optional[dt.datetime]
    archive: Optional[Path]
    manifest: Optional[Path]

    @property
    def complete(self) -> bool:
        return self.archive is not None and self.manifest is not None


def parse_stamp(stamp: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(
            tzinfo=dt.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def list_backups(outdir: Path) -> List[BackupRecord]:
    """Every artifact in `outdir`, paired by stamp (orphans included)."""
    outdir = Path(outdir)
    archives: Dict[str, Path] = {}
    manifests: Dict[str, Path] = {}
    if not outdir.is_dir():
        return []
    for path in outdir.iterdir():
        name = path.name
        if not name.startswith(BACKUP_PREFIX):
            continue
        if name.endswith(MANIFEST_SUFFIX):
            manifests[name[len(BACKUP_PREFIX):-len(MANIFEST_SUFFIX)]] = path
        elif name.endswith(ARCHIVE_SUFFIX):
            archives[name[len(BACKUP_PREFIX):-len(ARCHIVE_SUFFIX)]] = path
    records = []
    for stamp in set(archives) | set(manifests):
        records.append(BackupRecord(
            stamp=stamp,
            created_utc=parse_stamp(stamp),
            archive=archives.get(stamp),
            manifest=manifests.get(stamp),
        ))
    return sorted(records, key=lambda r: (r.created_utc is None, r.stamp))


def _month_key(moment: dt.datetime) -> Tuple[int, int]:
    utc = moment.astimezone(dt.timezone.utc)
    return utc.year, utc.month


def select_retention(
    records: List[BackupRecord],
    daily_keep: int = DAILY_KEEP,
    monthly_keep: int = MONTHLY_KEEP,
) -> Tuple[List[BackupRecord], List[BackupRecord]]:
    """Split records into (keep, drop) per the documented retention rule.

    1. The newest `daily_keep` records are always kept (daily window).
    2. Walking the remainder newest-first, the first record of each calendar
       month (UTC) is kept as that month's recovery point, up to `monthly_keep`
       months. Months already represented in the daily window do not consume a
       monthly slot and add no duplicate point.
    3. Records with an unparseable stamp are always kept (never auto-deleted).
    """
    daily_keep = max(1, int(daily_keep))
    monthly_keep = max(0, int(monthly_keep))
    dated = sorted(
        (r for r in records if r.created_utc is not None),
        key=lambda r: r.created_utc, reverse=True,
    )
    keep: List[BackupRecord] = [r for r in records if r.created_utc is None]
    covered: set = set()
    months_kept = 0
    for index, record in enumerate(dated):
        if index < daily_keep:
            keep.append(record)
            covered.add(_month_key(record.created_utc))
            continue
        key = _month_key(record.created_utc)
        if key in covered:
            continue
        if months_kept < monthly_keep:
            keep.append(record)
            covered.add(key)
            months_kept += 1
    keep_ids = {id(r) for r in keep}
    drop = [r for r in dated if id(r) not in keep_ids]
    return keep, drop


def prune_backups(outdir: Path, drop: List[BackupRecord]) -> List[Path]:
    """Delete dropped records, archive and manifest together."""
    removed: List[Path] = []
    for record in drop:
        for path in (record.archive, record.manifest):
            if path is None:
                continue
            try:
                path.unlink()
                removed.append(path)
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - filesystem failure
                log("retention", f"could not delete {path.name}: {type(exc).__name__}")
    return removed


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run_backup(
    outdir: Path,
    *,
    now: Optional[dt.datetime] = None,
    daily_keep: int = DAILY_KEEP,
    monthly_keep: int = MONTHLY_KEEP,
    connect: Optional[Callable[..., object]] = None,
    dump_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    tool: Optional[Path] = None,
    stale_lock_seconds: int = STALE_LOCK_SECONDS,
) -> int:
    """Run one backup. Returns 0 on success, non-zero on failure."""
    moment = (now or utcnow()).astimezone(dt.timezone.utc)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    connect = connect or default_connect
    dsn = dsn_env()
    password = os.environ.get("DB_PASSWORD", "")
    dump_version = ""

    lock = acquire_lock(outdir, now=moment, stale_seconds=stale_lock_seconds)
    if lock is None:
        log("lock", "another backup run holds the lock; skipping this run")
        return 0

    stamp = moment.strftime("%Y%m%d_%H%M%S")
    base = f"{BACKUP_PREFIX}{stamp}"
    raw = outdir / f".{base}.sql.part"
    archive = outdir / f"{base}{ARCHIVE_SUFFIX}"
    archive_part = outdir / f".{base}{ARCHIVE_SUFFIX}.part"
    manifest_path = outdir / f"{base}{MANIFEST_SUFFIX}"
    manifest_part = outdir / f".{base}{MANIFEST_SUFFIX}.part"
    verified = False

    try:
        log("start", f"backup run for {dsn['PGHOST']}:{dsn['PGPORT']}/{dsn['PGDATABASE']}"
                      f" -> {outdir}")

        log("1/6", "row-count snapshot")
        tables, counts = snapshot_row_counts(dsn, connect)
        log("1/6", f"{len(tables)} tables, total_rows="
                    f"{sum(v for v in counts.values() if v > 0)}")

        log("2/6", "pg_dump (credentials via PGPASSFILE)")
        try:
            tool = tool or find_tool("pg_dump")
        except RuntimeError as exc:
            log("2/6", f"FAILED {exc}")
            return 1
        proc = run_pg_dump(raw, dsn=dsn, password=password, tool=tool,
                           runner=dump_runner)
        if proc.returncode != 0:
            log("2/6", f"FAILED exit={proc.returncode} "
                       f"stderr={_first_lines(getattr(proc, 'stderr', None))}")
            return 1
        if not raw.exists() or raw.stat().st_size == 0:
            log("2/6", "FAILED pg_dump produced an empty dump file")
            return 1
        dump_version = pg_dump_version(tool, dump_runner)
        log("2/6", f"dump exit=0 size={raw.stat().st_size} bytes"
                    f"{' (' + dump_version + ')' if dump_version else ''}")

        log("3/6", "gzip + manifest")
        # Fail closed: when BACKUP_ENCRYPTION_ENABLED is set, a missing or
        # invalid key aborts the backup instead of silently writing plaintext.
        enc_key = _load_encryption_key(required=encryption_required())
        content_sha, archive_sha, archive_bytes, content_bytes = compress_archive(
            raw, archive_part, key=enc_key
        )
        verify_archive(archive_part, content_sha, archive_sha, key=enc_key)
        manifest = build_manifest(
            stamp=stamp, moment=moment, archive_name=archive.name,
            content_sha256=content_sha, archive_sha256=archive_sha,
            archive_bytes=archive_bytes, content_bytes=content_bytes,
            tables=tables, counts=counts, dump_version=dump_version,
            daily_keep=daily_keep, monthly_keep=monthly_keep,
            encryption_key_version="v1",
            encryption_enabled=enc_key is not None,
        )
        manifest_part.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(archive_part, archive)
        os.replace(manifest_part, manifest_path)
        _discard(raw)
        log("3/6", f"published {archive.name} "
                   f"({archive_bytes} bytes, sha256={archive_sha[:16]}...) "
                   f"+ {manifest_path.name}")

        log("4/6", "integrity verification")
        verify_archive(archive, content_sha, archive_sha, key=enc_key)
        log("4/6", "gzip round-trip + sha256 OK")
        verified = True

        log("5/6", "retention (only after a verified backup exists)")
        records = list_backups(outdir)
        keep, drop = select_retention(records, daily_keep, monthly_keep)
        orphans = [r for r in records if not r.complete]
        for orphan in orphans:
            log("5/6", f"incomplete artifact left untouched (review manually): "
                       f"{orphan.stamp}")
        protected_ids = {id(r) for r in keep}
        drop = [r for r in drop if id(r) not in protected_ids and r.complete]
        removed = prune_backups(outdir, drop)
        log("5/6", f"kept={len(keep)} daily_keep={daily_keep} "
                   f"monthly_keep={monthly_keep} removed={len(removed)}")

        log("6/6", f"SUCCESS backup={archive.name} "
                   f"tables={len(tables)} "
                   f"total_rows={sum(v for v in counts.values() if v > 0)}")
        return 0

    except Exception as exc:  # noqa: BLE001 - never leave a half-written backup
        log("error", f"backup FAILED: {type(exc).__name__}: {exc}")
        _discard(raw, archive_part, manifest_part)
        # A published-but-unverified archive must never be treated as a
        # recovery point, so the pair is removed together. Every earlier,
        # already-verified backup is left completely untouched. Once the new
        # archive verified OK it is kept even if a later (retention) step
        # fails, because the recovery point itself is sound.
        if not verified and (archive.exists() or manifest_path.exists()):
            _discard(archive, manifest_path)
        return 1
    finally:
        release_lock(lock)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AUSTRO AI PostgreSQL backup (one-shot)")
    parser.add_argument("outdir", nargs="?", default=None,
                        help="destination directory (default: $BACKUP_OUTDIR or ./backups)")
    parser.add_argument("--outdir", dest="outdir_opt", default=None)
    parser.add_argument("--engine", default="postgresql",
                        help="accepted for runbook compatibility; only "
                             "postgresql is supported")
    args = parser.parse_args(argv)
    if args.engine != "postgresql":
        print(f"unsupported engine: {args.engine}", file=sys.stderr)
        return 2
    outdir = Path(
        args.outdir_opt or args.outdir
        or os.environ.get("BACKUP_OUTDIR", "backups")
    )
    daily = int(os.environ.get("BACKUP_DAILY_KEEP", DAILY_KEEP))
    monthly = int(os.environ.get("BACKUP_MONTHLY_KEEP", MONTHLY_KEEP))
    return run_backup(outdir, daily_keep=daily, monthly_keep=monthly)


if __name__ == "__main__":
    sys.exit(main())
