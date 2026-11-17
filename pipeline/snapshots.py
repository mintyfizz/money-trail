"""Verified GitHub release snapshots; no Actions artifacts and no raw ZIP required on the consumer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from ingest.archive import GIB, ensure_space, exclusive_lock, save_json, sha256, verified_receipt
from pipeline.bronze import check_existing

ASSET_LIMIT = 2 * GIB  # GitHub requires every asset to be strictly smaller than 2 GiB.
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


def gh(*args):
    try:
        return subprocess.run(['gh', *map(str, args)], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as error:
        error.add_note((error.stderr or '').strip())
        raise


def api(endpoint):
    return json.loads(gh('api', endpoint))


def validate_repository(repository):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Expected owner/repository')


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.()-]+', name) or name in ('.', '..'):
        raise ValueError('Unsafe release filename or tag')


def release_info(repository, tag, missing_ok=False):
    validate_repository(repository)
    validate_name(tag)
    try:
        return api(f'repos/{repository}/releases/tags/{tag}')
    except subprocess.CalledProcessError as error:
        if 'HTTP 404' in (error.stderr or ''):
            # Drafts have no published tag yet; list releases with authenticated access.
            pages = json.loads(gh('api', '--paginate', '--slurp',
                                  f'repos/{repository}/releases?per_page=100'))
            matches = [release for page in pages for release in page if release['tag_name'] == tag]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous release tag')
            if matches:
                return matches[0]
            if missing_ok:
                return None
        raise


def release_assets(repository, release_id):
    pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repository}/releases/{int(release_id)}/assets?per_page=100'))
    return [asset for page in pages for asset in page]


def asset_metadata(path, kind):
    path = Path(path)
    validate_name(path.name)
    size = path.stat().st_size
    if path.is_symlink() or not path.is_file() or size >= ASSET_LIMIT:
        raise ValueError('Release assets must be regular files smaller than 2 GiB')
    return {'name': path.name, 'bytes': size, 'sha256': sha256(path), 'kind': kind,
            'storage': 'inline_empty' if size == 0 else 'release_asset'}


def check_remote_asset(asset, expected):
    if (asset.get('state') != 'uploaded' or asset.get('size') != expected['bytes']
            or asset.get('digest') != 'sha256:' + expected['sha256']):
        raise RuntimeError(f'Remote asset size/checksum is unverified: {expected["name"]}')


def publish(raw, directory, catalog_path, run_path, repository, commit, revision=1, reserve_gib=8):
    """Upload to a draft, verify provider SHA-256, then publish as a scoped prerelease."""
    validate_repository(repository)
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit) or revision < 1:
        raise ValueError('A full commit SHA and positive snapshot revision are required')
    raw, directory = Path(raw), Path(directory)
    receipt = verified_receipt(raw)
    manifest = check_existing(directory, receipt['sha256'])
    run = json.loads(Path(run_path).read_text())
    record = run['selected_source']
    if not manifest.get('snapshot_date') or manifest['source_url'] != record['url']:
        raise ValueError('Snapshot provenance is incomplete')
    scope = 'national' if record['agency_id'] == 'all' else f'agency-{int(record["agency_id"])}'
    tag = f'snapshot-{manifest["snapshot_date"]}-fy{int(record["fiscal_year"])}-{record["type"]}-{scope}-r{revision}'
    validate_name(tag)
    paths = [(raw, 'raw_zip'), (Path(str(raw) + '.receipt.json'), 'receipt'),
             (directory / 'manifest.json', 'manifest'), (Path(catalog_path), 'catalog'), (Path(run_path), 'measurement')]
    paths.extend((directory / entry['file'], 'parquet' if entry['file'].endswith('.parquet') else 'rejects')
                 for entry in manifest['artifacts'])
    entries = [asset_metadata(path, kind) for path, kind in paths]
    if len({entry['name'] for entry in entries}) != len(entries) or len(entries) + 1 > 1000:
        raise ValueError('Duplicate names or too many release assets')
    snapshot = {'format_version': 1, 'tag': tag, 'repository': repository, 'scope': scope,
                'source': record, 'source_sha256': receipt['sha256'], 'code_commit': commit,
                'preservation_format_version': manifest['format_version'], 'assets': entries,
                'raw_zip_retained': True, 'national_coverage_verified': False}
    snapshot_path = Path(run_path).parent / 'snapshot.json'
    ensure_space(snapshot_path.parent, 1024 * 1024, int(reserve_gib * GIB))
    save_json(snapshot_path, snapshot)
    paths.append((snapshot_path, 'snapshot_index'))
    entries.append(asset_metadata(snapshot_path, 'snapshot_index'))
    expected = {entry['name']: entry for entry in entries if entry['storage'] == 'release_asset'}
    # Resolve repository access before treating a missing release as absent.
    api(f'repos/{repository}')
    existing = release_info(repository, tag, missing_ok=True)
    if existing is None:
        notes = snapshot_path.parent / 'release-notes.md'
        notes.write_text(f'Raw snapshot for FY{record["fiscal_year"]} {record["type"]}, {record["agency_name"]}.\n\n'
                         f'Source date: {manifest["snapshot_date"]}. Source SHA-256: {receipt["sha256"]}.\n\n'
                         f'{manifest["csv_rows"]:,} CSV records = {manifest["valid_rows"]:,} Parquet rows + '
                         f'{manifest["rejected_rows"]:,} preserved rejects. Independent field-value/read-back validation passed.\n\n'
                         'The original ZIP is retained. This scope does not establish national reconciliation. '
                         'snapshot.json lists exact file sizes and SHA-256 values. Zero-byte files are reconstructed from its inline-empty entries.\n')
        existing = json.loads(gh('api', '--method', 'POST', f'repos/{repository}/releases',
                                 '-f', f'tag_name={tag}', '-f', f'target_commitish={commit}',
                                 '-F', 'draft=true', '-F', 'prerelease=true',
                                 '-f', f'name={record["agency_name"]} FY{record["fiscal_year"]} — {manifest["snapshot_date"]}',
                                 '-F', f'body=@{notes}'))
    if existing['target_commitish'] != commit:
        raise RuntimeError('Release belongs to a different code revision; choose a new snapshot revision')
    remote = {asset['name']: asset for asset in release_assets(repository, existing['id'])}
    if set(remote) - set(expected):
        raise RuntimeError('Release contains unexpected assets; no assets replaced or removed')
    for name, asset in remote.items():
        check_remote_asset(asset, expected[name])
    if not existing['draft'] and set(remote) != set(expected):
        raise RuntimeError('Published release is incomplete/different; use a new revision, never overwrite')
    for path, _ in paths:
        if path.name not in expected or path.name in remote:
            continue
        if not existing['draft']:
            raise RuntimeError('Published snapshots are never mutated')
        gh('api', '--method', 'POST',
           f'https://uploads.github.com/repos/{repository}/releases/{existing["id"]}/assets?name={path.name}',
           '-H', 'Content-Type: application/octet-stream', '--input', path)
    for attempt in range(3):
        remote = {asset['name']: asset for asset in release_assets(repository, existing['id'])}
        try:
            if set(remote) != set(expected):
                raise RuntimeError('Release asset inventory is incomplete')
            for name, item in expected.items():
                check_remote_asset(remote[name], item)
            break
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(2)
    if existing['draft']:
        gh('api', '--method', 'PATCH', f'repos/{repository}/releases/{existing["id"]}',
           '-F', 'draft=false', '-F', 'prerelease=true', '-f', 'make_latest=false')
    verified = api(f'repos/{repository}/releases/{existing["id"]}')
    if verified['draft']:
        raise RuntimeError('Snapshot remains a draft')
    return {'tag': tag, 'url': verified['html_url'], 'asset_count': len(expected),
            'remote_sha256_verified': True, 'raw_zip_retained': True}


def pull(repository, tag, root, reserve_gib=8):
    """Fetch metadata, rejection records and Parquet only; verify and atomically expose them."""
    validate_repository(repository)
    validate_name(tag)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / (tag + '.lock')):
        info = release_info(repository, tag)
        if info['draft']:
            raise RuntimeError('Only completed published snapshots may be pulled')
        remote = {entry['name']: entry for entry in release_assets(repository, info['id'])}
        index = remote['snapshot.json']
        if index['size'] > 10 * 1024**2 or not str(index.get('digest', '')).startswith('sha256:'):
            raise RuntimeError('Snapshot index is oversized or has no provider checksum')
        final = root / tag
        if final.exists():
            source = json.loads((final / 'snapshot.json').read_text())
            if sha256(final / 'snapshot.json') != index['digest'][7:]:
                raise RuntimeError('Existing snapshot index differs from release')
            for entry in source['assets']:
                if entry['kind'] != 'raw_zip':
                    validate_name(entry['name'])
                    path = final / entry['name']
                    if path.stat().st_size != entry['bytes'] or sha256(path) != entry['sha256']:
                        raise RuntimeError('Existing local snapshot has changed')
            check_existing(final, source['source_sha256'])
            return final
        staging = root / ('.' + tag + '.staging')
        staging.mkdir()  # Never overwrite a failed or interrupted pull.
        ensure_space(staging, index['size'], int(reserve_gib * GIB))
        gh('release', 'download', tag, '--repo', repository, '--pattern', 'snapshot.json', '--dir', staging)
        if sha256(staging / 'snapshot.json') != index['digest'][7:]:
            raise RuntimeError('Downloaded index checksum differs')
        snapshot = json.loads((staging / 'snapshot.json').read_text())
        if snapshot.get('tag') != tag or snapshot.get('repository') != repository or snapshot.get('format_version') != 1:
            raise ValueError('Snapshot index identity/format differs')
        selected = [entry for entry in snapshot['assets'] if entry['kind'] != 'raw_zip']
        names = [entry['name'] for entry in selected]
        if len(names) != len(set(names)) or 'snapshot.json' in names:
            raise ValueError('Duplicate or reserved asset names')
        for entry in selected:
            validate_name(entry['name'])
            if not isinstance(entry['bytes'], int) or not 0 <= entry['bytes'] < ASSET_LIMIT:
                raise ValueError('Invalid asset size')
            if entry['storage'] == 'inline_empty':
                if entry['bytes'] != 0 or entry['sha256'] != EMPTY_SHA256:
                    raise ValueError('Invalid inline empty file')
            elif entry['storage'] == 'release_asset':
                check_remote_asset(remote[entry['name']], entry)
            else:
                raise ValueError('Unknown asset storage mode')
        ensure_space(staging, sum(entry['bytes'] for entry in selected), int(reserve_gib * GIB))
        for entry in selected:
            path = staging / entry['name']
            ensure_space(staging, entry['bytes'], int(reserve_gib * GIB))
            if entry['storage'] == 'inline_empty':
                path.touch(exist_ok=False)
            else:
                gh('release', 'download', tag, '--repo', repository, '--pattern', entry['name'], '--dir', staging)
            if path.stat().st_size != entry['bytes'] or sha256(path) != entry['sha256']:
                raise RuntimeError(f'Downloaded asset differs: {entry["name"]}')
        check_existing(staging, snapshot['source_sha256'])
        staging.rename(final)
        return final


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('--repo', default='mintyfizz/money-trail')
    parser.add_argument('--output-root', type=Path, default=Path('data/github'))
    parser.add_argument('--reserve-gib', type=float, default=8)
    args = parser.parse_args()
    print(pull(args.repo, args.tag, args.output_root, args.reserve_gib))
