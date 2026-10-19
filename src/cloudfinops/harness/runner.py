"""Run cloud CLI commands read-only, with an audit log and raw-output capture.

Every command is checked against a read-only allowlist before it runs, and every
execution is appended to <run_dir>/commands.log (JSON lines) so the client can see
exactly what was called. Raw outputs are saved under <run_dir>/raw/ for traceability.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

READ_VERBS = {
    "list", "show", "query", "get", "describe", "search", "search-all-resources",
    "list-skus", "list-usage", "get-access-token", "print-access-token", "version", "ls",
}
# Interactive/local commands that are not reads but are safe and never touch cloud resources.
LOCAL_COMMANDS = {
    ("az", "login"), ("az", "account", "set"), ("az", "extension", "add"), ("az", "version"),
    ("aws", "sso", "login"), ("gcloud", "auth", "login"), ("gcloud", "config", "get"),
}
# POST endpoints that are reads (query APIs)
READ_POST_PATHS = ("/providers/microsoft.costmanagement/query",)
# Never let a CLI prompt (e.g. gcloud offering to enable an API) block or change anything
QUIET_ENV = {"CLOUDSDK_CORE_DISABLE_PROMPTS": "1", "AZURE_CORE_NO_COLOR": "1"}
_SQL_WRITE = re.compile(r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|call|export)\b", re.I)


class NotReadOnlyError(PermissionError):
    pass


class CliError(RuntimeError):
    def __init__(self, cmd: list[str], code: int, stderr: str):
        super().__init__(f"`{shlex.join(cmd)}` failed ({code}): {stderr.strip()[:500]}")
        self.code, self.stderr = code, stderr


def _command_words(cmd: list[str]) -> list[str]:
    words = []
    for tok in cmd:
        if tok.startswith("-"):
            break
        words.append(tok)
    return words


def check_read_only(cmd: list[str]) -> None:
    """Raise NotReadOnlyError unless the command is a known read (or safe local action)."""
    words = _command_words(cmd)
    if not words:
        raise NotReadOnlyError("empty command")
    for n in (2, 3):
        if tuple(words[:n]) in LOCAL_COMMANDS:
            return
    if words[:2] == ["az", "rest"]:
        method = "get"
        if "--method" in cmd:
            method = cmd[cmd.index("--method") + 1].lower()
        url = cmd[cmd.index("--url") + 1].lower() if "--url" in cmd else ""
        if method == "get" or (method == "post" and any(p in url.split("?")[0] for p in READ_POST_PATHS)):
            return
        raise NotReadOnlyError(f"az rest {method.upper()} to {url} is not an allowed read")
    if words[:2] == ["bq", "query"]:
        sql = cmd[-1]
        if not re.match(r"^\s*(select|with)\b", sql, re.I) or _SQL_WRITE.search(sql) or ";" in sql.strip().rstrip(";"):
            raise NotReadOnlyError("bq query only allows a single SELECT statement")
        return
    if words[-1] in READ_VERBS or words[-1].startswith(("list-", "get-", "describe-")):
        return
    raise NotReadOnlyError(f"`{' '.join(words)}` is not on the read-only allowlist")


class CliRunner:
    def __init__(self, run_dir: str | Path, timeout: int = 300, retries: int = 4):
        self.run_dir = Path(run_dir)
        self.raw_dir = self.run_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "commands.log"
        self.timeout, self.retries = timeout, retries
        self._lock = threading.Lock()

    @staticmethod
    def available(binary: str) -> bool:
        return shutil.which(binary) is not None

    def _log(self, entry: dict) -> None:
        with self._lock, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def run(self, cmd: list[str], save_as: str | None = None, parse_json: bool = True,
            interactive: bool = False):
        """Execute a read-only command. Returns parsed JSON (or text) and optionally saves raw output."""
        check_read_only(cmd)
        exe = shutil.which(cmd[0])  # on Windows the CLIs are az.cmd / gcloud.cmd, not launchable by bare name
        cmd_exec = [exe, *cmd[1:]] if exe else cmd
        attempt, delay = 0, 2.0
        while True:
            started = time.monotonic()
            if interactive:
                proc = subprocess.run(cmd_exec, timeout=self.timeout * 2)
                stdout, stderr = "", ""
            else:
                proc = subprocess.run(cmd_exec, capture_output=True, text=True, timeout=self.timeout,
                                      env={**os.environ, **QUIET_ENV}, stdin=subprocess.DEVNULL)
                stdout, stderr = proc.stdout, proc.stderr
            duration = round(time.monotonic() - started, 2)
            throttled = proc.returncode != 0 and any(
                s in stderr for s in ("429", "TooManyRequests", "Throttl", "RateExceeded"))
            self._log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "cmd": shlex.join(cmd), "exit": proc.returncode, "duration_s": duration,
                       "output": save_as or "", "bytes": len(stdout), "attempt": attempt + 1})
            if throttled and attempt < self.retries:
                attempt += 1
                time.sleep(delay)
                delay *= 2
                continue
            if proc.returncode != 0:
                raise CliError(cmd, proc.returncode, stderr)
            break

        if save_as:
            path = self.raw_dir / save_as
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stdout, encoding="utf-8")
        if not parse_json or interactive:
            return stdout
        return json.loads(stdout) if stdout.strip() else None

    def http_get(self, url: str, token: str, headers: dict | None = None, save_as: str | None = None):
        """Authenticated HTTPS GET (read-only by construction), logged like CLI commands."""
        import urllib.error
        import urllib.request

        attempt, delay = 0, 2.0
        while True:
            started = time.monotonic()
            req = urllib.request.Request(url, method="GET",
                                         headers={"Authorization": f"Bearer {token}", **(headers or {})})
            code, body, err = 200, "", ""
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode()
            except urllib.error.HTTPError as exc:
                code, err = exc.code, exc.read().decode(errors="replace")
            except Exception as exc:  # network failure
                code, err = -1, str(exc)
            self._log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "cmd": f"GET {url.split('?')[0]}", "exit": 0 if code == 200 else code,
                       "duration_s": round(time.monotonic() - started, 2), "output": save_as or "",
                       "bytes": len(body), "attempt": attempt + 1})
            if code == 429 and attempt < self.retries:
                attempt += 1
                time.sleep(delay)
                delay *= 2
                continue
            if code != 200:
                raise CliError(["GET", url.split("?")[0]], code, err)
            data = json.loads(body) if body else {}
            if save_as:
                self.save_json(save_as, data)
            return data

    def save_json(self, rel_path: str, data) -> Path:
        path = self.raw_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return path
