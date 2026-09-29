"""SQLite persistence for jobs and uploaded profiles."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    builtin    INTEGER NOT NULL DEFAULT 0,
    color_space TEXT NOT NULL,
    device_class TEXT NOT NULL,
    pcs        TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    filename   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    image_sha256    TEXT NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    image_mode      TEXT NOT NULL,
    source_profile  TEXT NOT NULL,
    press_profile   TEXT NOT NULL,
    display_profile TEXT NOT NULL,
    source_intent   TEXT NOT NULL,
    proof_intent    TEXT NOT NULL,
    threshold       REAL NOT NULL,
    use_embedded    INTEGER NOT NULL DEFAULT 0,
    embedded_sha256 TEXT,
    parameters_json TEXT NOT NULL,
    profile_digests_json TEXT NOT NULL,
    de_mean         REAL,
    de_p95          REAL,
    de_max          REAL,
    over_ratio      REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_families (
    family_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    current_version INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_drafts (
    draft_id        TEXT PRIMARY KEY,
    family_id       TEXT NOT NULL REFERENCES release_families(family_id),
    candidate_sha256 TEXT NOT NULL,
    candidate_path  TEXT NOT NULL,
    candidate_name  TEXT NOT NULL,
    baseline_job_id TEXT NOT NULL,
    fixed_baseline_json TEXT NOT NULL,
    limits_json     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    latest_run_id   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_versions (
    version_id      TEXT PRIMARY KEY,
    family_id       TEXT NOT NULL REFERENCES release_families(family_id),
    version_number  INTEGER NOT NULL,
    draft_id        TEXT NOT NULL REFERENCES release_drafts(draft_id),
    gate_run_id     TEXT NOT NULL,
    profile_sha256  TEXT NOT NULL,
    snapshot_path   TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    published_at    TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at    TEXT,
    UNIQUE(family_id, version_number)
);

CREATE TABLE IF NOT EXISTS gate_runs (
    run_id          TEXT PRIMARY KEY,
    draft_id        TEXT NOT NULL REFERENCES release_drafts(draft_id),
    family_id       TEXT NOT NULL REFERENCES release_families(family_id),
    candidate_job_id TEXT NOT NULL,
    baseline_job_id TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    checks_json     TEXT NOT NULL,
    metrics_json    TEXT NOT NULL,
    localizations_json TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_audits (
    audit_id        TEXT PRIMARY KEY,
    family_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    expected_version INTEGER,
    observed_version INTEGER,
    version_id      TEXT,
    version_number  INTEGER,
    draft_id        TEXT,
    gate_run_id     TEXT,
    profile_sha256  TEXT,
    result          TEXT NOT NULL,
    reason          TEXT,
    actor           TEXT NOT NULL DEFAULT 'local-user',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS release_audits_no_update
BEFORE UPDATE ON release_audits
BEGIN
    SELECT RAISE(ABORT, 'release audit records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS release_audits_no_delete
BEFORE DELETE ON release_audits
BEGIN
    SELECT RAISE(ABORT, 'release audit records are immutable');
END;

CREATE INDEX IF NOT EXISTS idx_release_drafts_family ON release_drafts(family_id, created_at);
CREATE INDEX IF NOT EXISTS idx_release_versions_family ON release_versions(family_id, version_number);
CREATE INDEX IF NOT EXISTS idx_gate_runs_draft ON gate_runs(draft_id, created_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_profile(conn: sqlite3.Connection, record) -> None:
    conn.execute(
        """
        INSERT INTO profiles (profile_id, name, role, builtin, color_space,
                              device_class, pcs, size, sha256, filename)
        VALUES (:profile_id, :name, :role, :builtin, :color_space,
                :device_class, :pcs, :size, :sha256, :filename)
        ON CONFLICT(profile_id) DO UPDATE SET
            name=excluded.name, role=excluded.role,
            color_space=excluded.color_space, device_class=excluded.device_class,
            pcs=excluded.pcs, size=excluded.size, sha256=excluded.sha256
        """,
        {
            "profile_id": record.profile_id,
            "name": record.name,
            "role": record.role,
            "builtin": 1 if record.builtin else 0,
            "color_space": record.color_space,
            "device_class": record.device_class,
            "pcs": record.pcs,
            "size": record.size,
            "sha256": record.sha256,
            "filename": record.filename,
        },
    )


def insert_job(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs
        (job_id, image_sha256, width, height, image_mode, source_profile,
         press_profile, display_profile, source_intent, proof_intent,
         threshold, use_embedded, embedded_sha256, parameters_json,
         profile_digests_json, de_mean, de_p95, de_max, over_ratio)
        VALUES (:job_id, :image_sha256, :width, :height, :image_mode,
                :source_profile, :press_profile, :display_profile,
                :source_intent, :proof_intent, :threshold, :use_embedded,
                :embedded_sha256, :parameters_json, :profile_digests_json,
                :de_mean, :de_p95, :de_max, :over_ratio)
        """,
        {
            "job_id": job["job_id"],
            "image_sha256": job["image_sha256"],
            "width": job["width"],
            "height": job["height"],
            "image_mode": job["image_mode"],
            "source_profile": job["source_profile"],
            "press_profile": job["press_profile"],
            "display_profile": job["display_profile"],
            "source_intent": job["source_intent"],
            "proof_intent": job["proof_intent"],
            "threshold": job["threshold"],
            "use_embedded": 1 if job.get("use_embedded") else 0,
            "embedded_sha256": job.get("embedded_sha256"),
            "parameters_json": json.dumps(job["parameters"], sort_keys=True, ensure_ascii=False),
            "profile_digests_json": json.dumps(job["profile_digests"], sort_keys=True, ensure_ascii=False),
            "de_mean": job["stats"]["mean"],
            "de_p95": job["stats"]["p95"],
            "de_max": job["stats"]["max"],
            "over_ratio": job["stats"]["over_ratio"],
        },
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def recent_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Configuration-family releases and regression gates
# ---------------------------------------------------------------------------

JSON_COLUMNS = {
    "jobs": {"parameters": dict, "profile_digests": dict},
    "release_drafts": {"fixed_baseline": dict, "limits": dict},
    "gate_runs": {"checks": list, "metrics": dict, "localizations": dict},
}


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    if "fixed_baseline_json" in data:
        table = "release_drafts"
    elif "localizations_json" in data:
        table = "gate_runs"
    elif "parameters_json" in data:
        table = "jobs"
    else:
        table = None
    if table:
        for key, caster in JSON_COLUMNS[table].items():
            value = data.pop(f"{key}_json", None)
            if value is not None:
                data[key] = caster(json.loads(value))
    return data


def get_family(conn: sqlite3.Connection, family_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM release_families WHERE family_id=?", (family_id,)
    ).fetchone()


def get_family_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM release_families WHERE name=?", (name,)
    ).fetchone()


def insert_family(conn: sqlite3.Connection, family_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO release_families (family_id, name) VALUES (?, ?)",
        (family_id, name),
    )


def insert_draft(conn: sqlite3.Connection, draft: dict) -> None:
    conn.execute(
        """
        INSERT INTO release_drafts
        (draft_id, family_id, candidate_sha256, candidate_path, candidate_name,
         baseline_job_id, fixed_baseline_json, limits_json, status)
        VALUES (:draft_id, :family_id, :candidate_sha256, :candidate_path,
                :candidate_name, :baseline_job_id, :fixed_baseline_json,
                :limits_json, 'draft')
        """,
        {
            "draft_id": draft["draft_id"],
            "family_id": draft["family_id"],
            "candidate_sha256": draft["candidate_sha256"],
            "candidate_path": draft["candidate_path"],
            "candidate_name": draft["candidate_name"],
            "baseline_job_id": draft["baseline_job_id"],
            "fixed_baseline_json": json.dumps(draft["fixed_baseline"], sort_keys=True, ensure_ascii=False),
            "limits_json": json.dumps(draft["limits"], sort_keys=True, ensure_ascii=False),
        },
    )


def get_draft(conn: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM release_drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()


def list_drafts(conn: sqlite3.Connection, family_id: str | None = None) -> list[sqlite3.Row]:
    if family_id:
        return conn.execute(
            "SELECT * FROM release_drafts WHERE family_id=? ORDER BY created_at DESC, draft_id DESC",
            (family_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM release_drafts ORDER BY created_at DESC, draft_id DESC"
    ).fetchall()


def update_draft_status(conn: sqlite3.Connection, draft_id: str, status: str,
                        latest_run_id: str | None = None) -> None:
    conn.execute(
        """
        UPDATE release_drafts
        SET status=?, latest_run_id=?, updated_at=datetime('now')
        WHERE draft_id=?
        """,
        (status, latest_run_id, draft_id),
    )


def insert_gate_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """
        INSERT INTO gate_runs
        (run_id, draft_id, family_id, candidate_job_id, baseline_job_id,
         candidate_sha256, passed, checks_json, metrics_json, localizations_json)
        VALUES (:run_id, :draft_id, :family_id, :candidate_job_id,
                :baseline_job_id, :candidate_sha256, :passed, :checks_json,
                :metrics_json, :localizations_json)
        """,
        {
            "run_id": run["run_id"],
            "draft_id": run["draft_id"],
            "family_id": run["family_id"],
            "candidate_job_id": run["candidate_job_id"],
            "baseline_job_id": run["baseline_job_id"],
            "candidate_sha256": run["candidate_sha256"],
            "passed": 1 if run["passed"] else 0,
            "checks_json": json.dumps(run["checks"], ensure_ascii=False, indent=2),
            "metrics_json": json.dumps(run["metrics"], sort_keys=True, ensure_ascii=False, indent=2),
            "localizations_json": json.dumps(run["localizations"], sort_keys=True, ensure_ascii=False, indent=2),
        },
    )


def get_gate_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM gate_runs WHERE run_id=?", (run_id,)).fetchone()


def list_gate_runs(conn: sqlite3.Connection, draft_id: str | None = None) -> list[sqlite3.Row]:
    if draft_id:
        return conn.execute(
            "SELECT * FROM gate_runs WHERE draft_id=? ORDER BY created_at DESC, run_id DESC",
            (draft_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM gate_runs ORDER BY created_at DESC, run_id DESC LIMIT 50"
    ).fetchall()


def insert_version(conn: sqlite3.Connection, version: dict) -> None:
    conn.execute(
        """
        INSERT INTO release_versions
        (version_id, family_id, version_number, draft_id, gate_run_id,
         profile_sha256, snapshot_path, profile_name, status)
        VALUES (:version_id, :family_id, :version_number, :draft_id,
                :gate_run_id, :profile_sha256, :snapshot_path, :profile_name,
                :status)
        """,
        {
            "version_id": version["version_id"],
            "family_id": version["family_id"],
            "version_number": version["version_number"],
            "draft_id": version["draft_id"],
            "gate_run_id": version["gate_run_id"],
            "profile_sha256": version["profile_sha256"],
            "snapshot_path": version["snapshot_path"],
            "profile_name": version["profile_name"],
            "status": version.get("status", "active"),
        },
    )


def get_version(conn: sqlite3.Connection, version_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM release_versions WHERE version_id=?", (version_id,)
    ).fetchone()


def get_version_number(conn: sqlite3.Connection, family_id: str, number: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM release_versions WHERE family_id=? AND version_number=?",
        (family_id, number),
    ).fetchone()


def list_versions(conn: sqlite3.Connection, family_id: str | None = None) -> list[sqlite3.Row]:
    if family_id:
        return conn.execute(
            "SELECT * FROM release_versions WHERE family_id=? ORDER BY version_number DESC",
            (family_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM release_versions ORDER BY created_at DESC, version_number DESC LIMIT 100"
    ).fetchall()


def insert_audit(conn: sqlite3.Connection, audit: dict) -> None:
    conn.execute(
        """
        INSERT INTO release_audits
        (audit_id, family_id, action, expected_version, observed_version,
         version_id, version_number, draft_id, gate_run_id, profile_sha256,
         result, reason, actor)
        VALUES (:audit_id, :family_id, :action, :expected_version,
                :observed_version, :version_id, :version_number, :draft_id,
                :gate_run_id, :profile_sha256, :result, :reason, :actor)
        """,
        {
            "audit_id": audit["audit_id"],
            "family_id": audit["family_id"],
            "action": audit["action"],
            "expected_version": audit.get("expected_version"),
            "observed_version": audit.get("observed_version"),
            "version_id": audit.get("version_id"),
            "version_number": audit.get("version_number"),
            "draft_id": audit.get("draft_id"),
            "gate_run_id": audit.get("gate_run_id"),
            "profile_sha256": audit.get("profile_sha256"),
            "result": audit["result"],
            "reason": audit.get("reason"),
            "actor": audit.get("actor", "local-user"),
        },
    )


def list_audits(conn: sqlite3.Connection, family_id: str | None = None,
                limit: int = 100) -> list[sqlite3.Row]:
    if family_id:
        return conn.execute(
            "SELECT * FROM release_audits WHERE family_id=? ORDER BY created_at DESC, audit_id DESC LIMIT ?",
            (family_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM release_audits ORDER BY created_at DESC, audit_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
