import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .config import DownloadConfig
from .downloader import Downloader
from .errors import DownloadError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="downloadarr", description="Resumable HTTP downloader")
    result.add_argument("url", nargs="?")
    result.add_argument("--url-env", metavar="NAME",
                        help="read the URL from an environment variable")
    result.add_argument("-o", "--output", required=True, type=Path)
    result.add_argument("-n", "--connections", type=int, default=8)
    result.add_argument("--mode", choices=("auto", "sequential", "parallel"), default="auto")
    result.add_argument("--retries", type=int, default=6)
    result.add_argument("--no-resume", action="store_true")
    result.add_argument("--json", action="store_true",
                        help="emit credential-safe transfer diagnostics as JSON")
    return result


async def _run(args) -> int:
    if bool(args.url) == bool(args.url_env):
        raise ValueError("provide exactly one URL or --url-env NAME")
    url = args.url
    if args.url_env:
        url = os.environ.pop(args.url_env, None)
        if not url:
            raise ValueError(f"URL environment variable is empty or missing: {args.url_env}")
    config = DownloadConfig(connections=args.connections, transfer_mode=args.mode,
                            retries=args.retries,
                            resume=not args.no_resume)
    downloader = Downloader(config)
    async def provider(refresh: bool) -> str:
        return url
    last = -1
    def progress(value) -> None:
        nonlocal last
        if args.json:
            return
        percent = int(value.downloaded_bytes * 100 / value.total_bytes)
        if percent != last:
            last = percent
            print(f"\r{percent:3d}%  {value.downloaded_bytes}/{value.total_bytes} bytes", end="", flush=True)
    result = await downloader.download(provider, args.output, progress)
    result.path.with_name(result.path.name + ".downloadarr.receipt.json").unlink(missing_ok=True)
    if args.json:
        print(json.dumps({
            "path": str(result.path),
            "byte_count": result.byte_count,
            "session_byte_count": result.session_byte_count,
            "elapsed": result.elapsed,
            "average_speed": result.average_speed,
            "resumed": result.resumed,
            "used_ranges": result.used_ranges,
            "range_requests": result.range_requests,
            "retry_count": result.retry_count,
            "cdn_host": result.cdn_host,
        }, separators=(",", ":")))
    else:
        print(f"\nSaved {result.byte_count} bytes to {result.path} in {result.elapsed:.2f}s "
              f"({result.average_speed / 1024 / 1024:.2f} MiB/s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(parser().parse_args(argv)))
    except (DownloadError, OSError, ValueError) as error:
        print(f"downloadarr: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
