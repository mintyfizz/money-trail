"""Run a bounded, raw-preserving ingestion on a GitHub runner and publish verified assets."""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from ingest import archive
from pipeline.bronze import convert
from pipeline.snapshots import ASSET_LIMIT, publish, validate_repository


@contextmanager
def metadata_retries():
    original = archive.curl
    def request(*args, **kwargs):
        for attempt in range(3):
            try:
                return original(*args, **kwargs)
            except subprocess.CalledProcessError as error:
                message = error.stderr.decode(errors='replace')
                temporary = error.returncode in (18, 28, 52, 56) or (error.returncode == 22 and
                            any(str(code) in message for code in (429, 500, 502, 503, 504)))
                if not temporary or attempt == 2:
                    raise
                time.sleep((5, 15)[attempt])
    archive.curl = request
    try:
        yield
    finally:
        archive.curl = original


def run(year, kind, agency, root, repository, commit, max_output_gib=2, revision=1, publish_assets=True):
    validate_repository(repository)
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit) or revision < 1:
        raise ValueError('A full commit SHA and positive snapshot revision are required')
    if not math.isfinite(max_output_gib) or max_output_gib <= 0:
        raise ValueError('Output limit must be finite and positive')
    if kind not in ('contracts', 'assistance') or not 2008 <= year <= 2100:
        raise ValueError('Invalid fiscal year or award kind')
    if agency not in ('smallest', 'all') and not re.fullmatch(r'[1-9][0-9]*', agency):
        raise ValueError('Agency must be smallest, all, or a positive agency database ID')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    report_path, catalog_path = root / 'run.json', root / 'catalog.json'
    report = {'started_at': archive.now(), 'status': 'discovering', 'code_commit': commit,
              'runner': os.environ.get('RUNNER_ENVIRONMENT', 'local'),
              'run_url': os.environ.get('GITHUB_SERVER_URL', 'https://github.com') + '/' + repository +
                         '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', 'local'),
              'free_disk_bytes_before': shutil.disk_usage(root).free, 'reserve_gib': 8,
              'source_zip_policy': 'retain_original_in_verified_release', 'candidates': []}
    with archive.exclusive_lock(root / 'remote.lock'), metadata_retries():
        archive.save_json(report_path, report)
        try:
            selected = None if agency == 'smallest' else ('all' if agency == 'all' else int(agency))
            catalog = archive.discover([year], agency=selected, output=catalog_path, kinds=(kind,))
            if not catalog['complete']:
                raise RuntimeError('Complete discovery required before selecting the smallest agency')
            for record in archive.select_records(catalog['files'], kind):
                if record['http']['bytes'] >= ASSET_LIMIT:
                    raise ValueError('Raw ZIP exceeds GitHub release per-file limit; select a smaller agency')
                if agency == 'smallest' and record['http']['bytes'] > 64 * archive.MIB:
                    raise RuntimeError('No nonempty pilot found below 64 MiB; larger files not started')
                report.update(status='downloading', selected_source=record)
                archive.save_json(report_path, report)
                for attempt in range(3):
                    try:
                        raw = archive.download(record, root / 'raw', reserve_gib=8)
                        break
                    except archive.DownloadInterrupted:
                        if attempt == 2:
                            raise
                        time.sleep((5, 15)[attempt])
                report['status'] = 'converting'
                archive.save_json(report_path, report)
                result = convert(raw, root / 'bronze', reserve_gib=8, max_output_gib=max_output_gib)
                measurement = {key: result[key] for key in ('source_file', 'source_sha256', 'source_bytes',
                              'snapshot_date', 'csv_rows', 'valid_rows', 'rejected_rows', 'parquet_bytes',
                              'parquet_to_zip_ratio', 'elapsed_seconds')}
                report['candidates'].append(measurement)
                archive.save_json(report_path, report)
                if agency == 'smallest' and result['csv_rows'] == 0:
                    print(f'HEADER_ONLY {record["agency_name"]}', flush=True)
                    continue
                if result['valid_rows'] == 0:
                    raise RuntimeError('No accepted data rows; inspect the preserved rejection ledger')
                report.update(status='validated', finished_at=archive.now(),
                              free_disk_bytes_after=shutil.disk_usage(root).free,
                              catalog_compressed_bytes=sum(r['http']['bytes'] for r in catalog['files']),
                              catalog_archives=len(catalog['files']), selected=measurement,
                              estimate_policy='Small-agency ratio is a provisional scenario, not a full-year capacity guarantee')
                archive.save_json(report_path, report)
                if publish_assets:
                    publication = publish(raw, root / 'bronze' / raw.stem, catalog_path, report_path,
                                          repository, commit, revision, reserve_gib=8)
                    archive.save_json(root / 'publication.json', publication)
                    print('PUBLISHED', json.dumps(publication), flush=True)
                    summary = os.environ.get('GITHUB_STEP_SUMMARY')
                    if summary:
                        with Path(summary).open('a') as output:
                            output.write(f'## Verified raw snapshot\n\n[{record["agency_name"]}]({publication["url"]})\n\n'
                                         f'- CSV records: {result["csv_rows"]:,}; rejects: {result["rejected_rows"]:,}\n'
                                         f'- ZIP: {result["source_bytes"]:,} bytes; Parquet: {result["parquet_bytes"]:,} bytes\n'
                                         f'- Parquet/ZIP ratio: {result["parquet_to_zip_ratio"]}\n'
                                         '- Original ZIP retained; remote asset SHA-256 checks passed.\n')
                    return {**report, 'publication': publication}
                return report
            raise RuntimeError('No nonempty agency archive found')
        except Exception as error:
            report.update(status='failed', error=str(error), failed_at=archive.now())
            archive.save_json(report_path, report)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--kind', choices=['contracts', 'assistance'], default='contracts')
    parser.add_argument('--agency', default='smallest')
    parser.add_argument('--data-root', type=Path, default=Path('data/remote'))
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', 'mintyfizz/money-trail'))
    parser.add_argument('--commit', default=os.environ.get('GITHUB_SHA'))
    parser.add_argument('--max-output-gib', type=float, default=2)
    parser.add_argument('--revision', type=int, default=1)
    args = parser.parse_args()
    run(args.year, args.kind, args.agency, args.data_root, args.repo, args.commit,
        args.max_output_gib, args.revision)
