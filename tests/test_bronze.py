import csv
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import pyarrow.parquet as parquet

from ingest.archive import save_json, sha256, verify_zip
from pipeline.bronze import PROVENANCE, convert


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
                  'zip': verify_zip(self.raw), 'snapshot_date': '2026-11-06'})

    def test_round_trip_preserves_strings_newlines_missing_and_invalid_amounts(self):
        self.fixture('id,county,amount,description\n001,01001,-0.10,"line one\nline two"\n002,,NaN,\n003,00001,NULL,text\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        files = list((self.out / 'sample').glob('*.parquet'))
        table = parquet.read_table(files)
        self.assertEqual(table['county'].to_pylist(), ['01001', '', '00001'])
        self.assertEqual(table['amount'].to_pylist(), ['-0.10', 'NaN', 'NULL'])
        self.assertEqual(table['description'][0].as_py(), 'line one\nline two')
        self.assertEqual(result['valid_rows'], 3)
        self.assertEqual(table['_mt_source_row_number'].to_pylist(), [2, 3, 4])
        self.assertEqual(table['_mt_source_file'].to_pylist(), ['source.csv'] * 3)
        self.assertEqual(result['source_bytes'], self.raw.stat().st_size)
        self.assertEqual(result['snapshot_date'], '2026-11-06')
        self.assertTrue(self.raw.exists())
        self.assertEqual(convert(self.raw, self.out, reserve_gib=0), result)

    def test_wrong_width_is_retained_in_reject_ledger(self):
        self.fixture('id,value\na,1\nb,2,extra\nc,3\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        self.assertEqual(result['valid_rows'], 2)
        self.assertEqual(result['rejected_rows'], 1)
        rejected = json.loads((self.out / 'sample/rejects.jsonl').read_text())
        self.assertEqual(rejected['raw_record'], 'b,2,extra\n')
        self.assertEqual(rejected['source_row_number'], 3)
        self.assertEqual(result['csv_rows'], 3)
        table = parquet.read_table(sorted((self.out / 'sample').glob('*.parquet')))
        self.assertEqual(table['_mt_source_row_number'].to_pylist(), [2, 4])

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
        self.assertEqual(parquet.read_schema(next((self.out / 'sample').glob('*.parquet'))).names, ['id', 'value', *PROVENANCE])

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

    def test_utf8_bom_crlf_quotes_whitespace_and_no_final_newline(self):
        self.fixture('\ufeffid,value\r\n001," spaces "\r\n002,"say ""hi""\r\nnext"\r\n003,é😀')
        result = convert(self.raw, self.out, reserve_gib=0)
        table = parquet.read_table(sorted((self.out / 'sample').glob('*.parquet')))
        self.assertEqual(table['value'].to_pylist(), [' spaces ', 'say "hi"\r\nnext', 'é😀'])
        self.assertEqual(table['_mt_source_row_number'].to_pylist(), [2, 3, 4])
        self.assertEqual(result['csv_rows'], 3)

    def test_malformed_quotes_invalid_utf8_and_provenance_collision_keep_zip(self):
        for text in ('id,value\n001,"unterminated\n', b'id,value\n001,\xff\n',
                     '_mt_source_file,value\na,1\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as output:
                self.fixture(text)
                with self.assertRaises((ValueError, RuntimeError, UnicodeError, csv.Error)):
                    convert(self.raw, output, reserve_gib=0)
                self.assertFalse((Path(output) / 'sample').exists())
                self.assertTrue(self.raw.exists())

    def test_same_row_count_with_changed_values_fails_readback(self):
        import pyarrow as pa
        from unittest.mock import patch
        from pipeline import bronze
        self.fixture('id,value\n001,1.00\n002,2.00\n')
        original = bronze.arrow_csv.open_csv

        class ChangedReader:
            def __init__(self, *args, **kwargs):
                self.reader = original(*args, **kwargs)
                self.schema = self.reader.schema
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.reader.close()
            def __iter__(self):
                for batch in self.reader:
                    yield pa.record_batch([batch.column(0), pa.array(['changed'] * batch.num_rows)], schema=self.schema)

        with patch.object(bronze.arrow_csv, 'open_csv', ChangedReader):
            with self.assertRaisesRegex(RuntimeError, 'read-back differs'):
                convert(self.raw, self.out, reserve_gib=0)
        self.assertFalse((self.out / 'sample').exists())
        self.assertTrue(self.raw.exists())

    def test_multiple_members_reset_row_numbers(self):
        with zipfile.ZipFile(self.raw, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('first.csv', 'id,value\na,1\n')
            archive.writestr('nested/second.csv', 'id,value\nb,2\n')
        save_json(str(self.raw) + '.receipt.json', {'url': 'fixture', 'bytes': self.raw.stat().st_size,
                  'sha256': sha256(self.raw), 'zip_crc_status': 'verified', 'zip': verify_zip(self.raw)})
        result = convert(self.raw, self.out, reserve_gib=0)
        table = parquet.read_table(sorted((self.out / 'sample').glob('*.parquet')))
        self.assertEqual(table['_mt_source_row_number'].to_pylist(), [2, 2])
        self.assertEqual(table['_mt_source_file'].to_pylist(), ['first.csv', 'nested/second.csv'])
        self.assertEqual(result['csv_rows'], 2)

    def test_rejected_multiline_record_and_trailing_rejects_are_preserved(self):
        self.fixture('id,value\r\n001,ok\r\nbad,"two\r\nlines",extra\r\n002,end\r\nbad,last,extra')
        result = convert(self.raw, self.out, reserve_gib=0)
        self.assertEqual((result['csv_rows'], result['valid_rows'], result['rejected_rows']), (4, 2, 2))
        rows = [json.loads(line) for line in (self.out / 'sample/rejects.jsonl').read_text().splitlines()]
        self.assertEqual([row['source_row_number'] for row in rows], [3, 5])
        self.assertEqual(rows[0]['raw_record'], 'bad,"two\r\nlines",extra\r\n')
        self.assertEqual(rows[1]['raw_record'], 'bad,last,extra')

    def test_all_rejected_rows_still_have_schema_and_independent_count(self):
        self.fixture('id,value\na,b,c\nd,e,f\n')
        result = convert(self.raw, self.out, reserve_gib=0)
        self.assertEqual((result['csv_rows'], result['valid_rows'], result['rejected_rows']), (2, 0, 2))
        self.assertTrue(list((self.out / 'sample').glob('*.parquet')))
