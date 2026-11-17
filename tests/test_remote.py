import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ingest import archive
from pipeline import remote


class RemoteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.records = [{'file_name': f'agency{n}.zip', 'agency_id': n, 'agency_name': f'Agency {n}',
                         'type': 'contracts', 'fiscal_year': 2025, 'http': {'bytes': n * 100},
                         'url': f'https://files.usaspending.gov/award_data_archive/agency{n}.zip'} for n in (1, 2)]

    def convert(self, path, *args, **kwargs):
        rows = 0 if path.name == 'agency1.zip' else 3
        return {'source_file': path.name, 'source_sha256': 'a' * 64, 'source_bytes': 200,
                'snapshot_date': '2026-11-06', 'csv_rows': rows, 'valid_rows': rows, 'rejected_rows': 0,
                'parquet_bytes': 500, 'parquet_to_zip_ratio': 2.5, 'elapsed_seconds': 1}

    def run_pilot(self, records=None):
        catalog = {'complete': True, 'files': self.records if records is None else records}
        with patch.object(remote.archive, 'discover', return_value=catalog), \
             patch.object(remote.archive, 'download', side_effect=lambda r, *a, **k: self.root / r['file_name']) as download, \
             patch.object(remote, 'convert', side_effect=self.convert), \
             patch.object(remote, 'publish', return_value={'url': 'https://example.com/release'}) as publish:
            result = remote.run(2025, 'contracts', 'smallest', self.root, 'test/repo', 'a' * 40)
            self.assertEqual(publish.call_count, 1)
            self.assertEqual(publish.call_args.kwargs['reserve_gib'], 8)
            for call in download.call_args_list:
                self.assertEqual(call.kwargs['reserve_gib'], 8)
            return result

    def test_header_only_candidate_is_skipped_before_publication(self):
        result = self.run_pilot()
        self.assertEqual(result['selected_source']['agency_id'], 2)
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(result['selected']['valid_rows'], 3)

    def test_smallest_mode_stops_before_large_download(self):
        self.records[0]['http']['bytes'] = 65 * archive.MIB
        with self.assertRaisesRegex(RuntimeError, 'larger files not started'):
            self.run_pilot(self.records[:1])
        self.assertEqual(json.loads((self.root / 'run.json').read_text())['status'], 'failed')

    def test_incomplete_catalog_cannot_establish_smallest(self):
        with patch.object(remote.archive, 'discover', return_value={'complete': False}), \
             patch.object(remote.archive, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'Complete discovery'):
                remote.run(2025, 'contracts', 'smallest', self.root, 'test/repo', 'a' * 40)
            download.assert_not_called()

    def test_invalid_inputs_stop_before_discovery(self):
        with patch.object(remote.archive, 'discover') as discover:
            for agency in ('../bad', '-1'):
                with self.assertRaises(ValueError):
                    remote.run(2025, 'contracts', agency, self.root, 'test/repo', 'a' * 40)
            discover.assert_not_called()
