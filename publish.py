#!/usr/bin/env python3
"""Publish reviewed public HTML, or select an earlier immutable release (#47)."""
import argparse
import base64
import gzip
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

from receiver import FILES, REPO, ASSETS, MAX_ARCHIVE, MAX_FILE, ROOT_SHA, sha, validate_manifest, validate_archive

BASE_URL = 'http://136.0.96.141'


def build(source, proof):
    import tarfile
    approved = json.loads(Path(proof).read_text())
    if not isinstance(approved, list) or len(approved) != len(FILES):
        raise ValueError('reviewed manifest must list exactly four files')
    approved = {item['name']: item for item in approved}
    if set(approved) != set(FILES):
        raise ValueError('reviewed names')
    raw = io.BytesIO()
    files = {}
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name in FILES:
            path = Path(source) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('input must be a regular HTML file')
            if not 0 < path.stat().st_size <= MAX_FILE:
                raise ValueError('input size limit')
            data = path.read_bytes()
            if len(data) != approved[name]['bytes'] or sha(data) != approved[name]['sha256']:
                raise ValueError('input changed after review: ' + name)
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o644, 0
            archive.addfile(member, io.BytesIO(data))
            files[name] = {'size': len(data), 'sha256': sha(data)}
    data = gzip.compress(raw.getvalue(), mtime=0)
    digest = sha(data)
    tag = 'reader-' + digest
    manifest = {'schema': 1, 'release': tag, 'archive_url': ASSETS + tag + '/reader.tar.gz',
                'archive_sha256': digest, 'archive_size': len(data), 'files': files}
    validate_archive(manifest, data)
    return manifest, data


def gh(*args, body=None, check=True):
    result = subprocess.run(['gh', *args], input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError('gh failed: ' + result.stderr.strip())
    return result


def current():
    result = gh('api', 'repos/' + REPO + '/contents/current.json', check=False)
    if result.returncode:
        if '404' in result.stderr:
            return None, None
        raise RuntimeError('cannot read current manifest: ' + result.stderr.strip())
    record = json.loads(result.stdout)
    return json.loads(base64.b64decode(record['content'])), record['sha']


def select_release(manifest, old_sha):
    validate_manifest(manifest)
    body = {'message': 'Publish reviewed reader ' + manifest['release'],
            'content': base64.b64encode((json.dumps(manifest, sort_keys=True) + '\n').encode()).decode(),
            'branch': 'main'}
    if old_sha:
        body['sha'] = old_sha
    gh('api', 'repos/' + REPO + '/contents/current.json', '--method', 'PUT', '--input', '-', body=body)


def wait_public(manifest, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = 'not installed yet'
    while True:
        try:
            with opener.open(BASE_URL + '/', timeout=10) as response:
                if response.status != 200 or sha(response.read(MAX_FILE + 1)) != ROOT_SHA:
                    raise RuntimeError('root HTML changed')
            for name, expected in manifest['files'].items():
                with opener.open(BASE_URL + '/remarked/' + name, timeout=10) as response:
                    data = response.read(MAX_FILE + 1)
                    if response.status != 200 or len(data) != expected['size'] or sha(data) != expected['sha256']:
                        raise ValueError('public checksum not current: ' + name)
            print('PUBLISHED_VERIFIED', manifest['release'], BASE_URL + '/remarked/')
            return
        except RuntimeError:
            raise
        except Exception as error:
            last = str(error)
        if time.monotonic() >= deadline:
            raise TimeoutError('Release uploaded, but NOT verified on VPS: ' + last)
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def publish(source, proof, timeout, verify=True):
    manifest, data = build(source, proof)
    old, old_sha = current()
    if old and old.get('files') == manifest['files']:
        print('NO_CHANGE: same reviewed HTML; no release or manifest write')
        if verify:
            wait_public(validate_manifest(old), timeout)
        else:
            print('READY_FOR_BOOTSTRAP; NOT_INSTALLED_NOT_PUBLISHED')
        return
    tag = manifest['release']
    with tempfile.TemporaryDirectory(prefix='bc1830-release-') as temporary:
        directory = Path(temporary)
        (directory / 'reader.tar.gz').write_bytes(data)
        (directory / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
        existing = gh('release', 'view', tag, '--repo', REPO, check=False)
        if existing.returncode:
            gh('release', 'create', tag, str(directory / 'reader.tar.gz'), str(directory / 'manifest.json'),
               '--repo', REPO, '--target', 'main', '--title', tag,
               '--notes', 'Reviewed public HTML only. SHA256 ' + manifest['archive_sha256'])
        else:
            check_dir = directory / 'readback'
            check_dir.mkdir()
            gh('release', 'download', tag, '--repo', REPO, '--dir', str(check_dir),
               '--pattern', 'reader.tar.gz', '--pattern', 'manifest.json')
            if json.loads((check_dir / 'manifest.json').read_text()) != manifest:
                raise ValueError('existing immutable release manifest differs')
            validate_archive(manifest, (check_dir / 'reader.tar.gz').read_bytes())
    # Pointer is committed only after both immutable assets have completed upload.
    select_release(manifest, old_sha)
    print('RELEASE_UPLOADED', tag, '(VPS verification pending)', flush=True)
    if verify:
        wait_public(manifest, timeout)
    else:
        print('READY_FOR_BOOTSTRAP; NOT_INSTALLED_NOT_PUBLISHED')


def rollback(tag, timeout):
    if not tag.startswith('reader-') or len(tag) != 71 or any(c not in '0123456789abcdef' for c in tag[7:]):
        raise ValueError('expected immutable reader-SHA256 tag')
    with tempfile.TemporaryDirectory(prefix='bc1830-rollback-') as temporary:
        gh('release', 'download', tag, '--repo', REPO, '--dir', temporary,
           '--pattern', 'reader.tar.gz', '--pattern', 'manifest.json')
        directory = Path(temporary)
        manifest = validate_manifest(json.loads((directory / 'manifest.json').read_text()))
        if manifest['release'] != tag:
            raise ValueError('rollback release mismatch')
        validate_archive(manifest, (directory / 'reader.tar.gz').read_bytes())
    old, old_sha = current()
    if old != manifest:
        select_release(manifest, old_sha)
    wait_public(manifest, timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('build', 'prepare', 'publish'):
        p = commands.add_parser(name)
        p.add_argument('source', type=Path)
        p.add_argument('--approved-manifest', type=Path, required=True)
        if name == 'build':
            p.add_argument('--output', type=Path, required=True)
        else:
            p.add_argument('--timeout', type=int, default=240)
    p = commands.add_parser('rollback')
    p.add_argument('release')
    p.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if args.command == 'build':
        manifest, data = build(args.source, args.approved_manifest)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'reader.tar.gz').write_bytes(data)
        (args.output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
        print('BUILT_NOT_PUBLISHED', manifest['release'])
    elif args.command in ('prepare', 'publish'):
        publish(args.source, args.approved_manifest, args.timeout, args.command == 'publish')
    else:
        rollback(args.release, args.timeout)


if __name__ == '__main__':
    main()
