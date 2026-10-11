#!/usr/bin/env python3
"""Generate an allowlisted public browser config from the build environment."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


URL_NAMES = ('AVIAN_MEDIA_ORIGIN', 'AVIAN_SNAPSHOT_INDEX_URL', 'AVIAN_WIKI_INDEX_URL')
WINDOW_NAMES = ('AVIAN_PUBLIC_STATIC_MODE',) + URL_NAMES


def https_url(value, label, *, origin_only=False):
    # Reject browser URL parser ambiguities and credentials in public config.
    if not isinstance(value, str) or not value or value != value.strip() or re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError(label + ' must be a clean absolute HTTPS URL')
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError(label + ' must be a clean absolute HTTPS URL') from None
    if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or parsed.netloc.endswith(':')):
        raise ValueError(label + ' must be HTTPS without credentials, query or fragment')
    if ':' in host:
        try:
            host = '[' + str(ipaddress.IPv6Address(host)) + ']'
        except ValueError:
            raise ValueError(label + ' has an invalid HTTPS host') from None
    else:
        # ASCII DNS/IP form keeps Python and browser origin comparisons equal.
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError(label + ' has an invalid HTTPS host')
        host = host.lower()
    origin = 'https://' + host + (':' + str(port) if port not in (None, 443) else '')
    if origin_only and parsed.path not in ('', '/'):
        raise ValueError(label + ' must be an HTTPS origin without a path')
    # Only the origin setting is normalized. Preserve absolute object paths.
    return (origin if origin_only else value), origin


def public_config(environment):
    """Resolve tri-state enablement; never copy or enumerate private values."""
    enabled = environment.get('AVIAN_CDN_ENABLED')
    if enabled not in (None, '0', '1'):
        raise ValueError('AVIAN_CDN_ENABLED must be exactly 0 or 1 when present')
    empty = {'AVIAN_PUBLIC_STATIC_MODE': False, **{name: '' for name in URL_NAMES}}
    if enabled == '0':
        return empty
    values = {name: environment.get(name, '') for name in URL_NAMES}
    present = {name: bool(value) for name, value in values.items()}
    required = present['AVIAN_MEDIA_ORIGIN'] and present['AVIAN_SNAPSHOT_INDEX_URL']
    if not required:
        if enabled == '1' or any(present.values()):
            raise ValueError('CDN mode requires both AVIAN_MEDIA_ORIGIN and AVIAN_SNAPSHOT_INDEX_URL')
        return empty
    media, origin = https_url(values['AVIAN_MEDIA_ORIGIN'], 'AVIAN_MEDIA_ORIGIN', origin_only=True)
    snapshot, snapshot_origin = https_url(values['AVIAN_SNAPSHOT_INDEX_URL'], 'AVIAN_SNAPSHOT_INDEX_URL')
    if snapshot_origin != origin:
        raise ValueError('AVIAN_SNAPSHOT_INDEX_URL must use AVIAN_MEDIA_ORIGIN')
    wiki = ''
    if values['AVIAN_WIKI_INDEX_URL']:
        wiki, wiki_origin = https_url(values['AVIAN_WIKI_INDEX_URL'], 'AVIAN_WIKI_INDEX_URL')
        if wiki_origin != origin:
            raise ValueError('AVIAN_WIKI_INDEX_URL must use AVIAN_MEDIA_ORIGIN')
    return dict(AVIAN_PUBLIC_STATIC_MODE=True, AVIAN_MEDIA_ORIGIN=media,
                AVIAN_SNAPSHOT_INDEX_URL=snapshot, AVIAN_WIKI_INDEX_URL=wiki)


def render(config):
    if tuple(config) != WINDOW_NAMES:
        raise ValueError('public config contains unexpected keys')
    # JSON escapes remain valid JavaScript; escape < for safe embedding too.
    lines = ['// Generated public configuration. Contains no deployment credentials.']
    for name in WINDOW_NAMES:
        value = json.dumps(config[name], ensure_ascii=True).replace('<', '\\u003c')
        lines.append('window.' + name + ' = ' + value + ';')
    return '\n'.join(lines) + '\n'


def generate(environment, output):
    # Validate completely before touching a previous generated release file.
    body = render(public_config(environment)).encode('utf-8')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + output.name + '.', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    try:
        generate(os.environ, arguments.output)
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
