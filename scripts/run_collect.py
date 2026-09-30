"""Scheduled local collection. No Slack sends and no secrets in logs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pakuri", "collect", "--config", "targets.json",
             "--output", "data/latest.json", "--hours", "24"],
            cwd=ROOT, capture_output=True, timeout=840,
        )
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        exit_code = 124
    except OSError:
        exit_code = 1
    record = {"at": datetime.now(timezone.utc).isoformat(), "exit_code": exit_code}
    try:
        payload = json.loads((ROOT / "data/latest.json").read_text(encoding="utf-8"))
        record.update(generated_at=payload.get("generated_at"), status=payload.get("status"),
                      items=len(payload.get("items", [])), errors=len(payload.get("errors", [])))
    except (ValueError, OSError):
        record["status"] = "no_output"
    folder = ROOT / "data"
    folder.mkdir(exist_ok=True)
    path = folder / "collector.log"
    if path.exists() and path.stat().st_size > 2_000_000:
        path.replace(folder / "collector.previous.log")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
