import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import pyarrow.parquet as parquet

from ingest.archive import save_json, sha256, verify_zip
from pipeline.bronze import convert


class BronzeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'sample.zip'
        self.out = self.root / 'bronze'

    def fixture(self, text):
        with zipfile.ZipFile(self.raw, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('source.csv', text)
        save_json(str(self.raw) + '.receipt.json', {'url': 'https://files.usaspending.gov/award_data_archive/sample.zip',
                  'bytes': self.raw.stat().st_size, 'sha256': sha256(self.raw), 'zip_crc_status': 'verified',
                  'zip': verify_zip(self.raw)})

    def test_round_trip_preserves_strings_newlines_missing_and_invalid_amounts(self):
        self.fixture('id,county,amount,description\n001,01001,-0.10,"line one\nline two"\n002,,NaN,\n003,00001,NULL,text\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        files = list((self.out / 'sample').glob('*.parquet'))
        table = parquet.read_table(files)
        self.assertEqual(table['county'].to_pylist(), ['01001', '', '00001'])
        self.assertEqual(table['amount'].to_pylist(), ['-0.10', 'NaN', 'NULL'])
        self.assertEqual(table['description'][0].as_py(), 'line one\nline two')
        self.assertEqual(result['valid_rows'], 3)
        self.assertTrue(self.raw.exists())
        self.assertEqual(convert(self.raw, self.out, reserve_gib=0), result)

    def test_wrong_width_is_retained_in_reject_ledger(self):
        self.fixture('id,value\na,1\nb,2,extra\nc,3\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        self.assertEqual(result['valid_rows'], 2)
        self.assertEqual(result['rejected_rows'], 1)
        rejected = json.loads((self.out / 'sample/rejects.jsonl').read_text())
        self.assertEqual(rejected['raw_record'], 'b,2,extra')

    def test_processing_requires_verified_original(self):
        self.fixture('id,value\na,1\n')
        receipt_path = Path(str(self.raw) + '.receipt.json')
        receipt = json.loads(receipt_path.read_text())
        receipt['zip_crc_status'] = 'pending'
        save_json(receipt_path, receipt)
        with self.assertRaisesRegex(RuntimeError, 'verified integrity receipt'):
            convert(self.raw, self.out, reserve_gib=0)
        self.assertFalse((self.out / 'sample').exists())

    def test_output_cap_does_not_publish_partial_or_delete_raw(self):
        self.fixture('id,value\na,1\n')
        with self.assertRaisesRegex(RuntimeError, 'output cap'):
            convert(self.raw, self.out, reserve_gib=0, max_output_gib=1 / 1024**3)
        self.assertFalse((self.out / 'sample').exists())
        self.assertTrue((self.out / '.sample.staging').exists())
        self.assertTrue(self.raw.exists())

    def test_header_only_retains_schema(self):
        self.fixture('id,value\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        self.assertEqual(result['valid_rows'], 0)
        self.assertEqual(parquet.read_schema(next((self.out / 'sample').glob('*.parquet'))).names, ['id', 'value'])

    def test_corrupted_completed_output_is_not_silently_reused(self):
        self.fixture('id,value\na,1\n')
        convert(self.raw, self.out, reserve_gib=0)
        next((self.out / 'sample').glob('*.parquet')).write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'differs from its manifest'):
            convert(self.raw, self.out, reserve_gib=0)

    def test_multiple_batches_and_parts_preserve_all_records(self):
        self.fixture('id,value\n' + ''.join(f'{i:06d},{i}.25\n' for i in range(3000)))
        result = convert(self.raw, self.out, reserve_gib=0, block_mib=0.002, part_mib=0.001)
        files = sorted((self.out / 'sample').glob('*.parquet'))
        self.assertGreater(len(files), 1)
        self.assertEqual(result['valid_rows'], 3000)
        self.assertEqual(parquet.read_table(files)['id'].to_pylist(), [f'{i:06d}' for i in range(3000)])

    def test_disk_reserve_stops_processing_without_publishing(self):
        self.fixture('id,value\na,1\n')
        from unittest.mock import patch
        with patch('ingest.archive.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(RuntimeError, 'Insufficient disk space'):
                convert(self.raw, self.out, reserve_gib=8)
        self.assertFalse((self.out / 'sample').exists())
        self.assertTrue(self.raw.exists())
