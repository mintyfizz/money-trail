import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ingest.archive import DownloadInterrupted, discover, download, list_agencies, sha256


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('sample.csv', 'id,value\na,1\nb,2\n')
        self.data = memory.getvalue()
        self.record = {'url': 'https://files.usaspending.gov/award_data_archive/sample.zip',
                       'file_name': 'sample.zip', 'http': {'bytes': len(self.data), 'etag': '"v1"',
                                                          'last_modified': 'unchanged'}}
        self.starts = []
        self.mode = 'success'
        self.addCleanup(patch.stopall)
        self.metadata = patch('ingest.archive.head', return_value=self.record['http']).start()
        self.transfer = patch('ingest.archive.fetch_range', side_effect=self.respond).start()

    def respond(self, url, start, end, etag, body, headers):
        self.starts.append(start)
        status = 200 if self.mode == 'ignored' else 206
        range_start = start + 1 if self.mode == 'bad_range' else start
        returned_etag = '"different"' if self.mode == 'changed_get' else etag
        Path(headers).write_text(f'HTTP/1.1 {status} Response\r\nContent-Range: bytes {range_start}-{end}/{len(self.data)}\r\nETag: {returned_etag}\r\n\r\n')
        payload = self.data[start:end + 1]
        if self.mode == 'drop':
            payload = payload[:37]
        if self.mode == 'empty':
            payload = b''
            Path(headers).write_text('')
        Path(body).write_bytes(payload)
        return subprocess.CompletedProcess([], 18 if self.mode == 'drop' else 52 if self.mode == 'empty' else 0, b'', b'connection interrupted')

    def run_download(self, **kwargs):
        return download(self.record, self.root, reserve_gib=0, chunk_mib=0.0001, **kwargs)

    def interrupt(self):
        self.mode = 'drop'
        with self.assertRaises(DownloadInterrupted):
            self.run_download()
        self.assertEqual((self.root / 'sample.zip.part').stat().st_size, 37)
        self.assertFalse((self.root / 'sample.zip').exists())

    def test_disconnected_range_resumes_at_last_saved_byte(self):
        self.interrupt()
        self.mode = 'success'
        result = self.run_download()
        self.assertEqual(result.read_bytes(), self.data)
        self.assertEqual(self.starts[1], 37)
        self.assertFalse((self.root / 'sample.zip.part').exists())
        self.assertEqual(json.loads((self.root / 'sample.zip.receipt.json').read_text())['sha256'], sha256(result))

    def test_changed_source_between_runs_preserves_partial(self):
        self.interrupt()
        self.metadata.return_value = {**self.record['http'], 'etag': '"v2"'}
        with self.assertRaisesRegex(RuntimeError, 'Source metadata changed'):
            self.run_download()
        self.assertEqual(len(self.starts), 1)
        self.assertEqual((self.root / 'sample.zip.part').stat().st_size, 37)

    def test_ignored_range_wrong_offset_and_changed_get_never_append(self):
        self.interrupt()
        before = (self.root / 'sample.zip.part').read_bytes()
        for mode in ('ignored', 'bad_range', 'changed_get'):
            self.mode = mode
            with self.assertRaisesRegex(RuntimeError, 'Unsafe range response'):
                self.run_download()
            self.assertEqual((self.root / 'sample.zip.part').read_bytes(), before)

    def test_empty_reply_preserves_checkpoint(self):
        self.interrupt()
        self.mode = 'empty'
        with self.assertRaises(DownloadInterrupted):
            self.run_download()
        self.assertEqual((self.root / 'sample.zip.part').stat().st_size, 37)

    def test_modified_local_partial_is_not_resumed(self):
        self.interrupt()
        (self.root / 'sample.zip.part').write_bytes(b'x' * 37)
        self.mode = 'success'
        with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            self.run_download()
        self.assertEqual(len(self.starts), 1)

    def test_weak_etag_is_not_used_for_resume(self):
        self.record['http']['etag'] = 'W/"weak"'
        with self.assertRaisesRegex(RuntimeError, 'strong quoted ETag'):
            self.run_download()
        self.assertFalse(self.starts)

    def test_corrupt_complete_zip_is_not_promoted(self):
        self.data = b'corrupt zip with correct HTTP length'
        self.record['http']['bytes'] = len(self.data)
        with self.assertRaises(zipfile.BadZipFile):
            self.run_download()
        self.assertFalse((self.root / 'sample.zip').exists())
        self.assertFalse((self.root / 'sample.zip.receipt.json').exists())

    def test_no_network_transfer_below_reserve(self):
        with patch('ingest.archive.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(RuntimeError, 'Insufficient disk space'):
                self.run_download()
        self.assertFalse(self.starts)


class AgencyDiscoveryTests(unittest.TestCase):
    def test_agencies_are_deduplicated_and_sorted(self):
        response = {'agencies': {'cfo_agencies': [{'toptier_agency_id': 2, 'name': 'B'}],
                                 'other_agencies': [{'toptier_agency_id': 1, 'name': 'A'},
                                                    {'toptier_agency_id': 2, 'name': 'B'}]}}
        with patch('ingest.archive.curl', return_value=json.dumps(response).encode()):
            self.assertEqual([a['toptier_agency_id'] for a in list_agencies()], [1, 2])

    def response(self, agency, kind='contracts'):
        return {'monthly_files': [{'fiscal_year': 2025, 'agency_name': f'Agency {agency}',
                                  'type': kind, 'file_name': f'FY2025_Agency{agency}_Contracts_Full_date.zip',
                                  'url': f'https://files.usaspending.gov/award_data_archive/a{agency}.zip'}]}

    def test_missing_archive_is_recorded_not_replaced_by_national_file(self):
        replies = [json.dumps(self.response(1)).encode(), b'{"monthly_files": []}']
        with patch('ingest.archive.curl', side_effect=replies), patch('ingest.archive.head', return_value={'bytes': 123}):
            result = discover([2025], agency_ids=[1, 2], kinds=['contracts'])
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['checks'][1]['status'], 'no_archive_returned')
        self.assertFalse(result['national_coverage_verified'])

    def test_discovery_failure_saves_prior_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'catalog.json'
            with patch('ingest.archive.curl', side_effect=[json.dumps(self.response(1)).encode(), RuntimeError('empty reply')]), patch('ingest.archive.head', return_value={'bytes': 123}):
                with self.assertRaises(RuntimeError):
                    discover([2025], agency_ids=[1, 2], output=output, kinds=['contracts'])
            result = json.loads(output.read_text())
            self.assertEqual(len(result['files']), 1)
            self.assertFalse(result['complete'])
            self.assertEqual(result['error'], 'empty reply')

    def test_national_fallback_is_rejected_for_agency(self):
        response = self.response(1)
        response['monthly_files'][0]['agency_name'] = 'All'
        with patch('ingest.archive.curl', return_value=json.dumps(response).encode()):
            with self.assertRaisesRegex(ValueError, 'National archive'):
                discover([2025], agency_ids=[1], kinds=['contracts'])
