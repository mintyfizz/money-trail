"""Inspect a bounded first-member CSV prefix from an interrupted ZIP transfer.

Diagnostic only: a prefix is NOT a complete or CRC-verified archive and MUST NOT
feed the lakehouse. This is not a random sample or an estimate of total row count.
"""
import argparse
import io
from pathlib import Path
import struct
import zlib

from ingest.archive import now, profile_csv, save_json, sha256


def sample(path, limit=10000):
    path = Path(path)
    output = bytearray()
    memory_cap = 64 * 1024**2
    with path.open('rb') as stream:
        header = stream.read(30)
        magic, version, flags, method, _, _, _, compressed_size, raw_size, name_len, extra_len = struct.unpack('<4s5H3I2H', header)
        if magic != b'PK\x03\x04' or method != 8 or flags & 1:
            raise ValueError('Only an unencrypted ZIP with a deflated first member is supported')
        name = stream.read(name_len).decode('utf-8')
        extra = stream.read(extra_len)
        offset = 0
        while offset + 4 <= len(extra):
            tag, length = struct.unpack_from('<HH', extra, offset)
            offset += 4
            if tag == 1:
                position = offset
                if raw_size == 0xFFFFFFFF:
                    raw_size = struct.unpack_from('<Q', extra, position)[0]
                    position += 8
                if compressed_size == 0xFFFFFFFF:
                    compressed_size = struct.unpack_from('<Q', extra, position)[0]
            offset += length
        if not name.lower().endswith('.csv'):
            raise ValueError('First member is not CSV')
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        while len(output) < memory_cap and not decoder.eof:
            compressed = stream.read(64 * 1024)
            if not compressed:
                break
            output.extend(decoder.decompress(compressed, memory_cap - len(output)))
    result = profile_csv(io.BytesIO(output), name, max_rows=limit)
    if result['rows'] != limit:
        raise ValueError('Prefix too short for requested sample; no complete-sample claim made')
    return {'file_name': path.name, 'profiled_at': now(), 'status': 'INCOMPLETE_ARCHIVE_PREFIX_SAMPLE',
            'received_bytes': path.stat().st_size, 'received_prefix_sha256': sha256(path),
            'zip_crc_status': 'UNVERIFIED: central directory and full member are unavailable',
            'sampling': f'First {limit} parsed records in the first CSV member; not representative',
            'first_member_declared_uncompressed_bytes': raw_size,
            'first_member_declared_compressed_bytes': compressed_size,
            'total_archive_rows': None, 'sample': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('partial', type=Path)
    parser.add_argument('--limit', type=int, default=10000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error('--limit must be positive')
    save_json(args.output, sample(args.partial, args.limit))
