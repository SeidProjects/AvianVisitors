#!/usr/bin/env python3
"""Optional bounded audio/image synchronization; no source or remote deletion.

The library joins the caller's lock, SQLite connection and SDK attempt guard.
The CLI acquires its own state lock; scheduling is explicitly opt-in. Secrets
are consumed by the SDK factory and are never part of Config or public receipts.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

MIB = 1024 * 1024
GIB = 1024 * MIB
TYPES = {'.mp3': ('audio', '.mp3', 'audio/mpeg'),
         '.png': ('images', '.png', 'image/png'),
         '.jpg': ('images', '.jpg', 'image/jpeg'),
         '.jpeg': ('images', '.jpg', 'image/jpeg'),
         '.webp': ('images', '.webp', 'image/webp')}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
MAX_DIRECTORY_ENTRIES = 100000
MAX_DEPTH = 16


class ConfigError(ValueError):
    pass


class UnsafeSource(ValueError):
    pass


class BudgetStop(RuntimeError):
    """Fatal cooperative stop; callers must not retry it inside a batch."""


class MediaPause(BudgetStop):
    """Only the media phase used its allotted share; later phases may continue."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.port is not None or parsed.path not in ('', '/') or
            parsed.query or parsed.fragment):
        raise ConfigError('an HTTPS origin without credentials or a path is required')
    return 'https://' + parsed.netloc


def _canonical(path, existing=True):
    try:
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or path != path.resolve(strict=existing):
            raise ConfigError('an absolute resolved directory is required')
        if existing and not stat.S_ISDIR(path.lstat().st_mode):
            raise ConfigError('configured root is not a directory')
    except (TypeError, OSError, RuntimeError) as error:
        raise ConfigError('an absolute resolved directory is required') from error
    return path


def _root_id(path):
    if path != path.resolve(strict=True):
        raise UnsafeSource('root alias changed')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeSource('root is no longer a directory')
    return [info.st_dev, info.st_ino, info.st_uid]


@dataclass(frozen=True)
class Config:
    endpoint_url: str
    bucket: str
    public_origin: str
    audio_root: Path
    images_root: Path
    state_dir: Path
    interval_seconds: int = 900
    backoff_seconds: int = 60
    batch_files: int = 24
    batch_bytes: int = 16 * MIB
    file_bytes: int = 8 * MIB
    scan_entries: int = 1024
    min_free_bytes: int = 3 * GIB // 2
    max_sources: int = 100000
    max_directories: int = 10000
    max_objects: int = 150000
    local_state_bytes: int = 128 * MIB
    image_roots: object = None
    max_remote_media_bytes: int = 8 * GIB
    remote_reserved_bytes: int = 0

    def __post_init__(self):
        for name in ('endpoint_url', 'public_origin'):
            object.__setattr__(self, name, _origin(getattr(self, name)))
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', self.bucket):
            raise ConfigError('invalid bucket name')
        object.__setattr__(self, 'audio_root', _canonical(self.audio_root))
        if self.image_roots is None:
            object.__setattr__(self, 'images_root', _canonical(self.images_root))
        else:
            if (self.images_root is not None or not isinstance(self.image_roots, dict) or
                    not 1 <= len(self.image_roots) <= 8 or
                    any(not isinstance(label, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,31}', label) or
                        label == 'audio' for label in self.image_roots)):
                raise ConfigError('image roots require one exclusive mapping of up to eight labels')
            object.__setattr__(self, 'image_roots', {label: _canonical(path) for label, path in self.image_roots.items()})
        object.__setattr__(self, 'state_dir', _canonical(self.state_dir, existing=False))
        paths = (*self.roots.values(), self.state_dir)
        for number, path in enumerate(paths):
            for other in paths[number + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    raise ConfigError('source and state roots must be disjoint')
        bounds = {'interval_seconds': (60, 86400), 'backoff_seconds': (1, 3600),
                  'batch_files': (1, 24), 'batch_bytes': (1, 16 * MIB),
                  'file_bytes': (1, 8 * MIB), 'scan_entries': (4, 1024),
                  'min_free_bytes': (16 * MIB, 64 * GIB),
                  'max_sources': (1, 1000000), 'max_directories': (2, 100000),
                  'max_objects': (1, 1000000), 'local_state_bytes': (MIB, 1024 * MIB),
                  'max_remote_media_bytes': (1, 1024 * GIB), 'remote_reserved_bytes': (0, 1024 * GIB)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError('invalid bounded sync setting: ' + name)
        if self.max_directories < len(self.roots) or self.remote_reserved_bytes > self.max_remote_media_bytes:
            raise ConfigError('configured catalog or remote reservation exceeds its cap')

    @property
    def roots(self):
        return dict(audio=self.audio_root, **(self.image_roots or {'images': self.images_root}))

    @property
    def media_origin(self):
        return self.public_origin

    @property
    def remote_media_bytes(self):
        return self.max_remote_media_bytes

    @classmethod
    def from_env(cls, environment, *, require_credentials=True):
        required = ('ENDPOINT_URL', 'BUCKET', 'PUBLIC_ORIGIN', 'AUDIO_ROOT', 'STATE_DIR')
        # Accept the original plural spelling; new examples use IMAGE_ROOT.
        environment = dict(environment)
        if 'AVIAN_SYNC_IMAGE_ROOT' not in environment and 'AVIAN_SYNC_IMAGES_ROOT' in environment:
            environment['AVIAN_SYNC_IMAGE_ROOT'] = environment['AVIAN_SYNC_IMAGES_ROOT']
        single = environment.get('AVIAN_SYNC_IMAGE_ROOT')
        multiple = environment.get('AVIAN_SYNC_IMAGE_ROOTS')
        present = [bool(environment.get('AVIAN_SYNC_' + key)) for key in required] + [bool(single or multiple)]
        enabled = environment.get('AVIAN_SYNC_ENABLED')
        if enabled == '0':
            return None
        if enabled is None and not any(present):
            return None
        if enabled not in (None, '1'):
            raise ConfigError('AVIAN_SYNC_ENABLED must be 0 or 1')
        if not all(present):
            raise ConfigError('enabled sync requires complete server configuration')
        if single and multiple:
            raise ConfigError('choose IMAGE_ROOT or IMAGE_ROOTS, not both')
        if require_credentials and any(not environment.get('AVIAN_SYNC_' + key)
                                       for key in ('ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')):
            raise ConfigError('enabled sync requires explicit server credentials')
        values = {key.lower(): environment['AVIAN_SYNC_' + key] for key in required}
        values['images_root'] = single or None
        if multiple:
            try:
                values['image_roots'] = json.loads(multiple)
            except (TypeError, ValueError) as error:
                raise ConfigError('invalid image roots JSON mapping') from error
        for key in ('INTERVAL_SECONDS', 'BACKOFF_SECONDS', 'BATCH_FILES', 'BATCH_BYTES',
                    'FILE_BYTES', 'SCAN_ENTRIES', 'MIN_FREE_BYTES', 'MAX_SOURCES',
                    'MAX_DIRECTORIES', 'MAX_OBJECTS', 'LOCAL_STATE_BYTES',
                    'MAX_REMOTE_MEDIA_BYTES', 'REMOTE_RESERVED_BYTES'):
            if 'AVIAN_SYNC_' + key in environment:
                try:
                    values[key.lower()] = int(environment['AVIAN_SYNC_' + key])
                except (TypeError, ValueError) as error:
                    raise ConfigError('invalid numeric sync setting') from error
        return cls(**values)


def load_env_file(path):
    """Literal private dotenv values; no interpolation, execution or env dump."""
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid()) or
            info.st_mode & 0o077 or info.st_size > 65536):
        raise ConfigError('private env file must be a small owner-only regular file')
    fd = os.open(path, FILE_FLAGS)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ConfigError('private env file changed')
        text = handle.read(65537)
    if len(text.encode()) > 65536:
        raise ConfigError('private env file exceeds limit')
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch('[A-Z][A-Z0-9_]*', key):
            raise ConfigError('invalid literal env entry')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError('unterminated literal env value')
            value = value[1:-1]
        if key.startswith('AVIAN_SYNC_'):
            values[key] = value
    return values


def local_resources(state):
    filesystem = os.statvfs(state)
    memory = None
    path = Path('/proc/meminfo')
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('MemAvailable:'):
                values = line.split()
                if len(values) != 3 or values[2] != 'kB':
                    raise BudgetStop('available-memory evidence invalid')
                memory = int(values[1]) * 1024
                break
        if memory is None:
            raise BudgetStop('available-memory evidence missing')
    return filesystem.f_bavail * filesystem.f_frsize, memory


class SyncGuard:
    def __init__(self, config, *, clock=time.monotonic, sleep=time.sleep, resources=None):
        self.config, self.clock, self.sleep = config, clock, sleep
        self.resources = resources or (lambda: local_resources(config.state_dir))
        self.started = clock()
        self.attempts = self.selected_files = self.selected_bytes = 0
        self.released_bytes = 0
        self.next_release = self.started
        self.next_resource_check = self.started
        self.phase_attempt_limit, self.phase_seconds = 150, 120
        self.hook_id = 'avian-media-sync-' + uuid.uuid4().hex
        self.client = None

    def check(self):
        if self.clock() - self.started >= 120:
            raise BudgetStop('sync batch exceeded 120 seconds')
        if self.clock() >= self.next_resource_check:
            resources = self.resources()
            disk, memory = resources if isinstance(resources, tuple) else (resources, None)
            if disk < self.config.min_free_bytes:
                raise BudgetStop('sync free-disk floor breached')
            if memory is not None and memory < 512 * MIB:
                raise BudgetStop('sync available-memory floor breached')
            self.next_resource_check = self.clock() + 0.25
        if self.clock() - self.started >= self.phase_seconds:
            raise MediaPause('media time share consumed')

    def before_request(self, method, key, body_bytes=0):
        self.check()  # SDK before-send alone counts actual transport attempts.
        if isinstance(body_bytes, bool) or not isinstance(body_bytes, int) or body_bytes < 0:
            raise ValueError('invalid request body size')
        if self.attempts >= 150:
            raise BudgetStop('sync batch exhausted 150 attempts')
        if self.attempts >= self.phase_attempt_limit:
            raise MediaPause('media request share consumed')

    def before_send(self, **_):
        self.check()
        if self.attempts >= 150:
            raise BudgetStop('sync batch exhausted 150 attempts')
        if self.attempts >= self.phase_attempt_limit:
            raise MediaPause('media request share consumed')
        self.attempts += 1

    def attach(self, client):
        if self.client is not None:
            if self.client is not client:
                raise ValueError('guard already belongs to another client')
            return
        client.meta.events.register('before-send.s3', self.before_send, unique_id=self.hook_id)
        self.client = client

    def detach(self):
        if self.client is not None:
            self.client.meta.events.unregister('before-send.s3', unique_id=self.hook_id)
            self.client = None

    def admit(self, size):
        self.check()
        if (size < 1 or size > self.config.file_bytes or self.selected_files >= self.config.batch_files or
                self.selected_bytes + size > self.config.batch_bytes):
            return False
        self.selected_files += 1
        self.selected_bytes += size
        return True

    def wrap_body(self, body):
        return PayloadReader(body, self)

    def check_state_budget(self, ledger, *, additional_bytes=0):
        return check_state_budget(self.config, ledger, self, additional_bytes=additional_bytes)

    @contextmanager
    def media_share(self, reserve_attempts, reserve_seconds):
        if (isinstance(reserve_attempts, bool) or not isinstance(reserve_attempts, int) or not 0 <= reserve_attempts < 150 or
                isinstance(reserve_seconds, bool) or not isinstance(reserve_seconds, int) or not 0 <= reserve_seconds < 120):
            raise ValueError('invalid publisher budget reservation')
        previous = self.phase_attempt_limit, self.phase_seconds
        self.phase_attempt_limit, self.phase_seconds = 150 - reserve_attempts, 120 - reserve_seconds
        try:
            yield
        finally:
            self.phase_attempt_limit, self.phase_seconds = previous


class PayloadReader:
    """Shared 512 KiB/s upload-body release, including SDK checksum rereads.

    This is not a cap on TLS/HTTP/GET/HEAD transport bytes. Seek preserves payload.
    External process CPU/RAM/time controls remain the operator's responsibility.
    """
    def __init__(self, body, guard):
        self.body, self.guard = body, guard

    def read(self, amount=-1):
        self.guard.check()
        raw = self.body.read(amount)
        if raw:
            now = self.guard.clock()
            self.guard.next_release = max(now, self.guard.next_release) + len(raw) / 524288
            self.guard.sleep(max(0, self.guard.next_release - now))
            self.guard.released_bytes += len(raw)
            self.guard.check()
        return raw

    def readinto(self, buffer):
        raw = self.read(len(buffer))
        buffer[:len(raw)] = raw
        return len(raw)

    def seek(self, *args):
        return self.body.seek(*args)

    def tell(self):
        return self.body.tell()

    def readable(self):
        return True

    def seekable(self):
        return True

    @property
    def closed(self):
        return self.body.closed


def make_client(config, *, credentials):
    """Construct without cloud calls/default credential discovery/SDK retries."""
    import boto3
    from botocore.config import Config as SDKConfig
    if any(not credentials.get('AVIAN_SYNC_' + key) for key in ('ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')):
        raise ConfigError('explicit server credentials are required')
    os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
    return boto3.session.Session().client(
        's3', endpoint_url=config.endpoint_url, region_name='auto',
        aws_access_key_id=credentials['AVIAN_SYNC_ACCESS_KEY_ID'],
        aws_secret_access_key=credentials['AVIAN_SYNC_SECRET_ACCESS_KEY'],
        aws_session_token=credentials.get('AVIAN_SYNC_SESSION_TOKEN'),
        config=SDKConfig(connect_timeout=3, read_timeout=5, max_pool_connections=1,
                         retries={'total_max_attempts': 1, 'mode': 'standard'},
                         s3={'addressing_style': 'path'}))


@contextmanager
def state_lock(config):
    state = config.state_dir
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _canonical(state) != state:
        raise ConfigError('state path changed')
    info = state.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ConfigError('state directory must be owner-only')
    fd = os.open(state / 'publisher.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ConfigError('unsafe publisher lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _identity(config):
    return _json([config.endpoint_url, config.bucket, config.public_origin,
                  {kind: [str(root), _root_id(root)] for kind, root in config.roots.items()},
                  [str(config.state_dir), _root_id(config.state_dir)], config.remote_reserved_bytes])


def _bound(config, ledger):
    paths = [row[2] for row in ledger.execute('PRAGMA database_list') if row[1] == 'main']
    if len(paths) != 1 or Path(paths[0]).resolve() != config.state_dir / 'ledger.sqlite':
        raise ConfigError('shared media ledger must be in the configured state')
    row = ledger.execute('SELECT identity FROM media_sync_target WHERE id=1').fetchone()
    if row != (_identity(config),):
        raise ConfigError('media ledger belongs to another target/source identity')


def check_state_budget(config, ledger=None, guard=None, *, additional_bytes=0):
    """Allocated state-tree + conservative SQLite journal/spool admission.

    Never prunes unknown files. Shared cache writers must call the same helper
    before their writes, reserving their candidate/BLOB size as additional_bytes.
    Unlinked spool bytes require an explicit reservation because scandir cannot
    observe them. The caller's lock serializes reservations/writes.
    """
    if isinstance(additional_bytes, bool) or not isinstance(additional_bytes, int) or additional_bytes < 0:
        raise ValueError('invalid state reservation')
    total = entries = 0
    device = config.state_dir.lstat().st_dev
    mounts = _mount_points()
    def walk(fd, depth, path):
        nonlocal total, entries
        info = os.fstat(fd)
        total += max(info.st_size, info.st_blocks * 512)
        with os.scandir(fd) as children:
            for child in children:
                if guard is not None:
                    guard.check()
                entries += 1
                if entries > 10000 or depth >= MAX_DEPTH:
                    raise BudgetStop('local state inventory exceeds bounds')
                info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                full = path / child.name
                if str(full) in mounts or os.path.ismount(full):
                    raise BudgetStop('local state contains a nested mount')
                if info.st_dev != device or (not stat.S_ISDIR(info.st_mode) and
                                              (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                    raise BudgetStop('local state contains unsafe aliases/nodes')
                if stat.S_ISDIR(info.st_mode):
                    inner = os.open(child.name, DIRECTORY_FLAGS, dir_fd=fd)
                    try:
                        walk(inner, depth + 1, full)
                    finally:
                        os.close(inner)
                else:
                    total += max(info.st_size, info.st_blocks * 512)
    with _directory(config.state_dir, '.') as fd:
        walk(fd, 0, config.state_dir)
    ledger_bytes = 0
    ledger_path = config.state_dir / 'ledger.sqlite'
    if ledger_path.exists():
        info = ledger_path.lstat()
        ledger_bytes = max(info.st_size, info.st_blocks * 512)
    # An upcoming transaction can journal the full current DB. A small fixed
    # reserve covers schema/pages/index growth; callers add bulk row/BLOB peaks.
    peak = total + ledger_bytes + 65536 + additional_bytes
    if peak > config.local_state_bytes:
        raise BudgetStop('local state byte cap would be exceeded')
    return dict(allocated_bytes=total, journal_reservation_bytes=ledger_bytes,
                additional_bytes=additional_bytes, peak_bound_bytes=peak,
                quota_bytes=config.local_state_bytes)


def open_ledger(config, *, outer_lock_held):
    if outer_lock_held is not True:
        raise ConfigError('existing publisher lock is required')
    _canonical(config.state_dir)
    check_state_budget(config, additional_bytes=256 * 1024)
    path = config.state_dir / 'ledger.sqlite'
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ConfigError('unsafe media ledger')
    ledger = sqlite3.connect(path)
    try:
        ledger.execute('PRAGMA synchronous=FULL')
        with ledger:
            ledger.execute('CREATE TABLE IF NOT EXISTS media_sync_target(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL)')
            row = ledger.execute('SELECT identity FROM media_sync_target WHERE id=1').fetchone()
            identity = _identity(config)
            if row is not None and row != (identity,):
                raise ConfigError('media ledger belongs to another target/source identity')
            ledger.execute('INSERT OR IGNORE INTO media_sync_target VALUES (1,?)', (identity,))
            ledger.execute('CREATE TABLE IF NOT EXISTS media_sync_dirs(kind TEXT,path TEXT,cursor INTEGER,ticket INTEGER,mtime INTEGER,PRIMARY KEY(kind,path))')
            ledger.execute('CREATE TABLE IF NOT EXISTS media_sync_sources(kind TEXT,path TEXT,fingerprint TEXT,size INTEGER,mtime INTEGER,status TEXT,key TEXT,sha256 TEXT,content_type TEXT,verified_at REAL,PRIMARY KEY(kind,path))')
            ledger.execute('CREATE TABLE IF NOT EXISTS media_sync_objects(key TEXT PRIMARY KEY,sha256 TEXT,size INTEGER,content_type TEXT,status TEXT,verified_at REAL)')
            ledger.execute('CREATE INDEX IF NOT EXISTS media_sync_pending ON media_sync_sources(status,mtime,kind,path)')
            ledger.execute('CREATE INDEX IF NOT EXISTS media_sync_directory_queue ON media_sync_dirs(ticket,kind,path)')
            ledger.execute('CREATE INDEX IF NOT EXISTS media_sync_directory_hot ON media_sync_dirs(mtime DESC,kind,path)')
            for kind in config.roots:
                ledger.execute('INSERT OR IGNORE INTO media_sync_dirs VALUES (?,?,0,0,0)', (kind, '.'))
        check_state_budget(config, ledger)
        return ledger
    except Exception:
        ledger.close()
        raise


def _parts(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value or len(path.parts) > MAX_DEPTH:
        raise UnsafeSource('invalid relative source path')
    return () if value == '.' else path.parts


@contextmanager
def _directory(root, relative, mounts=None):
    if root != root.resolve(strict=True):
        raise UnsafeSource('source root alias changed')
    mounts = _mount_points() if mounts is None else mounts
    fd = os.open(root, DIRECTORY_FLAGS)
    try:
        full = root
        for part in _parts(relative):
            full = full / part
            if str(full) in mounts or os.path.ismount(full):
                raise UnsafeSource('nested source mount is ineligible')
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _mount_points():
    path = Path('/proc/self/mountinfo')
    if not path.exists():
        return set()
    raw = path.read_text()
    if len(raw) > MIB:
        raise UnsafeSource('mount inventory exceeds bound')
    unescape = lambda value: re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
    return {unescape(line.split()[4]) for line in raw.splitlines()}


def _fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink]


def _safe_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeSource('linked/special source is ineligible')


def _category(label):
    return 'audio' if label == 'audio' else 'images'


def _catalog_file(config, ledger, kind, path, info, counts):
    _safe_file(info)
    media_type = TYPES.get(PurePosixPath(path).suffix.lower())
    if media_type is None or media_type[0] != _category(kind):
        return 0
    fingerprint = _json(_fingerprint(info))
    row = ledger.execute('SELECT fingerprint FROM media_sync_sources WHERE kind=? AND path=?', (kind, path)).fetchone()
    if row == (fingerprint,):
        return 0
    if row is None:
        if counts['sources'] >= config.max_sources:
            raise BudgetStop('local source catalog cap consumed')
        counts['sources'] += 1
    status = 'pending' if 1 <= info.st_size <= config.file_bytes else 'ineligible'
    ledger.execute('INSERT OR REPLACE INTO media_sync_sources VALUES (?,?,?,?,?,?,NULL,NULL,?,NULL)',
                   (kind, path, fingerprint, info.st_size, info.st_mtime_ns, status, media_type[2]))
    return int(status == 'pending')


def _scan_directory(config, ledger, guard, row, limit, counts):
    kind, relative, cursor, ticket, _ = row
    processed = queued = unsafe = 0
    try:
        with _directory(config.roots[kind], relative, guard.source_mounts) as fd:
            info = os.fstat(fd)
            if info.st_dev != _root_id(config.roots[kind])[0]:
                raise UnsafeSource('source crosses a filesystem')
            total = 0
            completed = True
            with os.scandir(fd) as entries:
                for entry in entries:
                    guard.check()
                    total += 1
                    if total > MAX_DIRECTORY_ENTRIES:
                        raise UnsafeSource('source directory exceeds bounded traversal')
                    if total <= cursor:
                        continue
                    if processed >= limit:
                        completed = False
                        break
                    path = entry.name if relative == '.' else relative + '/' + entry.name
                    try:
                        _parts(path)
                        if str(config.roots[kind] / path) in guard.source_mounts:
                            raise UnsafeSource('nested source mount is ineligible')
                        child = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        if child.st_dev != info.st_dev:
                            raise UnsafeSource('source crosses a filesystem')
                        if stat.S_ISDIR(child.st_mode):
                            exists = ledger.execute('SELECT 1 FROM media_sync_dirs WHERE kind=? AND path=?', (kind, path)).fetchone()
                            if not exists:
                                if counts['directories'] >= config.max_directories:
                                    raise BudgetStop('local directory catalog cap consumed')
                                counts['directories'] += 1
                            ledger.execute('INSERT OR IGNORE INTO media_sync_dirs VALUES (?,?,0,0,?)', (kind, path, child.st_mtime_ns))
                        else:
                            queued += _catalog_file(config, ledger, kind, path, child, counts)
                    except (UnsafeSource, OSError):
                        unsafe += 1
                    processed += 1
                    cursor += 1
            next_ticket = ledger.execute('SELECT COALESCE(MAX(ticket),0)+1 FROM media_sync_dirs').fetchone()[0]
            ledger.execute('UPDATE media_sync_dirs SET cursor=?,ticket=?,mtime=? WHERE kind=? AND path=?',
                           (0 if completed else cursor, next_ticket if completed else ticket, info.st_mtime_ns, kind, relative))
    except (UnsafeSource, OSError):
        unsafe += 1
        next_ticket = ledger.execute('SELECT COALESCE(MAX(ticket),0)+1 FROM media_sync_dirs').fetchone()[0]
        ledger.execute('UPDATE media_sync_dirs SET cursor=0,ticket=? WHERE kind=? AND path=?', (next_ticket, kind, relative))
    return processed, queued, unsafe


def discover(config, ledger, guard, *, outer_lock_held):
    """Bounded hot + fair history traversal; no file-byte reads or network.

    Scandir offsets are not stable under concurrent directory edits. Repeated
    fair sweeps repair skips; exact file fingerprints gate uploads/associations.
    A known large directory may require bounded prefix enumeration to resume.
    """
    if outer_lock_held is not True:
        raise ConfigError('existing publisher lock is required')
    _bound(config, ledger)
    guard.check()
    guard.source_mounts = _mount_points()
    counts = {name: ledger.execute('SELECT COUNT(*) FROM media_sync_' + table).fetchone()[0]
              for name, table in (('sources', 'sources'), ('directories', 'dirs'))}
    if counts['sources'] > config.max_sources or counts['directories'] > config.max_directories:
        raise BudgetStop('local catalog is already beyond its cap')
    check_state_budget(config, ledger, guard, additional_bytes=config.scan_entries * 16384)
    totals = [0, 0, 0]
    hot_limit = config.scan_entries // 4
    with ledger:
        # Refresh root mtimes without scanning the full archive.
        for kind, root in config.roots.items():
            ledger.execute('UPDATE media_sync_dirs SET mtime=? WHERE kind=? AND path=?', (root.stat().st_mtime_ns, kind, '.'))
        cutoff = ledger.execute('SELECT COALESCE(MAX(ticket),0) FROM media_sync_dirs').fetchone()[0]
        hot = ledger.execute('SELECT * FROM media_sync_dirs ORDER BY mtime DESC,kind,path LIMIT 1').fetchone()
        if hot:
            result = _scan_directory(config, ledger, guard, hot, hot_limit, counts)
            totals = [a + b for a, b in zip(totals, result)]
        remaining = config.scan_entries - totals[0]
        while remaining > 0:
            guard.check()
            row = ledger.execute('SELECT * FROM media_sync_dirs WHERE ticket<=? ORDER BY ticket,kind,path LIMIT 1', (cutoff,)).fetchone()
            if row is None:
                break
            result = _scan_directory(config, ledger, guard, row, remaining, counts)
            totals = [a + b for a, b in zip(totals, result)]
            remaining -= result[0]
    check_state_budget(config, ledger, guard)
    return dict(scanned=totals[0], queued=totals[1], unsafe=totals[2], **counts)


def _not_found(error):
    response = getattr(error, 'response', {})
    return (response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404 and
            response.get('Error', {}).get('Code') in ('NoSuchKey', 'NotFound', '404'))


def _get_verified(client, config, guard, key, sha, size, mime):
    guard.before_request('GET', key)
    response = client.get_object(Bucket=config.bucket, Key=key)
    body = response['Body']
    try:
        if (isinstance(response.get('ContentLength'), bool) or
                not isinstance(response.get('ContentLength'), int) or response.get('ContentLength') != size or
                response.get('ContentType', '').split(';')[0].strip().lower() != mime):
            raise ValueError('cloud media length/type differs')
        digest, remaining = hashlib.sha256(), size
        while remaining:
            guard.check()
            raw = body.read(min(65536, remaining))
            if not raw:
                raise ValueError('cloud media body was shortened')
            if len(raw) > remaining:
                raise ValueError('cloud media body exceeded captured length')
            digest.update(raw)
            remaining -= len(raw)
        if body.read(1) or digest.hexdigest() != sha:
            raise ValueError('cloud media integrity differs')
    finally:
        body.close()


def _spool(config, row, guard, target):
    kind, relative, fingerprint, size, _, _, _, _, mime, _ = row
    parts = _parts(relative)
    with _directory(config.roots[kind], '/'.join(parts[:-1]) or '.', guard.source_mounts) as parent:
        fd = os.open(parts[-1], FILE_FLAGS, dir_fd=parent)
        try:
            info = os.fstat(fd)
            _safe_file(info)
            if _json(_fingerprint(info)) != fingerprint or info.st_size != size:
                raise UnsafeSource('selected source changed')
            digest, remaining = hashlib.sha256(), size
            while remaining:
                guard.check()
                raw = os.read(fd, min(65536, remaining))
                if not raw:
                    raise UnsafeSource('selected source shortened')
                digest.update(raw)
                if target.write(raw) != len(raw):
                    raise OSError('local media spool write was incomplete')
                remaining -= len(raw)
            if (_json(_fingerprint(os.fstat(fd))) != fingerprint or
                    _json(_fingerprint(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))) != fingerprint):
                raise UnsafeSource('selected source changed while reading')
        finally:
            os.close(fd)
    target.seek(0)
    sha = digest.hexdigest()
    extension = TYPES[PurePosixPath(relative).suffix.lower()][1]
    return _category(kind) + '/' + sha[:2] + '/' + sha + extension, sha, mime


def _associate(config, ledger, row, key, sha, mime, guard):
    kind, relative, fingerprint, size = row[:4]
    parts = _parts(relative)
    with _directory(config.roots[kind], '/'.join(parts[:-1]) or '.', guard.source_mounts) as parent:
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        _safe_file(info)
        if _json(_fingerprint(info)) != fingerprint:
            raise UnsafeSource('source changed before verified association')
    now = time.time()
    check_state_budget(config, ledger, guard, additional_bytes=size)
    with ledger:
        ledger.execute('UPDATE media_sync_objects SET status=\'verified\',verified_at=? WHERE key=?', (now, key))
        ledger.execute('UPDATE media_sync_sources SET status=\'verified\',key=?,sha256=?,content_type=?,verified_at=? WHERE kind=? AND path=? AND fingerprint=?',
                       (key, sha, mime, now, kind, relative, fingerprint))


def _mark_source_missing(config, ledger, row, guard):
    """Retire a vanished local association, never its immutable cloud receipt.

    Only local source open/stat failures call this helper. A fresh target/root
    identity check plus a second nofollow source lookup prevents cloud 404s or
    a replaced/unavailable configured root from tombstoning queued recordings.
    """
    _bound(config, ledger)
    guard.check()
    kind, relative, fingerprint = row[:3]
    parts = _parts(relative)
    try:
        with _directory(config.roots[kind], '/'.join(parts[:-1]) or '.', guard.source_mounts) as parent:
            os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        check_state_budget(config, ledger, guard)
        _bound(config, ledger)
        with ledger:
            ledger.execute("UPDATE media_sync_sources SET status='missing' WHERE kind=? AND path=? AND fingerprint=? AND status='pending'",
                           (kind, relative, fingerprint))
        return True
    return False


def _run_batch(config, client, ledger, guard, *, outer_lock_held):
    """Join an existing root lock/ledger/guard; never install a second SDK hook."""
    if outer_lock_held is not True:
        raise ConfigError('existing publisher lock is required')
    if getattr(guard, 'client', None) is not client:
        raise ConfigError('shared guard must already own the SDK before-send hook')
    _bound(config, ledger)
    result = discover(config, ledger, guard, outer_lock_held=True)
    result.update(uploaded=0, verified=0, failed=0)
    object_count = ledger.execute('SELECT COUNT(*) FROM media_sync_objects').fetchone()[0]
    remote_bytes = ledger.execute('SELECT COALESCE(SUM(size),0) FROM media_sync_objects').fetchone()[0]
    invalid_objects = ledger.execute("SELECT COUNT(*) FROM media_sync_objects WHERE typeof(size)!='integer' OR size<1 OR size>? OR status NOT IN ('pending','verified')", (config.file_bytes,)).fetchone()[0]
    if invalid_objects or type(remote_bytes) is not int or remote_bytes < 0:
        raise BudgetStop('durable remote media accounting is invalid')
    if remote_bytes + config.remote_reserved_bytes > config.max_remote_media_bytes:
        raise BudgetStop('durable remote media reservation already exceeds its cap')
    if object_count > config.max_objects:
        raise BudgetStop('local object catalog is already beyond its cap')
    hot = max(1, config.batch_files * 3 // 4)
    rows = ledger.execute('SELECT * FROM media_sync_sources WHERE status=\'pending\' ORDER BY mtime DESC,kind,path LIMIT ?', (hot,)).fetchall()
    history = ledger.execute('SELECT * FROM media_sync_sources WHERE status=\'pending\' ORDER BY mtime,kind,path LIMIT ?', (config.batch_files,)).fetchall()
    used = set()
    for row in rows + history:
        kind, relative, fingerprint, size = row[:4]
        if (kind, relative) in used:
            continue
        used.add((kind, relative))
        if guard.attempts + 3 > guard.phase_attempt_limit:
            result['paused'] = 'publisher-request-margin'
            break
        if not guard.admit(size):
            continue
        try:
            check_state_budget(config, ledger, guard, additional_bytes=size)
            # TemporaryFile is immediately unlinked on supported Unix hosts:
            # no persistent spool archive or unsafe age-based cleanup required.
            with tempfile.TemporaryFile(dir=config.state_dir) as spool:
                try:
                    key, sha, mime = _spool(config, row, guard, spool)
                except FileNotFoundError:
                    if _mark_source_missing(config, ledger, row, guard):
                        continue
                    raise
                existing = ledger.execute('SELECT sha256,size,content_type,status FROM media_sync_objects WHERE key=?', (key,)).fetchone()
                if existing is not None and existing[:3] != (sha, size, mime):
                    raise ValueError('durable immutable media identity differs')
                if existing is None:
                    if object_count >= config.max_objects:
                        raise BudgetStop('local immutable object catalog cap consumed')
                    object_count += 1
                    if remote_bytes + config.remote_reserved_bytes + size > config.max_remote_media_bytes:
                        raise BudgetStop('remote media body reservation would exceed its cap')
                    remote_bytes += size
                check_state_budget(config, ledger, guard, additional_bytes=size)
                with ledger:
                    ledger.execute('INSERT OR IGNORE INTO media_sync_objects VALUES (?,?,?,?,\'pending\',NULL)', (key, sha, size, mime))
                found = False
                try:
                    # A missing local proof is not evidence that a cloud key is
                    # absent. Verify pre-existing content before conditional PUT.
                    _get_verified(client, config, guard, key, sha, size, mime)
                    found = True
                except BudgetStop:
                    raise
                except Exception as error:
                    if not _not_found(error):
                        raise
                if not found:
                    guard.before_request('PUT', key, size)
                    client.put_object(Bucket=config.bucket, Key=key, Body=guard.wrap_body(spool),
                                      ContentLength=size, ContentType=mime,
                                      CacheControl='public,max-age=31536000,immutable', Metadata={'sha256': sha},
                                      IfNoneMatch='*')
                    result['uploaded'] += 1
                    _get_verified(client, config, guard, key, sha, size, mime)
                try:
                    _associate(config, ledger, row, key, sha, mime, guard)
                except FileNotFoundError:
                    if _mark_source_missing(config, ledger, row, guard):
                        continue
                    raise
                result['verified'] += 1
        except BudgetStop:
            raise
        except Exception:
            # Keep uncertain immutable receipt and pending source for next batch.
            # Do not expose SDK errors/headers/credential or deployment values.
            result['failed'] += 1
    result.update(selected_files=guard.selected_files, selected_bytes=guard.selected_bytes,
                  attempts=guard.attempts, payload_released_bytes=guard.released_bytes,
                  pending=ledger.execute('SELECT COUNT(*) FROM media_sync_sources WHERE status=\'pending\'').fetchone()[0])
    result.update(catalog_status(config, ledger))
    result['local_state'] = check_state_budget(config, ledger, guard)
    return result


def run_batch(config, client, ledger, guard, *, outer_lock_held,
              reserve_attempts=32, reserve_seconds=30):
    """Media phase leaves an explicit share for the current-cache publisher.

    A phase pause is recoverable; a global 150-attempt/120-second/resource stop
    propagates. The same SDK hook counts every phase. Media-only CLI reserves 0.
    """
    with guard.media_share(reserve_attempts, reserve_seconds):
        try:
            return _run_batch(config, client, ledger, guard, outer_lock_held=outer_lock_held)
        except MediaPause:
            return dict(paused='publisher-time-margin', attempts=guard.attempts,
                        selected_files=guard.selected_files, selected_bytes=guard.selected_bytes,
                        payload_released_bytes=guard.released_bytes,
                        **catalog_status(config, ledger))


def catalog_status(config, ledger):
    """Small count-only status; no private paths, credentials or byte reads."""
    _bound(config, ledger)
    source = dict(ledger.execute('SELECT status,COUNT(*) FROM media_sync_sources GROUP BY status'))
    objects = dict(ledger.execute('SELECT status,COUNT(*) FROM media_sync_objects GROUP BY status'))
    remote = ledger.execute('SELECT COALESCE(SUM(size),0) FROM media_sync_objects').fetchone()[0]
    return dict(pending=source.get('pending', 0), source_verified=source.get('verified', 0),
                source_ineligible=source.get('ineligible', 0), source_missing=source.get('missing', 0), sources=sum(source.values()),
                directories=ledger.execute('SELECT COUNT(*) FROM media_sync_dirs').fetchone()[0],
                objects=sum(objects.values()), object_pending=objects.get('pending', 0),
                remote_tracked_bytes=remote, remote_reserved_bytes=config.remote_reserved_bytes,
                remote_bound_bytes=remote + config.remote_reserved_bytes,
                remote_byte_cap=config.max_remote_media_bytes)


def _verified_source_absent(config, ledger, kind, relative, mounts, guard=None):
    """Confirm local retention beneath the physical root sealed in the ledger.

    A second nofollow lookup rejects reappearance. Open ancestor identities are
    checked against their still-current entries, so a moved/replaced directory
    cannot turn an inaccessible or different source into retained media.
    """
    _bound(config, ledger)
    sealed = json.loads(ledger.execute('SELECT identity FROM media_sync_target WHERE id=1').fetchone()[0])[3][kind][1]
    root = config.roots[kind]
    parts = _parts(relative)
    fds, edges = [], []
    try:
        fd = os.open(root, DIRECTORY_FLAGS)
        fds.append(fd)
        info = os.fstat(fd)
        if [info.st_dev, info.st_ino, info.st_uid] != sealed:
            raise ConfigError('verified source root differs from its ledger seal')
        full = root
        missing = None
        for number, part in enumerate(parts):
            if guard is not None:
                guard.check()
            full = full / part
            if str(full) in mounts or os.path.ismount(full):
                raise UnsafeSource('nested source mount is ineligible')
            try:
                if number == len(parts) - 1:
                    os.stat(part, dir_fd=fd, follow_symlinks=False)
                    return False
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                missing = (fd, part)
                break
            fds.append(child)
            child_info = os.fstat(child)
            if child_info.st_dev != sealed[0]:
                raise UnsafeSource('source crosses a filesystem')
            edges.append((fd, part, child))
            fd = child
        if missing is None:
            return False
        def stable_ancestors():
            current = os.fstat(fds[0])
            if [current.st_dev, current.st_ino, current.st_uid] != sealed:
                raise UnsafeSource('verified source root changed')
            for parent, name, child in edges:
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(child)
                if not stat.S_ISDIR(linked.st_mode) or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                    raise UnsafeSource('verified source ancestor changed')
        stable_ancestors()
        _bound(config, ledger)
        try:
            os.stat(missing[1], dir_fd=missing[0], follow_symlinks=False)
            return False
        except FileNotFoundError:
            pass
        stable_ancestors()
        _bound(config, ledger)
        return True
    finally:
        for fd in reversed(fds):
            os.close(fd)


def iter_verified_receipts(config, ledger, *, outer_lock_held, guard=None, predicate=None):
    """Read-only verified associations, including locally retained cloud media.

    Relative source paths/fingerprints are private integration evidence. The
    public frontend/exporter must emit only its reviewed media field allowlist.
    """
    if outer_lock_held is not True:
        raise ConfigError('existing publisher lock is required')
    _bound(config, ledger)
    mounts = _mount_points()
    cursor = ledger.execute('SELECT s.* FROM media_sync_sources s JOIN media_sync_objects o ON s.key=o.key WHERE s.status=\'verified\' AND o.status=\'verified\' AND typeof(o.verified_at) IN (\'real\',\'integer\') AND o.verified_at>0 AND o.verified_at<1e99 AND s.sha256=o.sha256 AND s.size=o.size AND s.content_type=o.content_type ORDER BY s.kind,s.path')
    for row in cursor:
        kind, relative, fingerprint, size, _, _, key, sha, mime, verified_at = row
        if guard is not None:
            guard.check()
        public_kind = 'audio' if kind == 'audio' else 'image'
        if predicate is not None and not predicate(public_kind, relative):
            continue
        extension = TYPES.get(PurePosixPath(relative).suffix.lower())
        expected_key = _category(kind) + '/' + str(sha)[:2] + '/' + str(sha) + (extension[1] if extension else '')
        if (kind not in config.roots or extension is None or extension[0] != _category(kind) or key != expected_key or
                not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha) or
                not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= config.file_bytes or
                mime != extension[2] or not isinstance(verified_at, (int, float)) or
                isinstance(verified_at, bool) or not 0 < verified_at < float('inf')):
            continue
        try:
            parts = _parts(relative)
            with _directory(config.roots[kind], '/'.join(parts[:-1]) or '.', mounts) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                _safe_file(info)
                if _json(_fingerprint(info)) != fingerprint:
                    continue
        except FileNotFoundError:
            try:
                if not _verified_source_absent(config, ledger, kind, relative, mounts, guard):
                    continue
            except (OSError, UnsafeSource):
                continue
        except (OSError, UnsafeSource):
            continue
        yield dict(kind=public_kind, root_label=kind,
                   relative_path=relative, key=key, sha256=sha, bytes=size,
                   content_type=mime, url=config.public_origin + '/' + key,
                   current_fingerprint=json.loads(fingerprint), verified_at=verified_at)


def dry_scan(config):
    """Read-only bounded initial scan: no state/SQLite/SDK/import/byte reads."""
    counts = dict(scanned=0, eligible=0, unsafe=0, eligible_bytes=0)
    mounts = _mount_points()
    queue = [(kind, '.') for kind in config.roots]
    while queue and counts['scanned'] < config.scan_entries:
        kind, relative = queue.pop(0)
        try:
            with _directory(config.roots[kind], relative, mounts) as fd, os.scandir(fd) as entries:
                for entry in entries:
                    if counts['scanned'] >= config.scan_entries:
                        break
                    counts['scanned'] += 1
                    path = entry.name if relative == '.' else relative + '/' + entry.name
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        _parts(path)
                        queue.append((kind, path))
                    else:
                        _safe_file(info)
                        value = TYPES.get(PurePosixPath(path).suffix.lower())
                        if value is not None and value[0] == _category(kind) and 1 <= info.st_size <= config.file_bytes:
                            counts['eligible'] += 1
                            counts['eligible_bytes'] += info.st_size
        except (UnsafeSource, OSError):
            counts['unsafe'] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    parser.add_argument('--dry-run', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--loop', action='store_true')
    args = parser.parse_args(argv)
    environment = load_env_file(args.env_file) if args.env_file else {}
    environment.update(os.environ)  # Existing process settings take precedence.
    config = Config.from_env(environment, require_credentials=not args.dry_run)
    if config is None:
        print(_json({'status': 'disabled'}))
        return 0
    if args.dry_run:
        print(_json(dict(dry_scan(config), status='read-only')))
        return 0
    client = make_client(config, credentials=environment)
    try:
        while True:
            delay = config.interval_seconds
            try:
                with state_lock(config):
                    ledger = open_ledger(config, outer_lock_held=True)
                    guard = SyncGuard(config)
                    try:
                        guard.attach(client)
                        result = run_batch(config, client, ledger, guard, outer_lock_held=True,
                                           reserve_attempts=0, reserve_seconds=0)
                        print(_json(dict(result, status='complete')), flush=True)
                        if result.get('failed'):
                            delay = config.backoff_seconds
                    finally:
                        guard.detach()
                        ledger.close()
            except (BudgetStop, OSError, sqlite3.Error, ConfigError, UnsafeSource):
                print(_json({'status': 'stopped'}), flush=True)
                if not args.loop:
                    return 1
                delay = config.backoff_seconds
            if not args.loop:
                return 0
            time.sleep(delay)
    finally:
        client.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ConfigError, ValueError, OSError):
        print('Media sync configuration or local state is invalid.', file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(0)
