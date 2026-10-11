"""Optional best-effort live update hint after verified current CDN publication.

No notification is an upload or media proof. Missed hints are recovered by the
browser's periodic CDN refresh. One durable latest delivery record is sufficient.
"""
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit
import urllib.request

import sync_media


@dataclass(frozen=True, repr=False)
class NotifyConfig:
    url: str
    token: str

    @classmethod
    def from_env(cls, environment):
        url = environment.get('AVIAN_SYNC_NOTIFY_URL', '')
        token = environment.get('AVIAN_SYNC_NOTIFY_TOKEN', '')
        if not url and not token:
            return None
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.port is not None or parsed.path != '/notify' or '?' in url or '#' in url or
                not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token)):
            raise ValueError('optional updates require a clean HTTPS notify URL and private token')
        return cls(url, token)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        raise ValueError('update notification redirects are refused')


def notify_current(sync, ledger, guard, config, *, opener=None):
    """Notify only the ledger-selected, fully verified current schema-2 index.

The caller retains the publisher lock. Exactly one attempted HTTP send joins
the same batch guard; the SDK hook remains installed only once. All network
failures preserve the pending hint and are harmless to the selected CDN cache.
"""
    if config is None:
        return 'disabled'
    row = ledger.execute("SELECT raw,phase FROM cdn_cache_graph WHERE slot='current'").fetchone()
    if row is None:
        return 'unavailable'
    if row[1] != 'selected':
        raise ValueError('notification requires a verified selected current cache')
    index = json.loads(bytes(row[0]))
    generation, as_of = index.get('generation'), index.get('as_of')
    if not isinstance(generation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', generation):
        raise ValueError('invalid selected generation')
    try:
        instant = dt.datetime.fromisoformat(as_of.replace('Z', '+00:00'))
        if instant.utcoffset() is None:
            raise ValueError('notification clock must be timezone aware')
    except (AttributeError, TypeError) as error:
        raise ValueError('invalid selected clock') from error
    raw = json.dumps({'generation': generation, 'as_of': as_of}, separators=(',', ':')).encode('utf-8')
    identity = hashlib.sha256((config.url + '\0' + config.token).encode('utf-8')).hexdigest()
    if len(raw) > 1024:
        raise ValueError('notification exceeds byte limit')
    try:
        guard.check()
        sync_media.check_state_budget(sync, ledger, guard, additional_bytes=65536)
        with ledger:
            ledger.execute('CREATE TABLE IF NOT EXISTS cdn_update_delivery(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL,generation TEXT NOT NULL,as_of TEXT NOT NULL,status TEXT NOT NULL)')
        previous = ledger.execute('SELECT identity,generation,as_of,status FROM cdn_update_delivery WHERE id=1').fetchone()
        if previous == (identity, generation, as_of, 'delivered'):
            return 'unchanged'
        with ledger:
            ledger.execute("INSERT OR REPLACE INTO cdn_update_delivery VALUES(1,?,?,?,'pending')", (identity, generation, as_of))
        guard.before_send()
        request = urllib.request.Request(config.url, data=raw, method='POST',
                                         headers={'Authorization': 'Bearer ' + config.token,
                                                  'Content-Type': 'application/json',
                                                  'Accept': 'application/json',
                                                  'User-Agent': 'AvianPublisher/1.0'})
        opener = opener or urllib.request.build_opener(NoRedirect())
        # This is a short optional admission, with no retry or redirect. Each
        # socket operation has a timeout; the caller's process time limit also
        # bounds blocking DNS/connect behavior.
        with opener.open(request, timeout=3) as response:
            guard.check()
            if response.status != 200:
                return 'deferred'
            body = response.read(1025)
            guard.check()
            if len(body) > 1024:
                return 'deferred'
            acknowledgment = json.loads(body)
            if (not isinstance(acknowledgment, dict) or acknowledgment.get('status') not in ('updated', 'ignored') or
                    acknowledgment.get('generation') != generation or acknowledgment.get('as_of') != as_of):
                return 'deferred'
        sync_media.check_state_budget(sync, ledger, guard, additional_bytes=65536)
        with ledger:
            ledger.execute("UPDATE cdn_update_delivery SET status='delivered' WHERE id=1 AND identity=? AND generation=? AND as_of=?", (identity, generation, as_of))
        return 'delivered'
    except Exception:
        # A notification never changes cache selection or retries in this batch.
        # Raw errors can contain endpoint, Authorization headers or private data.
        return 'deferred'
