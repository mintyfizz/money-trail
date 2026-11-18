"""Disk reserve checks, in exact bytes, made before work that writes to disk."""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import shutil

GIB = 1024 ** 3


def free_bytes(path: str | Path) -> int:
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def require(path: str | Path, needed_bytes: int, reserve_gib: float = 8) -> int:
    """Raise unless `needed_bytes` can be written while keeping `reserve_gib` free. Returns free bytes."""
    free = free_bytes(path)
    reserve = int(reserve_gib * GIB)
    if free - needed_bytes < reserve:
        raise RuntimeError(f"Disk reserve: {free} bytes free, {needed_bytes} needed, {reserve} must stay free")
    return free


class GuardedFile(io.BufferedIOBase):
    """A binary file that checks the reserve before every write, including Parquet footers."""

    def __init__(self, path: str | Path, reserve_gib: float = 8):
        self.path = Path(path)
        self.reserve_gib = reserve_gib
        # Unbuffered, so each checked write is the write that reaches the file.
        self.stream = self.path.open("xb", buffering=0)
        self.written = 0

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.stream.tell()

    def write(self, data) -> int:
        size = len(memoryview(data).cast("B"))
        require(self.path.parent, size, self.reserve_gib)
        count = self.stream.write(data)
        self.written += count
        return count

    def flush(self) -> None:
        if not self.stream.closed:
            self.stream.flush()

    def close(self) -> None:
        if not self.closed:
            try:
                self.flush()
            finally:
                self.stream.close()
                super().close()


def write_arrow_parquet(table, path: str | Path, reserve_gib: float = 8) -> int:
    """Write an Arrow table to Parquet with every byte, footer included, checked against the reserve."""
    import pyarrow.parquet as parquet
    with GuardedFile(path, reserve_gib) as sink:
        parquet.write_table(table, sink, compression="zstd")
    return Path(path).stat().st_size


MIN_SPILL = 256 * 1024 ** 2
MAX_SPILL = 2 * GIB


def spill_allowance(path: str | Path, reserve_gib: float = 8) -> int:
    """Bytes DuckDB may spill without crossing the reserve: free minus reserve, capped at 2 GiB."""
    room = free_bytes(path) - int(reserve_gib * GIB)
    if room < MIN_SPILL:
        raise RuntimeError(f"Disk reserve: only {room} bytes above the reserve; at least {MIN_SPILL} needed for query spill")
    return min(room, MAX_SPILL)


def duckdb_connect(temp_dir: str | Path, reserve_gib: float = 8):
    """A DuckDB connection whose spill to disk is limited to what fits above the reserve."""
    import duckdb
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    allowance = spill_allowance(temp_dir, reserve_gib)
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{temp_dir}'")
    con.execute(f"SET max_temp_directory_size = '{allowance}B'")
    return con


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--need", type=int, default=0, help="bytes about to be written")
    parser.add_argument("--reserve-gib", type=float, default=8)
    args = parser.parse_args()
    free = require(args.path, args.need, args.reserve_gib)
    print(f"ok: {free} bytes free, {args.need} needed, reserve {args.reserve_gib} GiB")


if __name__ == "__main__":
    main()
