import argparse
import asyncio
import hashlib
import json
import random
import shutil
import statistics
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import DownloadConfig
from .downloader import Downloader


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def run(url: str, connections: list[int], repetitions: int,
              expected_sha256: str | None, output_root: Path) -> dict:
    cases = connections * repetitions
    random.SystemRandom().shuffle(cases)
    output_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="downloadarr-benchmark-", dir=output_root))
    samples: dict[int, list[dict]] = {value: [] for value in connections}
    try:
        for index, count in enumerate(cases):
            target = workspace / f"run-{index}.bin"
            downloader = Downloader(DownloadConfig(
                connections=count, transfer_mode="sequential" if count == 1 else "parallel",
                resume=False))

            async def provider(refresh: bool) -> str:
                return url

            result = await downloader.download(provider, target)
            digest = await asyncio.to_thread(_hash, target)
            if expected_sha256 and digest.lower() != expected_sha256.lower():
                raise ValueError("benchmark SHA-256 mismatch")
            samples[count].append({
                "bytes": result.byte_count, "elapsed_seconds": result.elapsed,
                "average_bps": result.average_speed, "peak_bps": result.peak_speed,
                "ranges": result.range_requests, "retries": result.retry_count,
                "sha256": digest,
            })
            target.unlink(missing_ok=True)
            target.with_name(target.name + ".downloadarr.receipt.json").unlink(missing_ok=True)
        summaries = []
        for count in connections:
            speeds = sorted(item["average_bps"] for item in samples[count])
            p95_index = max(0, min(len(speeds) - 1, int(len(speeds) * 0.95) - 1))
            summaries.append({
                "connections": count, "sample_count": len(speeds),
                "median_bps": statistics.median(speeds), "p95_bps": speeds[p95_index],
                "minimum_bps": speeds[0], "maximum_bps": speeds[-1],
                "samples": samples[count],
            })
        return {"schema_version": 1, "cdn_host": urlsplit(url).hostname,
                "url_redacted": True, "repetitions": repetitions,
                "results": summaries}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="downloadarr-benchmark")
    parser.add_argument("url")
    parser.add_argument("--connections", default="1,4,8,16")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--sha256")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    counts = sorted({int(value) for value in args.connections.split(",")})
    if not counts or any(not 1 <= value <= 64 for value in counts):
        parser.error("connections must contain values between 1 and 64")
    if not 3 <= args.repetitions <= 20:
        parser.error("repetitions must be between 3 and 20")
    print(json.dumps(asyncio.run(run(args.url, counts, args.repetitions,
                                     args.sha256, args.output_root)), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
