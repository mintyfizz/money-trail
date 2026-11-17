"""Disk reserve checks, in exact bytes, made before work that writes to disk."""
from __future__ import annotations

import argparse
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
