import csv
import io
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from ingest.archive import archive_url, download, ensure_space, profile, profile_csv, sha256
from ingest.sample_prefix import sample


class ArchiveTests(unittest.TestCase):
    def test_csv_records_include_quoted_newlines_and_exact_decimal_sum(self):
        data = ('contract_transaction_unique_key,federal_action_obligation,award_type_code,description\n'
                'a,0.10,A,"line one\nline two"\n'
                'b,0.20,A,other\n'
                'c,-0.05,B,adjustment\n')
        result = profile_csv(io.BytesIO(data.encode()), 'sample.csv')
        self.assertEqual(result['rows'], 3)
        self.assertEqual(result['federal_action_obligation']['sum'], '0.25')
        self.assertEqual(result['federal_action_obligation']['negative_rows'], 1)

    def test_bad_width_and_invalid_amounts_remain_visible(self):
        data = ('contract_transaction_unique_key,federal_action_obligation\n'
                ',\nwrong,width,extra\nx,NaN\ny,invalid\n')
        result = profile_csv(io.BytesIO(data.encode()), 'sample.csv')
        self.assertEqual(result['rows'], 4)
        self.assertEqual(result['width_errors'], 1)
        self.assertEqual(result['federal_action_obligation']['invalid_rows'], 2)
        self.assertEqual(result['federal_action_obligation']['missing_rows'], 1)
        self.assertEqual(result['missing_counts']['contract_transaction_unique_key'], 1)

    def test_bad_quotes_fail_closed(self):
        with self.assertRaises(csv.Error):
            profile_csv(io.BytesIO(b'id,value\na,"unfinished'), 'sample.csv')

    def test_unsafe_source_is_rejected(self):
        for url in ['http://files.usaspending.gov/award_data_archive/a.zip',
                    'https://evil.example/award_data_archive/a.zip',
                    'https://user@files.usaspending.gov/award_data_archive/a.zip']:
            with self.assertRaises(ValueError):
                archive_url(url)

    def test_disk_reserve_prevents_download(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('ingest.archive.shutil.disk_usage') as usage:
                usage.return_value.free = 100
                with self.assertRaises(RuntimeError):
                    ensure_space(directory, 50, 60)

    def test_zip_profile_reads_all_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.zip'
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('a.csv', 'id,value\na,1\nb,2\n')
                archive.writestr('b.csv', 'id,value\nc,3\n')
                archive.writestr('readme.txt', 'test fixture')
            result = profile(path)
            self.assertEqual(result['rows'], 3)
            self.assertEqual(len(result['members']), 2)
            self.assertEqual(result['sha256'], sha256(path))

    def test_truncated_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.zip'
            path.write_bytes(b'PK\x03\x04truncated')
            with self.assertRaises(zipfile.BadZipFile):
                profile(path)

    def test_corrupted_member_crc_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corrupt.zip'
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
                archive.writestr('a.csv', 'id,value\na,1\n')
            data = bytearray(path.read_bytes())
            data[data.index(b'id,value')] = ord('x')
            path.write_bytes(data)
            with self.assertRaises(zipfile.BadZipFile):
                profile(path)

    def test_completed_download_receipt_and_idempotent_reuse(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('a.csv', 'id,value\na,1\n')
        data = payload.getvalue()
        record = {'url': 'https://files.usaspending.gov/award_data_archive/sample.zip',
                  'file_name': 'sample.zip',
                  'http': {'bytes': len(data), 'etag': '"version-1"', 'last_modified': 'date'}}
        def transfer(url, start, end, etag, body, headers):
            Path(body).write_bytes(data[start:end + 1])
            Path(headers).write_text(f'HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{end}/{len(data)}\r\nETag: {etag}\r\n\r\n')
            return subprocess.CompletedProcess([], 0, b'', b'')
        with tempfile.TemporaryDirectory() as directory:
            with patch('ingest.archive.head', return_value=record['http']), patch('ingest.archive.fetch_range', side_effect=transfer) as request:
                first = download(record, directory, reserve_gib=0)
                second = download(record, directory, reserve_gib=0)
                self.assertEqual(first, second)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(profile(first)['rows'], 1)
                self.assertTrue(Path(str(first) + '.receipt.json').exists())
                self.assertEqual(json.loads(Path(str(first) + '.receipt.json').read_text())['zip_crc_status'], 'verified')

    def test_prefix_sample_is_explicitly_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prefix.zip.partial'
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('a.csv', 'id,value\na,1\nb,2\nc,3\n')
            path.write_bytes(path.read_bytes()[:-22])
            result = sample(path, 2)
            self.assertEqual(result['sample']['rows'], 2)
            self.assertEqual(result['status'], 'INCOMPLETE_ARCHIVE_PREFIX_SAMPLE')
            self.assertIsNone(result['total_archive_rows'])
            with self.assertRaises(ValueError):
                sample(path, 99)

    def test_tampered_existing_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            name = 'sample.zip'
            target = Path(directory) / name
            target.write_bytes(b'changed')
            url = 'https://files.usaspending.gov/award_data_archive/' + name
            (Path(directory) / (name + '.receipt.json')).write_text(json.dumps({
                'url': url, 'etag': 'etag', 'sha256': 'different'}))
            record = {'url': url, 'file_name': name, 'http': {'bytes': 7, 'etag': 'etag'}}
            with self.assertRaises(RuntimeError):
                download(record, directory)


if __name__ == '__main__':
    unittest.main()
