import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ingest.archive import DownloadInterrupted, save_json, select_records
from pipeline.pilot import run


class PilotTests(unittest.TestCase):
    def test_interrupt_resume_verify_process_then_reuse_without_network(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('data.csv', 'id,amount\n001,1.20\n002,-0.20\n')
        data = payload.getvalue()
        name = 'FY2025_TestAgency_Contracts_Full_fixture.zip'
        record = {'file_name': name, 'type': 'contracts', 'agency_id': 1,
                  'url': 'https://files.usaspending.gov/award_data_archive/' + name,
                  'http': {'bytes': len(data), 'etag': '"v1"', 'last_modified': 'fixture'}}
        starts = []
        def transfer(url, start, end, etag, body, headers):
            starts.append(start)
            dropped = len(starts) == 1
            Path(body).write_bytes(data[start:start + 31] if dropped else data[start:end + 1])
            Path(headers).write_text(f'HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{end}/{len(data)}\r\nETag: {etag}\r\n\r\n')
            return subprocess.CompletedProcess([], 18 if dropped else 0, b'', b'')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / 'catalog.json'
            save_json(catalog, {'complete': True, 'files': [record]})
            with patch('ingest.archive.head', return_value=record['http']), patch('ingest.archive.fetch_range', side_effect=transfer) as request:
                with self.assertRaises(DownloadInterrupted):
                    run(catalog, root / 'data', reserve_gib=0)
                self.assertEqual(json.loads((root / 'data/pilot-run.json').read_text())['status'], 'interrupted')
                self.assertFalse((root / 'data/bronze').exists())
                report = run(catalog, root / 'data', reserve_gib=0)
                self.assertEqual(starts, [0, 31])
                self.assertEqual(report['files'][0]['valid_rows'], 2)
                self.assertEqual(report['status'], 'complete_for_selected_files')
                self.assertTrue((root / 'data/raw' / name).exists())
                run(catalog, root / 'data', reserve_gib=0)
                self.assertEqual(request.call_count, 2)

    def test_overlapping_national_and_agency_sources_are_rejected(self):
        records = [{'type': 'contracts', 'file_name': 'FY2025_All_Contracts.zip', 'url': 'a', 'http': {'bytes': 20}},
                   {'type': 'contracts', 'file_name': 'FY2025_Agency_Contracts.zip', 'url': 'b', 'http': {'bytes': 10}}]
        with self.assertRaisesRegex(ValueError, 'overlap'):
            select_records(records)
