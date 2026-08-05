#!/usr/bin/env python3
"""Ledger Arena transport client.

This module owns the network edge: SSE consumption, posting batches,
checkpoint replies, stream resets, crash recovery, and practice diagnostics.
The accounting stays in book.py.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from book import Book


MAX_POSTINGS_PER_REQUEST = 500
DEFAULT_URL = "https://hiring-arena.twocc.in"
LOGGER = logging.getLogger("ledger_arena.client")


class ReconnectRequested(Exception):
    """Raised internally when the stream asks us to reconnect at an offset."""


@dataclass
class SSEMessage:
    event: str
    data: str
    id: str | None = None


class DurableState:
    """Small JSON state store used to recover after a crash.

    The event log is enough to rebuild Book. Pending postings are persisted
    before they are sent, so a restart can finish sending them even if the
    broker does not redeliver those offsets.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.event_log_path = path.with_suffix(path.suffix + ".events.jsonl")
        self.cursor = 0
        self.pending: list[dict[str, Any]] = []
        self.submitted: set[str] = set()
        self.run_id: str | None = None

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.cursor = int(data.get("cursor", 0) or 0)
        self.pending = list(data.get("pending", []))
        self.submitted = set(data.get("submitted", []))
        self.run_id = data.get("run_id")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cursor": self.cursor,
            "pending": self.pending,
            "submitted": sorted(self.submitted),
            "run_id": self.run_id,
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def append_event(self, ev: dict[str, Any]) -> None:
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, separators=(",", ":"), sort_keys=True))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    def read_events(self) -> Iterable[dict[str, Any]]:
        if not self.event_log_path.exists():
            return
        with self.event_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def reset_for_new_run(self) -> None:
        self.cursor = 0
        self.pending = []
        self.submitted = set()
        self.run_id = None
        self.save()
        if self.event_log_path.exists():
            self.event_log_path.unlink()


class FeedbackLogger:
    def __init__(self, mode: str, path: Path | None) -> None:
        self.mode = mode
        self.path = path

    def record(self, kind: str, request: dict[str, Any], response: Any) -> None:
        if self.mode != "practice" or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "kind": kind,
            "request": request,
            "response": response,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":"), sort_keys=True))
            f.write("\n")
        for line in summarize_feedback(kind, request, response):
            LOGGER.info(line)


class ArenaClient:
    def __init__(
        self,
        url: str,
        key: str,
        mode: str,
        *,
        batch: int = 100,
        flush_ms: int = 400,
        state_file: Path,
        feedback_file: Path | None,
        start_new: bool = False,
    ) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.mode = mode
        self.batch = min(batch, MAX_POSTINGS_PER_REQUEST)
        self.flush_ms = flush_ms
        self.start_new = start_new
        self.state = DurableState(state_file)
        self.feedback = FeedbackLogger(mode, feedback_file)
        self.book = Book()
        self.done = False
        self.stats = {
            "events": 0,
            "posted": 0,
            "duplicates_seen": 0,
            "checkpoints": 0,
            "reconnects": 0,
            "resets": 0,
            "errors": 0,
        }

    # -- lifecycle ---------------------------------------------------------
    def prepare(self) -> None:
        if self.start_new:
            self.state.reset_for_new_run()
        else:
            self.state.load()
        replayed = 0
        for ev in self.state.read_events() or ():
            self.book.apply(copy.deepcopy(ev))
            replayed += 1
        if replayed or self.state.pending or self.state.submitted:
            LOGGER.info(
                "recovered cursor=%s replayed=%s pending=%s submitted=%s",
                self.state.cursor,
                replayed,
                len(self.state.pending),
                len(self.state.submitted),
            )

    def run(self, max_seconds: float) -> dict[str, Any]:
        self.prepare()
        deadline = time.monotonic() + max_seconds
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(30.0, connect=20.0, read=None)
        with httpx.Client(headers=headers, timeout=timeout) as http:
            self.flush(http)
            while time.monotonic() < deadline and not self.done:
                try:
                    self.consume(http, deadline)
                except ReconnectRequested:
                    continue
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        LOGGER.error(
                            "run is already finished for %s; pass --new to spend a new attempt",
                            self.mode,
                        )
                        self.done = True
                        break
                    self.stats["reconnects"] += 1
                    self.stats["errors"] += 1
                    LOGGER.warning(
                        "stream HTTP %s, reconnecting",
                        exc.response.status_code,
                    )
                    sleep_with_deadline(1.0, deadline)
                except (httpx.HTTPError, json.JSONDecodeError, OSError) as exc:
                    self.stats["reconnects"] += 1
                    self.stats["errors"] += 1
                    LOGGER.warning("stream error %s, reconnecting", type(exc).__name__)
                    sleep_with_deadline(1.0, deadline)
            self.flush(http)
            me = self.fetch_me(http)
        return {"stats": self.stats, "me": me, "state_file": str(self.state.path)}

    # -- stream ------------------------------------------------------------
    def consume(self, http: httpx.Client, deadline: float) -> None:
        params: dict[str, Any] = {"mode": self.mode, "from": self.state.cursor}
        if self.start_new and self.mode in {"submission", "final"}:
            params["new"] = "true"
            self.start_new = False

        LOGGER.info("connecting from offset %s", self.state.cursor)
        last_flush = time.monotonic()
        with http.stream("GET", f"{self.url}/v1/stream", params=params) as r:
            r.raise_for_status()
            for msg in parse_sse(r.iter_lines()):
                if time.monotonic() > deadline:
                    return
                self.handle_message(http, msg)
                if (
                    len(self.state.pending) >= self.batch
                    or (time.monotonic() - last_flush) * 1000 >= self.flush_ms
                ):
                    self.flush(http)
                    last_flush = time.monotonic()

    def handle_message(self, http: httpx.Client, msg: SSEMessage) -> None:
        ev = json.loads(msg.data) if msg.data else {}
        etype = msg.event or ev.get("type", "message")

        if etype == "stream_open":
            self.handle_stream_open(ev)
            return
        if etype == "stream_reset":
            self.handle_stream_reset(http, ev)
            raise ReconnectRequested
        if etype == "stream_end":
            LOGGER.info("stream ended")
            self.flush(http)
            self.done = True
            return

        if ev.get("type") == "checkpoint_request" or etype == "checkpoint_request":
            self.handle_checkpoint(http, ev.get("payload", {}))
            return

        self.handle_ledger_event(ev)

    def handle_stream_open(self, ev: dict[str, Any]) -> None:
        run_id = ev.get("run_id")
        if run_id and self.state.run_id and run_id != self.state.run_id:
            LOGGER.warning(
                "run changed from %s to %s; resetting local recovery state",
                self.state.run_id,
                run_id,
            )
            self.state.reset_for_new_run()
            self.book = Book()
        self.state.run_id = run_id or self.state.run_id
        self.state.save()
        LOGGER.info(
            "connected run=%s resumed_from=%s next_event_in=%ss",
            ev.get("run_id"),
            ev.get("resumed_from", ev.get("offset")),
            ev.get("next_event_in_seconds"),
        )

    def handle_stream_reset(self, http: httpx.Client, ev: dict[str, Any]) -> None:
        resume_from = ev.get("resume_from", ev.get("offset", self.state.cursor))
        self.flush(http)
        self.state.cursor = int(resume_from)
        self.state.save()
        self.stats["resets"] += 1
        LOGGER.warning("stream reset requested; reconnecting from %s", resume_from)

    def handle_ledger_event(self, ev: dict[str, Any]) -> None:
        event_id = ev["event_id"]
        offset = int(ev.get("offset", self.state.cursor))
        self.state.cursor = max(self.state.cursor, offset + 1)

        if event_id in self.state.submitted or any(
            p.get("event_id") == event_id for p in self.state.pending
        ):
            self.stats["duplicates_seen"] += 1
            self.state.save()
            return

        self.state.append_event(ev)
        try:
            legs = self.book.apply(copy.deepcopy(ev)) or []
        except Exception:
            LOGGER.exception("book.apply failed for %s; submitting empty legs", event_id)
            legs = []
        self.state.pending.append({"event_id": event_id, "legs": legs, "_offset": offset})
        self.state.save()
        self.stats["events"] += 1

    # -- submissions -------------------------------------------------------
    def flush(self, http: httpx.Client) -> None:
        while self.state.pending:
            chunk = self.state.pending[:MAX_POSTINGS_PER_REQUEST]
            wire_postings = [
                {"event_id": p["event_id"], "legs": p.get("legs") or []} for p in chunk
            ]
            body = {"postings": wire_postings}
            try:
                r = http.post(
                    f"{self.url}/v1/postings",
                    params={"mode": self.mode},
                    json=body,
                    timeout=30,
                )
                if r.status_code == 429:
                    retry_after = float(r.headers.get("Retry-After", 1))
                    LOGGER.warning("rate limited posting batch; sleeping %.1fs", retry_after)
                    time.sleep(retry_after)
                    return
                r.raise_for_status()
                response_json = response_as_json(r)
                self.feedback.record("postings", body, response_json)
                for p in chunk:
                    self.state.submitted.add(p["event_id"])
                self.state.pending = self.state.pending[len(chunk) :]
                self.state.save()
                self.stats["posted"] += len(chunk)
            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                LOGGER.warning("posting batch failed: %s", exc)
                self.state.save()
                time.sleep(1)
                return

    def handle_checkpoint(self, http: httpx.Client, payload: dict[str, Any]) -> None:
        checkpoint_id = payload["checkpoint_id"]
        as_of_event_id = payload.get("as_of_event_id")
        snap = self.snapshot(as_of_event_id)
        self.flush(http)
        body = {"checkpoint_id": checkpoint_id, **snap}
        try:
            r = http.post(
                f"{self.url}/v1/checkpoint",
                params={"mode": self.mode},
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            response_json = response_as_json(r)
            self.feedback.record("checkpoint", body, response_json)
            self.stats["checkpoints"] += 1
            LOGGER.info(
                "checkpoint %s sent%s",
                checkpoint_id,
                f" as_of={as_of_event_id}" if as_of_event_id else "",
            )
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            LOGGER.warning("checkpoint %s failed: %s", checkpoint_id, exc)

    def snapshot(self, as_of_event_id: str | None = None) -> dict[str, Any]:
        if as_of_event_id is None:
            return self.book.snapshot()

        snapshot_as_of = getattr(self.book, "snapshot_as_of", None)
        if callable(snapshot_as_of):
            return snapshot_as_of(as_of_event_id)

        try:
            return self.book.snapshot(as_of_event_id=as_of_event_id)
        except TypeError:
            pass
        try:
            return self.book.snapshot(as_of_event_id)
        except TypeError:
            LOGGER.warning(
                "book.py does not expose as-of snapshots; answering %s with current state",
                as_of_event_id,
            )
            return self.book.snapshot()

    def fetch_me(self, http: httpx.Client) -> dict[str, Any]:
        try:
            r = http.get(f"{self.url}/v1/me", params={"mode": self.mode}, timeout=20)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            self.stats["errors"] += 1
            return {}


def parse_sse(lines: Iterable[str]) -> Iterable[SSEMessage]:
    event = "message"
    data: list[str] = []
    msg_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data:
                yield SSEMessage(event=event, data="\n".join(data), id=msg_id)
            event = "message"
            data = []
            msg_id = None
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            msg_id = value

    if data:
        yield SSEMessage(event=event, data="\n".join(data), id=msg_id)


def response_as_json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


def summarize_feedback(kind: str, request: dict[str, Any], response: Any) -> list[str]:
    if not isinstance(response, dict):
        return []

    rows: list[dict[str, Any]] = []
    for key in ("results", "postings", "events", "feedback"):
        value = response.get(key)
        if isinstance(value, list):
            rows.extend(v for v in value if isinstance(v, dict))
    if not rows and kind == "postings":
        rows = [
            response | {"event_id": p.get("event_id")}
            for p in request.get("postings", [])
            if isinstance(p, dict)
        ]

    out: list[str] = []
    for row in rows:
        event_id = row.get("event_id", "?")
        correct = first_present(row, "correct", "ok", "matched", "balanced")
        duplicate = row.get("duplicate")
        has_diff = any(k in row for k in ("diff", "diffs", "mismatches"))
        has_expected_actual = "expected" in row and "actual" in row
        if duplicate:
            out.append(f"feedback {event_id}: duplicate ignored")
        elif correct is False or has_diff or (correct is None and has_expected_actual):
            details = compact_feedback_details(row)
            out.append(f"feedback {event_id}: {details}")

    if kind == "checkpoint":
        correct = first_present(response, "correct", "ok", "matched")
        if correct is False or any(k in response for k in ("diff", "diffs", "mismatches")):
            out.append(f"checkpoint feedback: {compact_feedback_details(response)}")
    return out


def compact_feedback_details(row: dict[str, Any]) -> str:
    keep = {
        k: v
        for k, v in row.items()
        if k
        in {
            "correct",
            "ok",
            "balanced",
            "duplicate",
            "diff",
            "diffs",
            "mismatches",
            "accounts",
            "expected",
            "actual",
            "message",
            "error",
        }
    }
    if not keep:
        keep = row
    text = json.dumps(keep, sort_keys=True)
    return text if len(text) <= 700 else text[:697] + "..."


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def sleep_with_deadline(seconds: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(seconds, remaining))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument(
        "--key",
        default=os.environ.get("LEDGER_ARENA_KEY"),
        help="API key from the portal; can also use LEDGER_ARENA_KEY",
    )
    ap.add_argument("--mode", default="practice", choices=["practice", "submission", "final"])
    ap.add_argument("--seconds", type=float, default=1500)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--flush-ms", type=int, default=400)
    ap.add_argument("--new", action="store_true", help="discard local state and start a new run")
    ap.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="durable client state path; defaults to .ledger_state_<mode>.json",
    )
    ap.add_argument(
        "--feedback-file",
        type=Path,
        default=None,
        help="practice feedback JSONL path; defaults to .practice_feedback.jsonl",
    )
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.key:
        ap.error("--key is required unless LEDGER_ARENA_KEY is set")

    if args.mode != "practice":
        print(f"\n  You are about to start or resume a {args.mode.upper()} run.")
        print("  Attempts are limited; use --new only when you mean to spend one.")
        if input("  Type the mode name to continue: ").strip() != args.mode:
            print("  Cancelled.")
            return 1

    state_file = args.state_file or Path(f".ledger_state_{args.mode}.json")
    feedback_file = args.feedback_file
    if feedback_file is None and args.mode == "practice":
        feedback_file = Path(".practice_feedback.jsonl")

    client = ArenaClient(
        args.url,
        args.key,
        args.mode,
        batch=args.batch,
        flush_ms=args.flush_ms,
        state_file=state_file,
        feedback_file=feedback_file,
        start_new=args.new,
    )
    LOGGER.info("connecting to %s as %s", args.url, args.mode)
    out = client.run(args.seconds)
    print("\nstats:", json.dumps(out["stats"], sort_keys=True))
    print("state:", out["state_file"])
    if feedback_file:
        print("feedback:", feedback_file)

    todo = getattr(client.book, "todo", {})
    if todo:
        print(f"\nnot implemented yet ({sum(todo.values())} events skipped):")
        for event_type, count in sorted(todo.items(), key=lambda kv: -kv[1]):
            print(f"  {event_type:<30} {count:>5} events")

    me = out.get("me") or {}
    latest = me.get("latest_run") if isinstance(me.get("latest_run"), dict) else None
    score_obj = latest or me
    if score_obj.get("score") is not None:
        label = "latest score" if latest else "score"
        print(f"{label}: {score_obj['score']}")
        for key, value in (score_obj.get("breakdown") or {}).items():
            if isinstance(value, dict):
                print(f"  {key:<26} {value.get('points', '?'):>6} / {value.get('max', '?')}")
            else:
                print(f"  {key:<26} {value}")
    else:
        print("score: withheld on this tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
