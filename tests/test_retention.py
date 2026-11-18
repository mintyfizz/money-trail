import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ingest.archive import save_json, sha256, verify_zip
from pipeline.bronze import convert
from pipeline.pilot import run
from pipeline.retention import release, reuse_deleted


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'raw').mkdir()

    def fixture(self, name, text='id,value\n001,1.00\n', snapshot='2026-11-06', extra=False):
        path = self.root / 'raw' / (name + '.zip')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('source.csv', text)
            if extra:
                archive.writestr('readme.txt', 'original source context')
        receipt = {'url': 'https://files.usaspending.gov/award_data_archive/' + path.name,
                   'bytes': path.stat().st_size, 'sha256': sha256(path), 'etag': '"fixture"',
                   'zip_crc_status': 'verified', 'zip': verify_zip(path), 'snapshot_date': snapshot}
        save_json(str(path) + '.receipt.json', receipt)
        convert(path, self.root / 'bronze', reserve_gib=0)
        record = {'file_name': path.name, 'type': 'contracts', 'agency_id': 1, 'url': receipt['url'],
                  'updated_date': snapshot, 'http': {'bytes': receipt['bytes'], 'etag': receipt['etag']}}
        return path, self.root / 'bronze' / name, record

    def pin(self):
        path, directory, _ = self.fixture('small')
        self.assertEqual(release(path, directory, reserve_gib=0)['reason'], 'permanent_test_fixture')
        return path

    def test_delete_only_after_verified_preservation_and_reuse_without_network(self):
        pinned = self.pin()
        path, directory, record = self.fixture('next', 'id,value\n001,1.00\nbad,2,extra\n002,-2.00\n')
        state = release(path, directory, reserve_gib=0)
        self.assertEqual(state['status'], 'deleted')
        self.assertEqual(state['csv_rows'], 3)
        self.assertEqual(state['valid_rows'] + state['rejected_rows'], 3)
        self.assertFalse(path.exists())
        self.assertTrue(pinned.exists())
        self.assertTrue(Path(str(path) + '.receipt.json').exists())
        catalog = self.root / 'catalog.json'
        save_json(catalog, {'complete': True, 'files': [record]})
        with patch('pipeline.pilot.download', side_effect=AssertionError('must not redownload')):
            report = run(catalog, self.root, reserve_gib=0)
        self.assertEqual(report['files'][0]['retention']['status'], 'already_deleted')
        self.assertEqual(report['files'][0]['parquet_bytes'], reuse_deleted(record, self.root)['parquet_bytes'])

    def test_bad_row_counts_prevent_deletion(self):
        self.pin()
        path, directory, _ = self.fixture('bad_count')
        manifest = json.loads((directory / 'manifest.json').read_text())
        manifest['csv_rows'] += 1
        save_json(directory / 'manifest.json', manifest)
        with self.assertRaisesRegex(RuntimeError, 'row reconciliation'):
            release(path, directory, reserve_gib=0)
        self.assertTrue(path.exists())

    def test_changed_parquet_or_reject_ledger_prevents_deletion(self):
        self.pin()
        for name in ('parquet', 'rejects'):
            with self.subTest(name=name):
                path, directory, _ = self.fixture(name)
                artifact = next(directory.glob('*.parquet')) if name == 'parquet' else directory / 'rejects.jsonl'
                artifact.write_bytes(b'corrupt')
                with self.assertRaisesRegex(RuntimeError, 'differs from its manifest'):
                    release(path, directory, reserve_gib=0)
                self.assertTrue(path.exists())

    def test_missing_snapshot_non_csv_members_and_large_fixture_keep_zip(self):
        for name, snapshot, extra in [('missing_date', None, False), ('extra', '2026-11-06', True)]:
            path, directory, _ = self.fixture(name, snapshot=snapshot, extra=extra)
            self.assertEqual(release(path, directory, reserve_gib=0)['status'], 'retained')
            self.assertTrue(path.exists())
        path, directory, _ = self.fixture('too_large')
        self.assertEqual(release(path, directory, reserve_gib=0, fixture_max_mib=0)['reason'],
                         'awaiting_small_nonempty_agency_fixture')
        self.assertTrue(path.exists())

    def test_changed_fixture_blocks_deletion_of_others(self):
        fixture = self.pin()
        fixture.write_bytes(b'changed')
        path, directory, _ = self.fixture('next')
        with self.assertRaisesRegex(RuntimeError, 'verified integrity receipt'):
            release(path, directory, reserve_gib=0)
        self.assertTrue(path.exists())

    def test_no_deletion_when_evidence_cannot_be_saved(self):
        self.pin()
        path, directory, _ = self.fixture('next')
        with patch('pipeline.retention.durable_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                release(path, directory, reserve_gib=0)
        self.assertTrue(path.exists())

    def test_crash_after_unlink_reuses_durable_predelete_evidence(self):
        from pipeline import retention
        self.pin()
        path, directory, record = self.fixture('next')
        original = retention.durable_json
        def crash_after_unlink(destination, value, reserve):
            if value.get('status') == 'deleted':
                raise OSError('crash')
            return original(destination, value, reserve)
        with patch.object(retention, 'durable_json', side_effect=crash_after_unlink):
            with self.assertRaisesRegex(OSError, 'crash'):
                release(path, directory, reserve_gib=0)
        self.assertFalse(path.exists())
        self.assertEqual(reuse_deleted(record, self.root)['valid_rows'], 1)

    def test_changed_catalog_or_manifest_after_deletion_fails_closed(self):
        self.pin()
        path, directory, record = self.fixture('next')
        release(path, directory, reserve_gib=0)
        record['updated_date'] = '2026-12-06'
        with self.assertRaisesRegex(RuntimeError, 'source identity'):
            reuse_deleted(record, self.root)
        record['updated_date'] = '2026-11-06'
        with (directory / 'manifest.json').open('a') as stream:
            stream.write(' ')
        with self.assertRaisesRegex(RuntimeError, 'manifest changed'):
            reuse_deleted(record, self.root)

    def test_keep_zips_override(self):
        path, _, record = self.fixture('keep')
        catalog = self.root / 'catalog.json'
        save_json(catalog, {'files': [record]})
        with patch('pipeline.pilot.download', return_value=path):
            result = run(catalog, self.root, reserve_gib=0, keep_zips=True)
        self.assertEqual(result['files'][0]['retention']['reason'], 'keep_zips_option')
        self.assertTrue(path.exists())
