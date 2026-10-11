"""Nondeleting local output preflight; cooperating publishers share one lock."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

DEFAULT_QUOTA = 256 * 1024 * 1024
DEFAULT_RESERVE = 32 * 1024 * 1024
MAX_ENTRIES = 100000


@contextmanager
def output_lock(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or not output.is_dir():
        raise ValueError('snapshot output must be a real directory')
    fd = os.open(str(output / '.snapshot-publisher.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('snapshot publisher lock is not a regular file')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def output_usage(output, entry_limit=MAX_ENTRIES):
    logical = allocated = entries = 0
    pending = [Path(output)]
    while pending:
        with os.scandir(pending.pop()) as directory:
            for item in directory:
                entries += 1
                if entries > entry_limit:
                    raise ValueError('snapshot output entry inventory limit exceeded')
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError('snapshot output contains a link or special file')
                logical += info.st_size
                allocated += max(info.st_size, info.st_blocks * 512)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(item.path))
    return {'logical_bytes': logical, 'allocated_bytes': allocated, 'entries': entries}


def preflight(output, new_object_lengths, manifest_length,
              quota=DEFAULT_QUOTA, reserve=DEFAULT_RESERVE):
    if (isinstance(quota, bool) or not isinstance(quota, int) or quota < 1 or
            isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0):
        raise ValueError('invalid local snapshot quota or disk reserve')
    usage = output_usage(output)
    filesystem = os.statvfs(output)
    block = max(filesystem.f_frsize, 4096)
    round_up = lambda length: ((length + block - 1) // block) * block
    # New objects are atomically renamed; a candidate manifest and a complete
    # index temporary coexist with the previous index before replacement.
    # Add filesystem room for the candidate directory and directory growth.
    additional = (sum(round_up(n) for n in new_object_lengths) +
                  2 * round_up(manifest_length) + (len(new_object_lengths) + 4) * block)
    peak = usage['allocated_bytes'] + additional
    if peak > quota:
        raise ValueError('local snapshot quota would be exceeded; previous index retained')
    available = filesystem.f_bavail * filesystem.f_frsize
    if available < additional + reserve:
        raise ValueError('local disk reserve would be breached; previous index retained')
    return {**usage, 'additional_bound_bytes': additional, 'peak_bound_bytes': peak,
            'quota_bytes': quota, 'available_bytes': available, 'reserve_bytes': reserve}
