"""Dispatch: parse argv and run the selected sub-command."""

from __future__ import annotations

import sys
from ..ledger import LedgerError
from .parser import build_parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (LedgerError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
