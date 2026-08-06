import argparse
import asyncio
import sys
from pathlib import Path

from .config import DownloadConfig
from .downloader import Downloader
from .errors import DownloadError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="downloadarr", description="Resumable HTTP downloader")
    result.add_argument("url")
    result.add_argument("-o", "--output", required=True, type=Path)
    result.add_argument("-n", "--connections", type=int, default=8)
    result.add_argument("--retries", type=int, default=6)
    result.add_argument("--no-resume", action="store_true")
    return result


async def _run(args) -> int:
    config = DownloadConfig(connections=args.connections, retries=args.retries,
                            resume=not args.no_resume)
    downloader = Downloader(config)
    async def provider(refresh: bool) -> str:
        return args.url
    last = -1
    def progress(value) -> None:
        nonlocal last
        percent = int(value.downloaded_bytes * 100 / value.total_bytes)
        if percent != last:
            last = percent
            print(f"\r{percent:3d}%  {value.downloaded_bytes}/{value.total_bytes} bytes", end="", flush=True)
    result = await downloader.download(provider, args.output, progress)
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
