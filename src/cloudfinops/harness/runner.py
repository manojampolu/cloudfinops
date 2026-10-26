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
    ("aws", "sso", "login"), ("gcloud", "auth", "login"), ("gcloud", "config", "get"), ("aws", "configure", "get"),
}
# POST endpoints that are reads (query APIs)
READ_POST_PATHS = ("/providers/microsoft.costmanagement/query",)
# Never let a CLI prompt (e.g. gcloud offering to enable an API) block or change anything
QUIET_ENV = {"CLOUDSDK_CORE_DISABLE_PROMPTS": "1", "AZURE_CORE_NO_COLOR": "1"}
_SQL_WRITE = re.compile(r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|call|export)\b", re.I)


_CMD_SPECIAL = set(' \t&|<>^()"%!,;=')


def windows_batch_cmdline(cmd: list[str]) -> str:
    """Command line for a .cmd/.bat target (az.cmd, gcloud.cmd).

    cmd.exe re-parses the line, so an unquoted & | < > ^ in an argument (e.g. a URL with &$skiptoken=)
    splits or redirects the command. Quote every argument that contains a special character.
    """
    out = []
    for arg in cmd:
        if arg and not (set(arg) & _CMD_SPECIAL):
            out.append(arg)
        else:
            out.append('"' + arg.replace('"', '""') + '"')
    return " ".join(out)


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
            interactive: bool = False, absent_ok: tuple[str, ...] = ()):
        """Execute a read-only command. Returns parsed JSON (or text) and optionally saves raw output.

        absent_ok: error codes that mean "not configured" (e.g. NoSuchTagSet). They return None and are logged
        as a normal answer, not a failure.
        """
        check_read_only(cmd)
        exe = shutil.which(cmd[0])  # on Windows the CLIs are az.cmd / gcloud.cmd, not launchable by bare name
        cmd_exec = [exe, *cmd[1:]] if exe else cmd
        if exe and exe.lower().endswith((".cmd", ".bat")):
            cmd_exec = windows_batch_cmdline(cmd_exec)
        attempt, delay = 0, 2.0
        while True:
            started = time.monotonic()
            if interactive:
                try:
                    proc = subprocess.run(cmd_exec, timeout=self.timeout * 2)
                except subprocess.TimeoutExpired as exc:
                    self._log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cmd": shlex.join(cmd),
                               "exit": -1, "duration_s": round(time.monotonic() - started, 2), "output": "",
                               "bytes": 0, "attempt": attempt + 1})
                    raise CliError(cmd, -1, f"no response after {self.timeout * 2}s; the sign-in was not completed. "
                                            "Sign in yourself (e.g. gcloud auth login / az login / aws sso login) "
                                            "and run the command again.") from exc
                stdout, stderr = "", ""
            else:
                proc = subprocess.run(cmd_exec, capture_output=True, text=True, timeout=self.timeout,
                                      env={**os.environ, **QUIET_ENV}, stdin=subprocess.DEVNULL)
                stdout, stderr = proc.stdout, proc.stderr
            duration = round(time.monotonic() - started, 2)
            throttled = proc.returncode != 0 and any(
                s in stderr for s in ("429", "TooManyRequests", "Throttl", "RateExceeded"))
            absent = proc.returncode != 0 and any(code in stderr for code in absent_ok)
            self._log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "cmd": shlex.join(cmd), "exit": 0 if absent else proc.returncode, "duration_s": duration,
                       "output": save_as or "", "bytes": len(stdout), "attempt": attempt + 1,
                       **({"result": "not configured"} if absent else {})})
            if absent:
                return None
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
        return self._http("GET", url, token, None, headers, save_as)

    def http_post(self, url: str, token: str, body: dict, headers: dict | None = None, save_as: str | None = None):
        """Authenticated HTTPS POST, allowed only for query APIs (READ_POST_PATHS)."""
        path = url.split("?")[0].lower()
        if not any(p in path for p in READ_POST_PATHS):
            raise NotReadOnlyError(f"POST to {url.split('?')[0]} is not an allowed read")
        return self._http("POST", url, token, body, headers, save_as)

    def _http(self, method: str, url: str, token: str, body: dict | None, headers: dict | None, save_as: str | None):
        import urllib.error
        import urllib.request

        attempt, delay = 0, 5.0
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Authorization": f"Bearer {token}", **({"Content-Type": "application/json"} if data else {}),
                **(headers or {})}
        while True:
            started = time.monotonic()
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            code, text, err, retry_after = 200, "", "", None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode()
            except urllib.error.HTTPError as exc:
                code, err = exc.code, exc.read().decode(errors="replace")
                retry_after = _retry_after(exc.headers)
            except Exception as exc:  # network failure
                code, err = -1, str(exc)
            self._log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "cmd": f"{method} {url.split('?')[0]}", "exit": 0 if code == 200 else code,
                       "duration_s": round(time.monotonic() - started, 2), "output": save_as or "",
                       "bytes": len(text), "attempt": attempt + 1})
            if code in (429, 503) and attempt < self.retries:
                attempt += 1
                time.sleep(min(120.0, retry_after if retry_after is not None else delay))
                delay *= 2
                continue
            if code != 200:
                raise CliError([method, url.split("?")[0]], code, err)
            result = json.loads(text) if text else {}
            if save_as:
                self.save_json(save_as, result)
            return result

    def save_json(self, rel_path: str, data) -> Path:
        path = self.raw_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return path


def _retry_after(headers) -> float | None:
    """Seconds to wait from Retry-After or Azure Cost Management's x-ms-ratelimit-*-retry-after headers."""
    if not headers:
        return None
    for key, value in headers.items():
        k = key.lower()
        if k == "retry-after" or (k.startswith("x-ms-ratelimit") and k.endswith("retry-after")):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None
