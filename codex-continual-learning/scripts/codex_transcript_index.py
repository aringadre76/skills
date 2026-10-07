#!/usr/bin/env python3
"""Discover, extract, and index Codex, Cursor, and OpenCode transcripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1
DEFAULT_INDEX = Path(".cursor/hooks/state/codex-continual-learning-index.json")
SUPPORTED_SOURCES = {"codex", "cursor", "opencode", "opencode-legacy"}
HOST_INJECTED_PREFIXES = (
    "<recommended_plugins>",
    "<environment_context>",
    "# AGENTS.md instructions for ",
)


def canonical_path(value: str) -> str:
    """Normalize Windows, WSL UNC, and /mnt/<drive> paths for comparison."""
    normalized = value.strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    lowered = normalized.lower()

    for prefix in ("/wsl$/", "/wsl.localhost/"):
        if lowered.startswith(prefix):
            parts = normalized.split("/")
            normalized = "/" + "/".join(parts[3:])
            lowered = normalized.lower()
            break

    if lowered.startswith("/mnt/") and len(normalized) > 6:
        drive = normalized[5]
        if normalized[6:7] == "/":
            normalized = f"{drive}:/{normalized[7:]}"

    return normalized.rstrip("/").lower()


def unique_existing(paths: Iterable[Path], predicate: Any) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen or not predicate(path):
            continue
        seen.add(resolved)
        result.append(path)
    return result


def codex_homes(explicit: list[str] | None) -> list[Path]:
    candidates = [Path(value) for value in explicit or []]
    if os.environ.get("CODEX_HOME"):
        candidates.append(Path(os.environ["CODEX_HOME"]))
    if os.environ.get("USERPROFILE"):
        candidates.append(Path(os.environ["USERPROFILE"]) / ".codex")
    candidates.append(Path.home() / ".codex")
    return unique_existing(candidates, lambda path: (path / "sessions").is_dir())


def cursor_roots(explicit: list[str] | None) -> list[Path]:
    candidates = [Path(value) for value in explicit or []]
    candidates.append(Path.home() / ".cursor")
    return unique_existing(candidates, lambda path: (path / "projects").is_dir())


def opencode_databases(explicit: list[str] | None) -> list[Path]:
    candidates = [Path(value) for value in explicit or []]
    candidates.append(Path.home() / ".local/share/opencode/opencode.db")
    return unique_existing(candidates, lambda path: path.is_file())


def opencode_storage_roots(explicit: list[str] | None) -> list[Path]:
    candidates = [Path(value) for value in explicit or []]
    candidates.append(Path.home() / ".local/share/opencode/storage")
    return unique_existing(candidates, lambda path: (path / "session").is_dir())


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def iso_from_milliseconds(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def read_codex_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("type") == "session_meta":
                    payload = record.get("payload", {})
                    return {
                        "session_id": payload.get("session_id") or payload.get("id"),
                        "timestamp": payload.get("timestamp"),
                        "cwd": payload.get("cwd"),
                    }
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def codex_files(home: Path) -> Iterable[Path]:
    sessions = home / "sessions"
    if sessions.is_dir():
        yield from sessions.rglob("*.jsonl")
    archived = home / "archived_sessions"
    if archived.is_dir():
        yield from archived.glob("*.jsonl")


def workspace_slug(workspace: str) -> str:
    value = canonical_path(workspace)
    if re.match(r"^[a-z]:/", value):
        value = value[0] + "/" + value[3:]
    return re.sub(r"[^a-z0-9]+", "-", value.strip("/")).strip("-")


def add_entry(
    entry: dict[str, Any],
    indexed: dict[str, Any],
    discovered: set[str],
    pending: list[dict[str, Any]],
) -> None:
    discovered.add(entry["key"])
    previous = indexed.get(entry["key"], {})
    if previous.get("revision") != entry["revision"]:
        pending.append(entry)


def discover_codex(
    args: argparse.Namespace,
    workspace: str,
    indexed: dict[str, Any],
    discovered: set[str],
    pending: list[dict[str, Any]],
    errors: list[str],
) -> bool:
    homes = codex_homes(args.codex_home)
    if not homes:
        errors.append("codex: no session store found")
        return False
    for home in homes:
        for path in sorted(codex_files(home)):
            meta = read_codex_meta(path)
            if not meta or not meta.get("cwd") or canonical_path(str(meta["cwd"])) != workspace:
                continue
            if args.session_id and meta.get("session_id") != args.session_id:
                continue
            stat = path.stat()
            resolved = str(path.resolve())
            add_entry(
                {
                    "key": f"codex:{resolved}",
                    "source": "codex",
                    "revision": stat.st_mtime_ns,
                    "session_id": meta.get("session_id"),
                    "timestamp": meta.get("timestamp"),
                    "cwd": meta.get("cwd"),
                    "size_bytes": stat.st_size,
                    "locator": {"path": resolved},
                },
                indexed,
                discovered,
                pending,
            )
    return True


def discover_cursor(
    args: argparse.Namespace,
    workspace: str,
    indexed: dict[str, Any],
    discovered: set[str],
    pending: list[dict[str, Any]],
    errors: list[str],
) -> bool:
    roots = cursor_roots(args.cursor_root)
    if not roots:
        errors.append("cursor: no projects store found")
        return False
    slug = workspace_slug(args.workspace)
    for root in roots:
        transcript_root = root / "projects" / slug / "agent-transcripts"
        if not transcript_root.is_dir():
            continue
        for path in sorted(transcript_root.rglob("*.jsonl")):
            session_id = path.stem
            if args.session_id and session_id != args.session_id:
                continue
            stat = path.stat()
            resolved = str(path.resolve())
            add_entry(
                {
                    "key": f"cursor:{resolved}",
                    "source": "cursor",
                    "revision": stat.st_mtime_ns,
                    "session_id": session_id,
                    "timestamp": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "cwd": args.workspace,
                    "size_bytes": stat.st_size,
                    "locator": {"path": resolved},
                },
                indexed,
                discovered,
                pending,
            )
    return True


def open_opencode_database(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def discover_opencode(
    args: argparse.Namespace,
    workspace: str,
    indexed: dict[str, Any],
    discovered: set[str],
    pending: list[dict[str, Any]],
    errors: list[str],
) -> bool:
    databases = opencode_databases(args.opencode_db)
    if not databases:
        errors.append("opencode: no database found")
        return False
    successful = False
    for database in databases:
        try:
            connection = open_opencode_database(database)
            rows = connection.execute(
                """
                SELECT s.id, s.directory, s.time_created,
                       max(s.time_updated,
                           coalesce((SELECT max(m.time_updated) FROM message m WHERE m.session_id = s.id), 0),
                           coalesce((SELECT max(p.time_updated) FROM part p WHERE p.session_id = s.id), 0))
                FROM session s
                """
            ).fetchall()
            connection.close()
            successful = True
        except sqlite3.Error as exc:
            errors.append(f"opencode: cannot read {database}: {exc}")
            continue
        database_resolved = str(database.resolve())
        for session_id, directory, created_ms, revision in rows:
            if not directory or canonical_path(directory) != workspace:
                continue
            if args.session_id and session_id != args.session_id:
                continue
            key = f"opencode:{database_resolved}#{session_id}"
            add_entry(
                {
                    "key": key,
                    "source": "opencode",
                    "revision": int(revision),
                    "session_id": session_id,
                    "timestamp": iso_from_milliseconds(created_ms),
                    "cwd": directory,
                    "size_bytes": database.stat().st_size,
                    "locator": {"database": database_resolved, "session_id": session_id},
                },
                indexed,
                discovered,
                pending,
            )
    return successful


def legacy_session_files(storage: Path) -> Iterable[Path]:
    yield from (storage / "session").rglob("*.json")


def legacy_session_revision(storage: Path, session_path: Path, session_id: str) -> tuple[int, int]:
    files = [session_path]
    message_root = storage / "message" / session_id
    message_files = list(message_root.glob("*.json")) if message_root.is_dir() else []
    files.extend(message_files)
    for message_path in message_files:
        part_root = storage / "part" / message_path.stem
        if part_root.is_dir():
            files.extend(part_root.glob("*.json"))
    stats = [path.stat() for path in files]
    return max(stat.st_mtime_ns for stat in stats), sum(stat.st_size for stat in stats)


def discover_opencode_legacy(
    args: argparse.Namespace,
    workspace: str,
    indexed: dict[str, Any],
    discovered: set[str],
    pending: list[dict[str, Any]],
    errors: list[str],
) -> bool:
    roots = opencode_storage_roots(args.opencode_storage)
    if not roots:
        errors.append("opencode-legacy: no JSON storage found")
        return False
    successful = False
    for storage in roots:
        successful = True
        for session_path in sorted(legacy_session_files(storage)):
            session = load_json(session_path, {})
            session_id = session.get("id")
            directory = session.get("directory")
            if not session_id or not directory or canonical_path(str(directory)) != workspace:
                continue
            if args.session_id and session_id != args.session_id:
                continue
            try:
                revision, size_bytes = legacy_session_revision(storage, session_path, session_id)
            except OSError as exc:
                errors.append(f"opencode-legacy: cannot inspect {session_path}: {exc}")
                continue
            storage_resolved = str(storage.resolve())
            created_ms = (session.get("time") or {}).get("created")
            add_entry(
                {
                    "key": f"opencode-legacy:{storage_resolved}#{session_id}",
                    "source": "opencode-legacy",
                    "revision": revision,
                    "session_id": session_id,
                    "timestamp": iso_from_milliseconds(created_ms),
                    "cwd": directory,
                    "size_bytes": size_bytes,
                    "locator": {"storage": storage_resolved, "session_id": session_id},
                },
                indexed,
                discovered,
                pending,
            )
    return successful


def selected_sources(value: str) -> set[str]:
    sources = {part.strip().lower() for part in value.split(",") if part.strip()}
    unsupported = sources - SUPPORTED_SOURCES
    if unsupported:
        raise SystemExit(f"Unsupported sources: {', '.join(sorted(unsupported))}")
    return sources


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    index_path = Path(args.index)
    index = load_json(index_path, {"schema_version": SCHEMA_VERSION, "sessions": {}})
    indexed = index.get("sessions", {}) if isinstance(index.get("sessions"), dict) else {}
    workspace = canonical_path(args.workspace)
    sources = selected_sources(args.sources)
    pending: list[dict[str, Any]] = []
    discovered: set[str] = set()
    errors: list[str] = []
    successful_sources: set[str] = set()

    discoverers = {
        "codex": discover_codex,
        "cursor": discover_cursor,
        "opencode": discover_opencode,
        "opencode-legacy": discover_opencode_legacy,
    }
    for source in sorted(sources):
        if discoverers[source](args, workspace, indexed, discovered, pending, errors):
            successful_sources.add(source)

    indexed_missing: list[str] = []
    if not args.session_id:
        for key, metadata in indexed.items():
            source = metadata.get("source") or key.split(":", 1)[0]
            if source in successful_sources and key not in discovered:
                indexed_missing.append(key)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": args.workspace,
        "canonical_workspace": workspace,
        "sources": sorted(sources),
        "pending": sorted(pending, key=lambda entry: (entry.get("timestamp") or "", entry["key"])),
        "indexed_missing": sorted(indexed_missing),
        "errors": errors,
    }


def command_discover(args: argparse.Namespace) -> None:
    manifest = build_manifest(args)
    if args.manifest_out:
        write_json(Path(args.manifest_out), manifest)
        counts: dict[str, int] = {source: 0 for source in manifest["sources"]}
        for entry in manifest["pending"]:
            counts[entry["source"]] = counts.get(entry["source"], 0) + 1
        output: dict[str, Any] = {
            "manifest": args.manifest_out,
            "pending": len(manifest["pending"]),
            "pending_by_source": counts,
            "indexed_missing": len(manifest["indexed_missing"]),
            "errors": manifest["errors"],
        }
    else:
        output = manifest
    print(json.dumps(output, indent=2, sort_keys=True))


def accepted_text(role: str, text: Any) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    return not (role == "user" and text.lstrip().startswith(HOST_INJECTED_PREFIXES))


def iter_codex_messages(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path = Path(entry["locator"]["path"])
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload", {})
            if record.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            for part in payload.get("content", []):
                if part.get("type") not in {"input_text", "output_text"}:
                    continue
                text = part.get("text")
                if accepted_text(role, text):
                    yield {"timestamp": record.get("timestamp"), "role": role, "text": text}


def iter_cursor_messages(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path = Path(entry["locator"]["path"])
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = record.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = record.get("message", {}).get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if accepted_text(role, text):
                    yield {"timestamp": record.get("timestamp"), "role": role, "text": text}


def iter_opencode_messages(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    database = Path(entry["locator"]["database"])
    session_id = entry["locator"]["session_id"]
    connection = open_opencode_database(database)
    try:
        rows = connection.execute(
            """
            SELECT m.time_created, m.data, p.data
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.time_created, m.id, p.time_created, p.id
            """,
            (session_id,),
        )
        for created_ms, message_data, part_data in rows:
            try:
                role = json.loads(message_data).get("role")
                part = json.loads(part_data)
            except (TypeError, json.JSONDecodeError):
                continue
            if role not in {"user", "assistant"} or part.get("type") != "text":
                continue
            text = part.get("text")
            if accepted_text(role, text):
                yield {"timestamp": iso_from_milliseconds(created_ms), "role": role, "text": text}
    finally:
        connection.close()


def iter_opencode_legacy_messages(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    storage = Path(entry["locator"]["storage"])
    session_id = entry["locator"]["session_id"]
    message_root = storage / "message" / session_id
    messages: list[tuple[int, str, Path, dict[str, Any]]] = []
    for message_path in message_root.glob("*.json") if message_root.is_dir() else []:
        message = load_json(message_path, {})
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        created_ms = (message.get("time") or {}).get("created") or 0
        messages.append((int(created_ms), str(message.get("id") or message_path.stem), message_path, message))

    for created_ms, message_id, _message_path, message in sorted(messages):
        role = message["role"]
        part_root = storage / "part" / message_id
        parts: list[tuple[str, dict[str, Any]]] = []
        for part_path in part_root.glob("*.json") if part_root.is_dir() else []:
            part = load_json(part_path, {})
            parts.append((str(part.get("id") or part_path.stem), part))
        for _part_id, part in sorted(parts):
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if accepted_text(role, text):
                yield {"timestamp": iso_from_milliseconds(created_ms), "role": role, "text": text}


def command_extract(args: argparse.Namespace) -> None:
    manifest = load_json(Path(args.manifest), {})
    entries = {entry.get("key"): entry for entry in manifest.get("pending", [])}
    entry = entries.get(args.key)
    if not entry:
        raise SystemExit(f"Pending manifest entry not found: {args.key}")
    iterator = {
        "codex": iter_codex_messages,
        "cursor": iter_cursor_messages,
        "opencode": iter_opencode_messages,
        "opencode-legacy": iter_opencode_legacy_messages,
    }[entry["source"]]
    output = Path(args.output) if args.output else None
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
    handle = output.open("w", encoding="utf-8", newline="\n") if output else None
    try:
        target = handle if handle else os.sys.stdout
        for sequence, message in enumerate(iterator(entry), start=1):
            record = {
                "source": entry["source"],
                "session_id": entry.get("session_id"),
                "sequence": sequence,
                **message,
            }
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if handle:
            handle.close()


def command_mark(args: argparse.Namespace) -> None:
    manifest = load_json(Path(args.manifest), {})
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("Unsupported or missing manifest schema version.")
    index_path = Path(args.index)
    index = load_json(index_path, {"schema_version": SCHEMA_VERSION, "sessions": {}})
    sessions = index.get("sessions", {}) if isinstance(index.get("sessions"), dict) else {}

    for missing in manifest.get("indexed_missing", []):
        sessions.pop(missing, None)
    for entry in manifest.get("pending", []):
        sessions[entry["key"]] = {
            "source": entry["source"],
            "revision": entry["revision"],
            "session_id": entry.get("session_id"),
            "timestamp": entry.get("timestamp"),
            "cwd": entry.get("cwd"),
            "locator": entry.get("locator"),
        }

    write_json(
        index_path,
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": manifest.get("workspace"),
            "canonical_workspace": manifest.get("canonical_workspace"),
            "sessions": sessions,
        },
    )
    print(json.dumps({"marked": len(manifest.get("pending", [])), "index": str(index_path)}))


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="List new or changed workspace sessions.")
    discover.add_argument("--workspace", required=True)
    discover.add_argument("--sources", default="codex,cursor,opencode,opencode-legacy")
    discover.add_argument("--codex-home", action="append")
    discover.add_argument("--cursor-root", action="append")
    discover.add_argument("--opencode-db", action="append")
    discover.add_argument("--opencode-storage", action="append")
    discover.add_argument("--index", default=str(DEFAULT_INDEX))
    discover.add_argument("--manifest-out")
    discover.add_argument("--session-id")
    discover.set_defaults(func=command_discover)

    extract = commands.add_parser("extract", help="Extract one pending session as message JSONL.")
    extract.add_argument("--manifest", required=True)
    extract.add_argument("--key", required=True)
    extract.add_argument("--output")
    extract.set_defaults(func=command_extract)

    mark = commands.add_parser("mark", help="Mark manifest sessions processed after validation.")
    mark.add_argument("--manifest", required=True)
    mark.add_argument("--index", default=str(DEFAULT_INDEX))
    mark.set_defaults(func=command_mark)

    return root


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
