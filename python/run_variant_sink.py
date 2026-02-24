#!/usr/bin/env python3
"""
Launch a sink instance for a specific variant + instance id.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Talestral transcript sink instance for a specific variant.",
    )
    parser.add_argument("--variant", required=True, help="Variant id (e.g. default, behavioral).")
    parser.add_argument("--instance", required=True, help="Instance id (e.g. meeting-a).")
    parser.add_argument("--host", default="0.0.0.0", help="Sink bind host.")
    parser.add_argument("--port", type=int, required=True, help="Sink bind port.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override analysis output directory. Default: python/output/<instance>.",
    )
    parser.add_argument(
        "--transcript-file",
        default=None,
        help="Override transcript file path.",
    )
    parser.add_argument("--log-level", default="info", help="Uvicorn log level.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path(__file__).parent / "output" / args.instance
    )

    os.environ["VARIANT_ID"] = args.variant
    os.environ["INSTANCE_ID"] = args.instance
    os.environ["SINK_HOST"] = args.host
    os.environ["SINK_PORT"] = str(args.port)
    os.environ["OUTPUT_DIR"] = str(output_dir)
    if args.transcript_file:
        os.environ["TRANSCRIPT_FILE"] = str(Path(args.transcript_file).expanduser())

    from transcript_sink import VARIANT, app  # Import after env config

    print(
        f"Starting sink variant={VARIANT.variant_id} instance={args.instance} "
        f"bind=http://{args.host}:{args.port} output_dir={output_dir}"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
