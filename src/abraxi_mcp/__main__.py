"""Explicit-root CLI; stdio is the sole exposed transport."""

import argparse
import sys

from .filesystem import MESSAGES, Refusal, RootFilesystem
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic-root ABRAXI MCP tracer (stdio only)")
    parser.add_argument("--root", required=True, help="Existing synthetic directory; no fallback")
    parser.add_argument("--write-denied-prefix", action="append", default=[],
                        help="Repeatable root-relative write prohibition; '.' denies all writes")
    parser.add_argument("--read-denied-prefix", action="append", default=[],
                        help="Repeatable root-relative read and write prohibition; '.' denies all file access")
    args = parser.parse_args(argv)
    try:
        with RootFilesystem(args.root, tuple(args.write_denied_prefix),
                            read_denied_prefixes=tuple(args.read_denied_prefix)) as filesystem:
            build_server(filesystem).run(transport="stdio")
    except Refusal as exc:
        print(f"Startup refused: {exc.outcome}: {MESSAGES[exc.outcome]}", file=sys.stderr)
        return 2
    except Exception:
        print("Server failed: INTERNAL_ERROR", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
