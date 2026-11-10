#!/usr/bin/env python3
"""BC1830 public reader: fixed-source data pull; code is installed once (#47)."""
import argparse
import fcntl
import gzip
import grp
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = 'molegan1984/bc1830-public-releases'
SOURCE = 'https://raw.githubusercontent.com/' + REPO + '/main/current.json'
ASSETS = 'https://github.com/' + REPO + '/releases/download/'
FILES = ('index.html', 'part-1.html', 'part-2.html', 'guest-journey.html')
MAX_ARCHIVE, MAX_FILE, MAX_TOTAL = 8 * 1024**2, 6 * 1024**2, 16 * 1024**2
ROOT = Path('/srv/bc1830-mvp-demo')
STATE = Path('/var/lib/bc1830-reader')
INSTALL = Path('/usr/local/lib/bc1830-reader/receiver.py')
USER = 'bc1830-reader'
ROOT_SHA = '3baf9c4a5dc9773951edefaff84b157247f458f736af3d38bc4bc8009764ba0e'
HOSTS = {'raw.githubusercontent.com', 'github.com',
         'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_manifest(m):
    if not isinstance(m, dict) or set(m) != {'schema', 'release', 'archive_url',
            'archive_sha256', 'archive_size', 'files'}:
        raise ValueError('manifest fields')
    digest = m['archive_sha256']
    if type(m['schema']) is not int or m['schema'] != 1:
        raise ValueError('manifest schema')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('archive digest')
    if m['release'] != 'reader-' + digest or m['archive_url'] != ASSETS + m['release'] + '/reader.tar.gz':
        raise ValueError('fixed immutable archive source')
    if type(m['archive_size']) is not int or not 0 < m['archive_size'] <= MAX_ARCHIVE:
        raise ValueError('archive size')
    if not isinstance(m['files'], dict) or set(m['files']) != set(FILES):
        raise ValueError('four fixed HTML files required')
    total = 0
    for item in m['files'].values():
        if not isinstance(item, dict) or set(item) != {'size', 'sha256'}:
            raise ValueError('file metadata')
        if type(item['size']) is not int or not 0 < item['size'] <= MAX_FILE:
            raise ValueError('file size')
        if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            raise ValueError('file digest')
        total += item['size']
    if total > MAX_TOTAL:
        raise ValueError('total size')
    return m


def validate_archive(m, data):
    validate_manifest(m)
    if len(data) != m['archive_size'] or sha(data) != m['archive_sha256']:
        raise ValueError('archive checksum or length')
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        raw = compressed.read(MAX_TOTAL + 65537)
    if len(raw) > MAX_TOTAL + 65536:
        raise ValueError('decompression limit')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        members = archive.getmembers()
        if len(members) != len(FILES):
            raise ValueError('archive member count')
        for member in members:
            if not member.isfile() or member.name not in FILES or member.name in files:
                raise ValueError('unsafe archive member')
            expected = m['files'][member.name]
            if member.size != expected['size']:
                raise ValueError('member size')
            content = archive.extractfile(member).read(expected['size'] + 1)
            if len(content) != expected['size'] or sha(content) != expected['sha256']:
                raise ValueError('file checksum')
            files[member.name] = content
    return files


class FixedHTTPS(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_https(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != 'https' or u.hostname not in HOSTS or u.username or u.password or u.port not in (None, 443):
        raise ValueError('HTTPS source host')


def download(url, limit, etag=None):
    check_https(url)
    headers = {'User-Agent': 'bc1830-public-reader/1', 'Cache-Control': 'no-cache'}
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FixedHTTPS())
    try:
        with opener.open(request, timeout=25) as response:
            check_https(response.url)
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('download size limit')
            return data, response.headers.get('ETag')
    except urllib.error.HTTPError as error:
        if error.code == 304 and etag:
            return None, etag
        raise


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.write_text(json.dumps(data, sort_keys=True) + '\n')
    os.replace(temporary, path)


def pointer(path, target):
    temporary = path.with_name(path.name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def apply_release(m, data, root, state):
    files = validate_archive(m, data)  # No FS mutations before full validation.
    root_before = sha((root / 'index.html').read_bytes())
    web, releases = root / 'remarked', state / 'releases'
    marker = web / '.managed-by-public-reader'
    if web.exists() and not marker.is_file():
        raise ValueError('existing /remarked is not managed by this receiver')
    web.mkdir(mode=0o755, exist_ok=True)
    if not marker.exists():
        marker.touch()
    releases.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in FILES:
        alias = web / name
        wanted = 'current/' + name
        if alias.is_symlink():
            if os.readlink(alias) != wanted:
                raise ValueError('unexpected public symlink')
        elif alias.exists():
            raise ValueError('existing public file')
        else:
            alias.symlink_to(wanted)
    destination = releases / m['release']
    if destination.exists():
        for name, content in files.items():
            if (destination / name).is_symlink() or (destination / name).read_bytes() != content:
                raise ValueError('immutable cached release changed')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.incoming-', dir=releases))
        try:
            for name, content in files.items():
                (stage / name).write_bytes(content)
                (stage / name).chmod(0o644)
            stage.chmod(0o755)
            stage.rename(destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    current = web / 'current'
    changed = not current.is_symlink() or current.resolve() != destination.resolve()
    if changed:
        if current.is_symlink():
            old = current.resolve()
            if old.parent != releases.resolve():
                raise ValueError('current pointer outside managed releases')
            pointer(web / 'previous', old)
        pointer(current, destination.resolve())
    atomic_json(state / 'active.json', m)
    if sha((root / 'index.html').read_bytes()) != root_before:
        raise RuntimeError('root HTML changed')
    return 'updated' if changed else 'unchanged'


def verified_source(etag=None):
    raw, tag = download(SOURCE, 16384, etag)
    if raw is None:
        return None, None, tag
    manifest = validate_manifest(json.loads(raw))
    data, _ = download(manifest['archive_url'], MAX_ARCHIVE)
    validate_archive(manifest, data)
    return manifest, data, tag


def pull():
    if sha((ROOT / 'index.html').read_bytes()) != ROOT_SHA:
        raise ValueError('unexpected root HTML; leaving site unchanged')
    STATE.mkdir(exist_ok=True)
    with (STATE / 'pull.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = json.loads((STATE / 'etag.json').read_text()) if (STATE / 'etag.json').exists() else {}
        manifest, data, tag = verified_source(old.get('etag'))
        if manifest is None:
            print('UNCHANGED (ETag)')
            return
        result = apply_release(manifest, data, ROOT, STATE)
        atomic_json(STATE / 'etag.json', {'etag': tag})
        print(result.upper(), manifest['release'])


def bootstrap():
    # Mandatory gate: HTTPS, manifest and complete archive first; no users/units yet.
    manifest, data, _ = verified_source()
    if os.geteuid() != 0:
        raise PermissionError('bootstrap requires root')
    if sha((ROOT / 'index.html').read_bytes()) != ROOT_SHA:
        raise ValueError('unexpected root HTML')
    for name in ('bc1830-reader-pull.service', 'bc1830-reader-pull.timer'):
        path = Path('/etc/systemd/system') / name
        if path.exists() and not path.read_text().startswith('# BC1830 public reader (#47)'):
            raise ValueError('refusing to overwrite unrelated unit')
    try:
        account = pwd.getpwnam(USER)
        if account.pw_shell not in ('/usr/sbin/nologin', '/bin/false') or grp.getgrgid(account.pw_gid).gr_name != USER:
            raise ValueError('existing account is not the dedicated service user')
    except KeyError:
        subprocess.run(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(STATE), '--shell', '/usr/sbin/nologin', USER], check=True)
        account = pwd.getpwnam(USER)
    STATE.mkdir(mode=0o755, exist_ok=True)
    apply_release(manifest, data, ROOT, STATE)
    for directory in (STATE, STATE / 'releases', ROOT / 'remarked'):
        directory.chmod(0o755)
        os.chown(directory, account.pw_uid, account.pw_gid)
    # Check nginx can read the initial release before enabling an automatic service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name in FILES:
        with opener.open('http://127.0.0.1/remarked/' + name, timeout=10) as response:
            if sha(response.read(MAX_FILE + 1)) != manifest['files'][name]['sha256']:
                raise ValueError('initial local HTTP checksum')
    INSTALL.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = INSTALL.with_name('receiver.py.new')
    temporary.write_bytes(Path(__file__).read_bytes())
    temporary.chmod(0o644)
    os.replace(temporary, INSTALL)
    service = '''# BC1830 public reader (#47)
[Unit]
Description=BC1830 public reader HTTPS pull
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=bc1830-reader
Group=bc1830-reader
ExecStart=/usr/bin/python3 /usr/local/lib/bc1830-reader/receiver.py pull
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/bc1830-reader /srv/bc1830-mvp-demo/remarked
TimeoutStartSec=90
MemoryMax=128M
'''
    timer = '''# BC1830 public reader (#47)
[Unit]
Description=Check BC1830 public release each minute
[Timer]
OnBootSec=15s
OnUnitActiveSec=60s
AccuracySec=5s
Unit=bc1830-reader-pull.service
[Install]
WantedBy=timers.target
'''
    for name, text in [('bc1830-reader-pull.service', service), ('bc1830-reader-pull.timer', timer)]:
        path = Path('/etc/systemd/system') / name
        if path.exists() and not path.read_text().startswith('# BC1830 public reader (#47)'):
            raise ValueError('refusing to overwrite unrelated unit')
        path.write_text(text)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'bc1830-reader-pull.timer'], check=True)
    print('INSTALLED', manifest['release'], 'ROOT UNCHANGED', ROOT_SHA)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('bootstrap', 'pull'))
    args = parser.parse_args()
    try:
        bootstrap() if args.command == 'bootstrap' else pull()
    except Exception as error:
        print('FAILED:', type(error).__name__, str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
