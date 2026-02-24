#!/usr/bin/env python3
"""
Launch a Streamlit UI instance for a specific variant + sink target.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a LegionMeet Streamlit UI instance for a specific product spec.",
    )
    parser.add_argument(
        "--variant",
        default="default",
        help="Variant plugin id for compatibility (default: default).",
    )
    parser.add_argument("--instance", required=True, help="Instance id (e.g. meeting-a).")
    parser.add_argument("--port", type=int, required=True, help="Streamlit port.")
    parser.add_argument(
        "--sink-url",
        required=True,
        help="Transcript sink base URL (e.g. http://127.0.0.1:8765).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override analysis output directory. Default: python/output/<instance>.",
    )
    parser.add_argument(
        "--product-spec",
        required=True,
        help="Required path to product spec JSON (e.g. ./legionmeet_platform/specs/prd-pro.json).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Streamlit bind host.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path(__file__).parent / "output" / args.instance
    )

    env = os.environ.copy()
    env["VARIANT_ID"] = args.variant
    env["INSTANCE_ID"] = args.instance
    env["SINK_URL"] = args.sink_url
    env["OUTPUT_DIR"] = str(output_dir)
    env["PRODUCT_SPEC_PATH"] = str(Path(args.product_spec).expanduser())

    cmd = [
        "streamlit",
        "run",
        "streamlit_ui.py",
        "--server.port",
        str(args.port),
        "--server.address",
        args.host,
    ]
    print(
        f"Starting LegionMeet UI variant={args.variant} instance={args.instance} "
        f"bind=http://{args.host}:{args.port} sink={args.sink_url} output_dir={output_dir} "
        f"product_spec={env['PRODUCT_SPEC_PATH']}"
    )
    subprocess.run(cmd, check=True, cwd=Path(__file__).parent, env=env)


if __name__ == "__main__":
    main()
