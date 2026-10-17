"""Savings register (SQLite).

Keeps findings and the client's decisions across assessment runs:
  estimated savings  - from the latest run (scenario model)
  validated savings  - entered after owner review
  realized savings   - measured from billing after implementation (`verify`)
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import CostRecord, Recommendation

STATUSES = ("open", "in-review", "approved", "rejected", "implemented", "verified", "deferred")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, provider TEXT, run_dir TEXT,
  period_start TEXT, period_end TEXT, window_spend REAL, est_savings REAL, findings INTEGER);
CREATE TABLE IF NOT EXISTS findings (
  key TEXT PRIMARY KEY, rule_id TEXT, resource_id TEXT, resource_name TEXT, service TEXT, category TEXT,
  title TEXT, owner TEXT, account TEXT, confidence TEXT, est_monthly_savings REAL, savings_low REAL,
  savings_high REAL, current_monthly_cost REAL, scenario_group TEXT, payload TEXT,
  first_seen_run INTEGER, last_seen_run INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS decisions (
  key TEXT PRIMARY KEY REFERENCES findings(key), status TEXT DEFAULT 'open', owner TEXT,
  validated_savings REAL, implemented_on TEXT, realized_savings REAL, notes TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS resource_costs (
  resource_id TEXT, day TEXT, cost REAL, PRIMARY KEY (resource_id, day));
"""


class Register:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ ingest

    def record_run(self, provider: str, run_dir: str, period: tuple[str, str], window_spend: float,
                   recs: list[Recommendation], est_savings: float, records: list[CostRecord]) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.db.execute(
            "INSERT INTO runs (created_at, provider, run_dir, period_start, period_end, window_spend, est_savings, findings)"
            " VALUES (?,?,?,?,?,?,?,?)", (now, provider, run_dir, *period, window_spend, est_savings, len(recs)))
        run_id = cur.lastrowid
        seen = set()
        for r in recs:
            seen.add(r.key)
            self.db.execute("""
                INSERT INTO findings (key, rule_id, resource_id, resource_name, service, category, title, owner, account,
                  confidence, est_monthly_savings, savings_low, savings_high, current_monthly_cost, scenario_group,
                  payload, first_seen_run, last_seen_run, active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(key) DO UPDATE SET title=excluded.title, owner=excluded.owner,
                  confidence=excluded.confidence, est_monthly_savings=excluded.est_monthly_savings,
                  savings_low=excluded.savings_low, savings_high=excluded.savings_high,
                  current_monthly_cost=excluded.current_monthly_cost, scenario_group=excluded.scenario_group,
                  payload=excluded.payload, last_seen_run=excluded.last_seen_run, active=1
            """, (r.key, r.rule_id, r.resource_id, r.resource_name, r.service, r.category, r.title, r.owner,
                  r.account, r.confidence, r.est_monthly_savings, r.savings_low, r.savings_high,
                  r.current_monthly_cost, r.scenario_group, json.dumps(r.to_dict(), default=str), run_id, run_id))
            # decisions.owner holds only an owner a person assigned; the suggestion lives in findings.owner
            self.db.execute("INSERT OR IGNORE INTO decisions (key, status, updated_at) VALUES (?, 'open', ?)",
                            (r.key, now))
        # Findings that disappeared: keep, but mark inactive (resolved? or resource gone)
        for row in self.db.execute("SELECT key FROM findings WHERE active=1").fetchall():
            if row["key"] not in seen:
                self.db.execute("UPDATE findings SET active=0 WHERE key=?", (row["key"],))
        self.db.executemany(
            "INSERT OR REPLACE INTO resource_costs (resource_id, day, cost) VALUES (?,?,?)",
            [(rid, d, c) for (rid, d), c in _daily_by_resource(records).items()])
        self.db.commit()
        return run_id

    # ------------------------------------------------------------------ decisions

    def update(self, key_or_prefix: str, **fields) -> int:
        allowed = {"status", "owner", "validated_savings", "implemented_on", "notes"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown field(s): {bad}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        keys = self._match(key_or_prefix)
        if not keys:
            raise ValueError(f"No finding matches '{key_or_prefix}'")
        if len(keys) > 1:
            raise ValueError(f"'{key_or_prefix}' matches {len(keys)} findings; use the full id")
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        self.db.execute(f"UPDATE decisions SET {sets} WHERE key=?",
                        (*fields.values(), datetime.now(timezone.utc).isoformat(timespec="seconds"), keys[0]))
        self.db.commit()
        return 1

    def _match(self, key_or_id: str) -> list[str]:
        rows = self.db.execute("SELECT key, rowid FROM findings").fetchall()
        if key_or_id.isdigit():
            return [r["key"] for r in rows if str(r["rowid"]) == key_or_id]
        exact = [r["key"] for r in rows if r["key"] == key_or_id]
        return exact or [r["key"] for r in rows if key_or_id in r["key"]]

    def rows(self, status: str | None = None, active_only: bool = True) -> list[sqlite3.Row]:
        q = ("SELECT f.rowid AS id, f.*, d.status, d.owner AS decision_owner, d.validated_savings, d.implemented_on,"
             " d.realized_savings, d.notes FROM findings f JOIN decisions d USING(key)")
        cond, args = [], []
        if active_only:
            cond.append("(f.active=1 OR d.status IN ('implemented','verified'))")
        if status:
            cond.append("d.status=?")
            args.append(status)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        return self.db.execute(q + " ORDER BY f.est_monthly_savings DESC", args).fetchall()

    def decisions(self) -> dict[str, dict]:
        return {r["key"]: dict(r) for r in self.rows(active_only=False)}

    def totals(self) -> dict[str, float]:
        rows = self.rows(active_only=False)
        return {
            "validated": sum(r["validated_savings"] or 0 for r in rows if r["status"] not in ("rejected",)),
            "realized": sum(r["realized_savings"] or 0 for r in rows),
            "approved": sum(1 for r in rows if r["status"] == "approved"),
            "implemented": sum(1 for r in rows if r["status"] in ("implemented", "verified")),
            "rejected": sum(1 for r in rows if r["status"] == "rejected"),
            "open": sum(1 for r in rows if r["status"] in ("open", "in-review") and r["active"]),
        }

    # ------------------------------------------------------------------ verification

    def verify(self, min_days_after: int = 7, baseline_days: int = 30) -> list[dict]:
        """Realized savings = (avg daily cost before - avg daily cost after implementation) x 30.

        Uses resource-level daily costs stored from runs. Does not adjust for usage growth;
        the report states this.
        """
        results = []
        for r in self.rows(active_only=False):
            if r["status"] not in ("implemented", "verified") or not r["implemented_on"] or not r["resource_id"]:
                continue
            impl = date.fromisoformat(r["implemented_on"])
            costs = {date.fromisoformat(row["day"]): row["cost"] for row in self.db.execute(
                "SELECT day, cost FROM resource_costs WHERE resource_id=?", (r["resource_id"],))}
            last_day, first_day = max(costs, default=impl), min(costs, default=impl)
            before = [costs.get(impl - timedelta(days=i), 0.0) for i in range(1, baseline_days + 1)
                      if impl - timedelta(days=i) >= first_day]  # only days covered by billing history
            if not before:
                results.append({"key": r["key"], "status": "no baseline cost history before implementation"})
                continue
            after_days = (last_day - impl).days
            if after_days < min_days_after:
                results.append({"key": r["key"], "status": f"waiting ({after_days} days of data after change)"})
                continue
            after = [costs.get(impl + timedelta(days=i), 0.0) for i in range(1, after_days + 1)]
            realized = (sum(before) / len(before) - sum(after) / len(after)) * 30
            self.db.execute("UPDATE decisions SET realized_savings=?, status='verified', updated_at=? WHERE key=?",
                            (realized, datetime.now(timezone.utc).isoformat(timespec="seconds"), r["key"]))
            results.append({"key": r["key"], "status": "verified", "realized_monthly": realized,
                            "estimated_monthly": r["est_monthly_savings"]})
        self.db.commit()
        return results


def _daily_by_resource(records: list[CostRecord]) -> dict[tuple[str, str], float]:
    out = defaultdict(float)
    for r in records:
        if r.resource_id:
            out[(r.resource_id, r.day.isoformat())] += r.cost
    return out
