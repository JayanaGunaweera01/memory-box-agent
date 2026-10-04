"""`uv run memorybox`: start MemoryBox on this computer.

Checks that Ollama is running and has the models the config names, prints the address to open on a
phone on the same Wi-Fi, and starts the server. `--demo` runs with canned answers and no model, to
try the app or take screenshots on any machine.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys


def lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it only picks the interface
            return s.getsockname()[0]
    except OSError:
        return None


def check_models() -> list[str]:
    from memorybox.adapters.ollama import host, installed_models
    from memorybox.config import load_config

    cfg = load_config()
    have = installed_models()
    if have is None:
        return [f"Ollama is not answering at {host()}. Install it from https://ollama.com, then run `ollama serve`."]
    problems = []
    for st in cfg.stages.values():
        for alias, role in ((st.primary, "needed"), (st.fallback, "optional fallback")):
            if not alias:
                continue
            m = cfg.model(alias).id_for_adapter
            if m not in have and f"{m}:latest" not in have:
                problems.append(f"{m} is not pulled ({role}). Run: ollama pull {m}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="memorybox", description="A private memory collection for a box of keepsakes.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 lets a phone on the same Wi-Fi connect; 127.0.0.1 keeps it to this computer")
    ap.add_argument("--demo", action="store_true", help="canned answers, no model: for trying the app out")
    ap.add_argument("--new-box", action="store_true",
                    help="put the current box away in the archive folder and start with an empty one")
    args = ap.parse_args(argv)

    import uvicorn

    from memorybox.store import Store, home_dir
    from memorybox.web.app import create_app

    client = None
    if args.demo:
        from memorybox.fakes import FakeClient

        client = FakeClient(delay_s=1.5)
        os.environ.setdefault("MEMORYBOX_HOME", str(home_dir().with_name(".memorybox-demo")))
        import shutil

        shutil.rmtree(home_dir(), ignore_errors=True)  # the demo box is throwaway: start clean every time
    else:
        for p in check_models():
            print(f"  ! {p}", file=sys.stderr)

    store = Store()
    if args.new_box:
        moved = store.start_new_box()
        print(f"  The previous box was moved to {moved}" if moved else "  The box was already empty.")
    app = create_app(store, client, model_name="canned demo answers, no model loaded" if args.demo else None)
    lan = lan_address()
    print(f"\n  MemoryBox is running on this computer{' (demo mode)' if args.demo else ''}.")
    print(f"    here:           http://localhost:{args.port}/")
    if lan and args.host == "0.0.0.0":
        print(f"    on a phone:     http://{lan}:{args.port}/   (same Wi-Fi)")
    print(f"    the archive:    {store.home}   (copy it to keep it, delete it to forget everything)\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
