import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ingest.archive import save_json, sha256, verify_zip, GIB
from pipeline.bronze import convert
from pipeline import snapshots


class ReleaseServer:
    """Small fake of the release API, including the provider-side SHA-256 digest."""
    def __init__(self):
        self.info = None
        self.files = {}
        self.downloads = []
        self.corrupt_digest = False
        self.corrupt_download = False
        self.published = False

    def assets(self):
        return [{'name': name, 'size': len(data), 'state': 'uploaded',
                 'digest': 'sha256:' + ('0' * 64 if self.corrupt_digest else hashlib.sha256(data).hexdigest())}
                for name, data in self.files.items()]

    def __call__(self, *args):
        args = list(map(str, args))
        if args[0] == 'api':
            if args[1:3] == ['--paginate', '--slurp']:
                return json.dumps([self.assets()])
            if '/releases/tags/' in args[1]:
                if self.info is None:
                    raise subprocess.CalledProcessError(1, args, stderr='HTTP 404')
                return json.dumps(self.info)
            return '{}'
        action, tag = args[1:3]
        if action == 'create':
            self.info = {'id': 1, 'draft': True, 'html_url': 'https://github.com/test/repo/releases/tag/' + tag}
        elif action == 'upload':
            path = Path(args[3])
            if not self.info['draft'] or path.name in self.files:
                raise AssertionError('Unexpected published overwrite')
            self.files[path.name] = path.read_bytes()
        elif action == 'edit':
            self.info['draft'] = False
            self.published = True
        elif action == 'download':
            name = args[args.index('--pattern') + 1]
            destination = Path(args[args.index('--dir') + 1]) / name
            self.downloads.append(name)
            destination.write_bytes(b'changed' if self.corrupt_download else self.files[name])
        else:
            raise AssertionError(args)
        return ''


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.raw = self.root / 'agency.zip'
        with zipfile.ZipFile(self.raw, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('source.csv', 'id,value\n001,1.00\n002,-2.00\n')
        self.url = 'https://files.usaspending.gov/award_data_archive/agency.zip'
        save_json(str(self.raw) + '.receipt.json', {'url': self.url, 'bytes': self.raw.stat().st_size,
                  'sha256': sha256(self.raw), 'etag': '"source"', 'zip_crc_status': 'verified',
                  'zip': verify_zip(self.raw), 'snapshot_date': '2026-11-06'})
        convert(self.raw, self.root / 'bronze', reserve_gib=0)
        self.directory = self.root / 'bronze' / 'agency'
        self.record = {'url': self.url, 'agency_id': 1, 'agency_name': 'Test Agency',
                       'fiscal_year': 2025, 'type': 'contracts'}
        save_json(self.root / 'run.json', {'selected_source': self.record})
        save_json(self.root / 'catalog.json', {'files': [self.record], 'complete': True})
        self.server = ReleaseServer()
        mock = patch('pipeline.snapshots.gh', side_effect=self.server)
        mock.start()
        self.addCleanup(mock.stop)

    def publish(self):
        return snapshots.publish(self.raw, self.directory, self.root / 'catalog.json',
                                 self.root / 'run.json', 'test/repo', 'a' * 40, reserve_gib=0)

    def test_publish_retains_raw_and_pull_downloads_only_derived_files(self):
        result = self.publish()
        self.assertTrue(self.raw.exists())
        self.assertTrue(self.server.published)
        self.assertEqual(self.server.files['agency.zip'], self.raw.read_bytes())
        self.assertNotIn('rejects.jsonl', self.server.files)
        target = snapshots.pull('test/repo', result['tag'], self.root / 'consumer', reserve_gib=0)
        self.assertFalse((target / 'agency.zip').exists())
        self.assertNotIn('agency.zip', self.server.downloads)
        self.assertEqual((target / 'rejects.jsonl').read_bytes(), b'')
        self.assertEqual(len(list(target.glob('*.parquet'))), 1)
        self.assertEqual(snapshots.pull('test/repo', result['tag'], self.root / 'consumer', 0), target)
        self.assertEqual(self.publish()['tag'], result['tag'])

    def test_remote_digest_failure_leaves_draft(self):
        self.server.corrupt_digest = True
        with patch('pipeline.snapshots.time.sleep'), self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.publish()
        self.assertTrue(self.server.info['draft'])
        self.assertFalse(self.server.published)
        self.assertTrue(self.raw.exists())

    def test_changed_rebuild_does_not_overwrite_published_assets(self):
        self.publish()
        original = dict(self.server.files)
        save_json(self.root / 'run.json', {'selected_source': self.record, 'different_run': True})
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.publish()
        self.assertEqual(self.server.files, original)

    def test_corrupt_download_never_promotes_snapshot(self):
        result = self.publish()
        self.server.corrupt_download = True
        root = self.root / 'consumer'
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            snapshots.pull('test/repo', result['tag'], root, 0)
        self.assertFalse((root / result['tag']).exists())

    def test_local_tampering_is_detected_on_reuse(self):
        result = self.publish()
        root = self.root / 'consumer'
        target = snapshots.pull('test/repo', result['tag'], root, 0)
        next(target.glob('*.parquet')).write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            snapshots.pull('test/repo', result['tag'], root, 0)

    def test_disk_reserve_stops_before_downloading(self):
        result = self.publish()
        with patch('ingest.archive.shutil.disk_usage') as usage:
            usage.return_value.free = 8 * GIB
            with self.assertRaisesRegex(RuntimeError, 'disk space'):
                snapshots.pull('test/repo', result['tag'], self.root / 'consumer')
        self.assertEqual(self.server.downloads, [])

    def test_unsafe_index_paths_are_rejected(self):
        result = self.publish()
        index = json.loads(self.server.files['snapshot.json'])
        index['assets'][1]['name'] = '../outside.json'
        self.server.files['snapshot.json'] = json.dumps(index).encode()
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            snapshots.pull('test/repo', result['tag'], self.root / 'consumer', 0)
        self.assertEqual(self.server.downloads, ['snapshot.json'])

    def test_asset_size_limit_is_strict(self):
        with patch('pipeline.snapshots.ASSET_LIMIT', self.raw.stat().st_size):
            with self.assertRaisesRegex(ValueError, 'smaller than'):
                snapshots.asset_metadata(self.raw, 'raw_zip')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            snapshots.validate_name('../bad')

    def test_corrupt_parquet_stops_before_creating_release(self):
        next(self.directory.glob('*.parquet')).write_bytes(b'bad')
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertIsNone(self.server.info)
