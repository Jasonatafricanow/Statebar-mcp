"""CLI: ``statebar-mcp mcp`` / ``statebar-mcp serve`` (派单总纲 §5)."""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="statebar-mcp",
        description="Hermes User State Layer — short-term user state for AI agents.",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: ~/.statebar-mcp/user_state.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_mcp = sub.add_parser("mcp", help="MCP stdio server (default, no daemon)")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_serve = sub.add_parser("serve", help="REST service (multi-client shared)")
    p_serve.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=None, help="bind port (default 8765)")
    p_serve.add_argument("--log-file", default=None, help="append logs to a file (daemon mode)")
    p_serve.set_defaults(func=_cmd_serve)

    p_health = sub.add_parser("health", help="print service health (CLI check)")
    p_health.set_defaults(func=_cmd_health)

    args = parser.parse_args(argv)
    return args.func(args)


def _build_service(db_override=None):
    import os

    if db_override:
        os.environ["DSH_USER_STATE_DB"] = db_override
    from .config import build_persistent_extractor, load_config
    from .core.service import UserStateService
    from .core.store import SQLiteStore

    cfg = load_config()
    store = SQLiteStore(str(cfg.db_path))
    extractor = build_persistent_extractor(cfg)
    service = UserStateService(store, persistent_extractor=extractor)
    return service, cfg


def _cmd_mcp(args) -> int:
    from .transports.mcp_stdio import run_stdio

    service, _cfg = _build_service(args.db)
    try:
        run_stdio(service)
    finally:
        service.close()
    return 0


def _cmd_serve(args) -> int:
    import logging

    from .config import is_loopback_host
    from .transports.rest import run_server

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    service, cfg = _build_service(args.db)
    host = args.host or cfg.serve.host
    port = args.port or cfg.serve.port
    token = cfg.serve.auth_token

    # Fail-closed: never expose user state on a network interface without
    # authentication. Loopback + no token is the only open configuration.
    if not is_loopback_host(host) and not token:
        logging.error(
            "refusing to bind %s without an auth token: user state must not be "
            "exposed on the network unauthenticated. Set DSH_USER_STATE_SERVE_TOKEN "
            "(or serve.auth_token in config.json), or bind 127.0.0.1.",
            host,
        )
        service.close()
        return 2

    try:
        run_server(service, host, port, auth_token=token,
                   max_body_bytes=cfg.serve.max_body_bytes)
    finally:
        service.close()
    return 0


def _cmd_health(args) -> int:
    import json

    service, cfg = _build_service(args.db)
    try:
        print(json.dumps(service.healthcheck(), ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
