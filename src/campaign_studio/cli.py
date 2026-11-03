"""One local entry point for serving and checking the installed application."""

import argparse
import errno
import json
import socket
import sqlite3
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from campaign_studio.app import create_app
from campaign_studio.config import ConfigurationError, Settings
from campaign_studio.imaging import FontProblem
from campaign_studio.sample import run_replay, run_sample
from campaign_studio.smoke import run_smoke
from campaign_studio.state import StateError
from campaign_studio.storage import CapacityError, EarlierWorkspace, Storage, StorageError


def serve(settings: Settings) -> int:
    # Keep the reserved socket through startup. A probe-and-close would race,
    # and application lifespan can resume previously confirmed file cleanup.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", settings.port))
            listener.listen()
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            print(
                f"Port {settings.port} is already in use. Open the running workspace, or "
                "choose another CAMPAIGN_STUDIO_PORT and start again. No workspace was opened.",
                file=sys.stderr,
            )
            return 1
        Storage(settings.data_dir).require_project_layout()
        config = uvicorn.Config(
            create_app(settings, seed_examples=True),
            host="127.0.0.1",
            port=settings.port,
            workers=1,
            reload=False,
            proxy_headers=False,
            access_log=False,
        )
        server = uvicorn.Server(config)
        print(f"Opening Motif – Campaign Studio at http://127.0.0.1:{settings.port}/", flush=True)
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            return 130
        return 0 if server.started else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Motif – Campaign Studio local services")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Open the local campaign workspace")
    commands.add_parser("smoke", help="Check the installed package without a provider")
    sample = commands.add_parser("sample-run", help="Run a key-free sample or saved-source replay")
    sample.add_argument("--products", type=int, choices=(2, 3), default=2)
    sample.add_argument("--us-only", action="store_true")
    sample.add_argument("--replay", action="store_true", help="Use genuine saved sources locally")
    commands.add_parser("workspace", help="Show the data location, usage, and free disk space")
    for command in commands.choices.values():
        command.add_argument(
            "--data-dir", type=Path, help="Use this data workspace without moving it"
        )
    args = parser.parse_args(argv)
    if args.command == "sample-run" and args.replay and (args.products != 2 or args.us_only):
        parser.error("The saved replay has two products and both markets; omit sample overrides.")
    try:
        settings = Settings.from_environment(data_dir=args.data_dir)
        if args.command == "serve":
            return serve(settings)
        storage = Storage(
            settings.data_dir,
            location_notice=settings.location_notice,
            budget_bytes=settings.storage_budget_bytes,
            reserve_bytes=settings.disk_reserve_bytes,
        )
        if args.command == "workspace":
            print(json.dumps(storage.summary()))
        elif args.command == "smoke":
            print(run_smoke(storage).model_dump_json())
        elif args.command == "sample-run":
            result = (
                run_replay(storage)
                if args.replay
                else run_sample(storage, args.products, args.us_only)
            )
            print(json.dumps(result))
    except ValidationError as error:
        fields = sorted({str(item["loc"][0]) for item in error.errors(include_input=False)})
        print(f"Invalid configuration: {', '.join(fields)}. Check .env.example.", file=sys.stderr)
        return 2
    except CapacityError as error:
        print(str(error), file=sys.stderr)
        print(json.dumps(error.details), file=sys.stderr)
        print(
            "Storage policy: CAMPAIGN_STUDIO_STORAGE_BUDGET_BYTES and "
            "CAMPAIGN_STUDIO_DISK_RESERVE_BYTES. Restart after changing configuration.",
            file=sys.stderr,
        )
        return 1
    except (StateError, ConfigurationError, FontProblem, EarlierWorkspace) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, StorageError, sqlite3.Error) as error:
        # Avoid copying supplied paths or OS diagnostics into shared output.
        print(f"Local storage or server setup failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0
