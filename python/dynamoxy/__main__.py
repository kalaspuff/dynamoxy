"""Command line helpers: ``dynamoxy guide | version | events | operations | reserved WORD``."""

from __future__ import annotations

import argparse
import sys
from importlib import resources

from dynamoxy import __version__
from dynamoxy._core import event_kinds, is_reserved_word, operation_names
from dynamoxy.events import PYTHON_EVENT_KINDS


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``dynamoxy`` command."""
    parser = argparse.ArgumentParser(prog="dynamoxy", description="dynamoxy command line helpers.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("guide", help="print the usage guide (written for developers and AI agents)")
    commands.add_parser("version", help="print the installed version")
    commands.add_parser("events", help="list event kinds with their default levels")
    commands.add_parser("operations", help="list the DynamoDB operations dynamoxy reports metrics for")
    reserved = commands.add_parser("reserved", help="tell whether words are DynamoDB reserved words")
    reserved.add_argument("words", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "guide":
        sys.stdout.write(resources.files("dynamoxy").joinpath("AGENT_GUIDE.md").read_text(encoding="utf-8"))
    elif args.command == "version":
        print(__version__)
    elif args.command == "events":
        rows = [(name, level, description) for name, level, description in event_kinds()]
        rows += [(kind.value, level, description) for kind, (level, description) in PYTHON_EVENT_KINDS.items()]
        for name, level, description in rows:
            print(f"{name:<22} {level:<8} {description}")
    elif args.command == "operations":
        for name in operation_names():
            print(name)
    elif args.command == "reserved":
        for word in args.words:
            reserved_word = is_reserved_word(word)
            print(f"{word}: {'reserved (dynamoxy uses #placeholders for it)' if reserved_word else 'not reserved'}")
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
