"""Small local run records; never persist prompts, credentials or tool payloads."""

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class RunRecord:
    def __init__(self) -> None:
        self.run_id = uuid4().hex
        self.path = Path("outputs/runs") / f"{self.run_id}.jsonl"
        self.started = time.monotonic()
        self.failed = False

    def emit(
        self,
        event: str,
        *,
        status: str | None = None,
        code: str | None = None,
        operation: str | None = None,
        call_id: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if self.failed:
            return
        row = {
            "run_id": self.run_id,
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            **{
                key: value
                for key, value in {
                    "status": status,
                    "code": code,
                    "operation": operation,
                    "call_id": call_id,
                    "duration_ms": duration_ms,
                }.items()
                if value is not None
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            self.failed = True
            print("运行记录写入失败；本次记录可能不完整。", file=sys.stderr)

    def __enter__(self):
        self.emit("run_started")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.emit(
            "run_finished",
            status="ok" if exc_type is None else "error",
            code=exc_type.__name__ if exc_type else None,
            duration_ms=round((time.monotonic() - self.started) * 1000),
        )
