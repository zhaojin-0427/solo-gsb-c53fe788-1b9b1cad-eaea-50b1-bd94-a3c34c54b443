"""Press-profile version drafts, immutable snapshots and regression gates."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageCms
from pydantic import BaseModel, Field

from . import config, database as db
from . import lcms_native
from . import pipeline as pl
from . import storage
from .profiles import ProfileManager
from .profiles import ProfileError

router = APIRouter(prefix="/api/releases", tags=["releases"])

# The application is a local, single-process SQLite service. This lock orders
# publish/withdraw/restore transactions; the expected version still detects
# stale UI requests and every decision is appended to the immutable audit log.
_release_lock = threading.RLock()


class ReleaseError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


class ExpectedVersion(BaseModel):
    expected_version: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Paths and profile snapshots
# ---------------------------------------------------------------------------

def _roots() -> tuple[Path, Path, Path, Path]:
    root = Path(config.DATA_DIR) / "releases"
    drafts = root / "drafts"
    snapshots = root / "snapshots"
    runs = root / "gate-runs"
    for path in (drafts, snapshots, runs):
        path.mkdir(parents=True, exist_ok=True)
    return root, drafts, snapshots, runs


class SnapshotProfileManager:
    """Profile resolver that pins selected ICC bytes by SHA-256.

    All other profile lookups delegate to the normal profile manager, so a
    regression run cannot observe later uploads or replacements.
    """

    def __init__(self, base: ProfileManager, snapshots: dict[str, Path]):
        self.base = base
        self.snapshots = dict(snapshots)

    def _snapshot_path(self, profile_id: str) -> Path:
        try:
            return self.snapshots[profile_id]
        except KeyError as exc:
            raise ProfileError(f"快照配置不存在: {profile_id}") from exc

    def load_bytes(self, profile_id: str) -> bytes:
        if profile_id in self.snapshots:
            return self._snapshot_path(profile_id).read_bytes()
        return self.base.load_bytes(profile_id)

    def get(self, profile_id: str, role: str):
        data = self.load_bytes(profile_id)
        header = lcms_native.read_header(data)
        prof = ImageCms.ImageCmsProfile(io.BytesIO(data))
        self.validate_for_role(header, role)
        return prof

    def get_bytes_header(self, profile_id: str) -> tuple[bytes, dict]:
        data = self.load_bytes(profile_id)
        return data, lcms_native.read_header(data)

    def validate_for_role(self, header: dict, role: str) -> None:
        self.base.validate_for_role(header, role)

    def open_pil(self, profile_id: str):
        return ImageCms.ImageCmsProfile(io.BytesIO(self.load_bytes(profile_id)))

    def profile_digest(self, profile_id: str) -> str:
        return hashlib.sha256(self.load_bytes(profile_id)).hexdigest()

    def summary(self, profile_id: str) -> dict:
        data = self.load_bytes(profile_id)
        header = lcms_native.read_header(data)
        try:
            name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(data))).strip()
        except Exception:
            name = Path(self._snapshot_path(profile_id)).stem if profile_id in self.snapshots else profile_id
        return {
            "profile_id": profile_id,
            "name": name or "immutable-snapshot",
            "sha256": hashlib.sha256(data).hexdigest(),
            **header,
        }


def _validate_candidate_press(data: bytes) -> dict:
    try:
        header = lcms_native.read_header(data)
        ImageCms.ImageCmsProfile(io.BytesIO(data))
    except Exception as exc:
        raise ReleaseError(f"候选印刷 ICC 配置无效: {exc}", 422) from exc
    if header["color_space"] != "CMYK" or header["device_class"] not in ("prtr", "spac"):
        raise ReleaseError(
            f"候选配置必须是 CMYK 输出配置（prtr/spac），实际为 "
            f"{header['device_class']}/{header['color_space']}",
            422,
        )
    try:
        name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(data))).strip()
    except Exception:
        name = ""
    header["name"] = name or "候选印刷配置"
    return header


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_as_snapshot(source: Path | bytes, target: Path, expected_sha: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        actual = _sha256(target)
        if actual != expected_sha:
            raise ReleaseError(f"快照摘要冲突: {target}", 409)
        return
    if isinstance(source, bytes):
        target.write_bytes(source)
    else:
        shutil.copy2(source, target)
    actual = _sha256(target)
    if actual != expected_sha:
        raise ReleaseError("快照写入后 SHA-256 校验失败", 500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _limits(mean: float, p95: float, over_ratio: float, changed_pixels: int) -> dict:
    values = {
        "max_de_mean": float(mean),
        "max_de_p95": float(p95),
        "max_over_threshold_ratio": float(over_ratio),
        "max_mask_changed_pixels": int(changed_pixels),
    }
    if not all(np.isfinite(v) for key, v in values.items() if key != "max_mask_changed_pixels"):
        raise ReleaseError("门禁阈值必须是有限数字")
    if values["max_de_mean"] < 0 or values["max_de_p95"] < 0:
        raise ReleaseError("ΔE 均值和 P95 上限不能为负")
    if not 0 <= values["max_over_threshold_ratio"] <= 1:
        raise ReleaseError("超阈面积比例上限必须位于 0 到 1 之间")
    if values["max_mask_changed_pixels"] < 0:
        raise ReleaseError("遮罩变化像素上限不能为负")
    return values


def _require(conn: sqlite3.Connection, row: sqlite3.Row | None, message: str,
             status: int = 404) -> sqlite3.Row:
    if row is None:
        raise ReleaseError(message, status)
    return row


def _run_dir(run_id: str) -> Path:
    return _roots()[3] / run_id


def _write_audit(conn: sqlite3.Connection, **audit: Any) -> None:
    payload = {"audit_id": uuid.uuid4().hex, "actor": "local-user", **audit}
    db.insert_audit(conn, payload)
    conn.commit()


def _current_number(conn: sqlite3.Connection, family_id: str) -> int:
    family = _require(conn, db.get_family(conn, family_id), "配置族不存在", 404)
    return int(family["current_version"] or 0)


# ---------------------------------------------------------------------------
# Gate execution
# ---------------------------------------------------------------------------

def _sample_coordinates(mask: np.ndarray, limit: int = 200) -> dict:
    coords_yx = np.argwhere(mask)
    count = int(len(coords_yx))
    if count == 0:
        return {"count": 0, "sample": [], "bounds": None}
    if count <= limit:
        selected = coords_yx
    else:
        idx = np.linspace(0, count - 1, limit, dtype=np.int64)
        selected = coords_yx[idx]
    points = [[int(y), int(x)] for y, x in selected]
    return {
        "count": count,
        "sample": points,
        "bounds": {
            "top": int(coords_yx[:, 0].min()),
            "left": int(coords_yx[:, 1].min()),
            "bottom": int(coords_yx[:, 0].max()) + 1,
            "right": int(coords_yx[:, 1].max()) + 1,
        },
    }


def _top_delta_e_locations(de: np.ndarray, limit: int = 100) -> list[list[int]]:
    flat = de.reshape(-1)
    take = min(limit, flat.size)
    if take == 0:
        return []
    candidates = np.argpartition(flat, -take)[-take:]
    candidates = candidates[np.argsort(flat[candidates])[::-1]]
    h, w = de.shape
    return [[int(i // w), int(i % w)] for i in candidates]


def _change_overlay(proof: np.ndarray, added: np.ndarray, removed: np.ndarray) -> bytes:
    visual = proof.astype(np.float32).copy()
    visual[added] = visual[added] * 0.35 + np.array([255, 40, 40], np.float32) * 0.65
    visual[removed] = visual[removed] * 0.35 + np.array([50, 110, 255], np.float32) * 0.65
    buf = io.BytesIO()
    Image.fromarray(np.rint(np.clip(visual, 0, 255)).astype(np.uint8), "RGB").save(
        buf, format="PNG"
    )
    return buf.getvalue()


def _load_baseline(conn: sqlite3.Connection, baseline_job_id: str, pm: ProfileManager,
                   verify_live_profiles: bool = True) -> tuple[dict, Path, bytes]:
    row = _require(conn, db.get_job(conn, baseline_job_id), "基线任务不存在，请先运行并保存代表性任务", 404)
    base = Path(config.JOB_DIR) / baseline_job_id
    if not (base / "meta.json").exists() or not (base / "original.bin").exists():
        raise ReleaseError("基线任务产物不完整，无法固定图像与参数", 422)
    image_data = (base / "original.bin").read_bytes()
    if hashlib.sha256(image_data).hexdigest() != row["image_sha256"]:
        raise ReleaseError("基线图像 SHA-256 与数据库记录不一致", 409)

    fixed = {
        "job_id": baseline_job_id,
        "image_sha256": row["image_sha256"],
        "width": row["width"],
        "height": row["height"],
        "image_mode": row["image_mode"],
        "source_profile": row["source_profile"],
        "display_profile": row["display_profile"],
        "baseline_press_profile": row["press_profile"],
        "baseline_press_sha256": json.loads(row["profile_digests_json"]).get("press"),
        "source_profile_sha256": json.loads(row["profile_digests_json"]).get("source"),
        "display_profile_sha256": json.loads(row["profile_digests_json"]).get("display"),
        "source_intent": row["source_intent"],
        "proof_intent": row["proof_intent"],
        "threshold": row["threshold"],
        "use_embedded": bool(row["use_embedded"]),
        "embedded_sha256": row["embedded_sha256"],
    }

    if verify_live_profiles:
        # Fail early if a non-embedded source/display profile has been removed.
        if not fixed["use_embedded"]:
            pm.load_bytes(fixed["source_profile"])
        pm.load_bytes(fixed["display_profile"])
    return fixed, base, image_data


def _build_input_snapshots(fixed: dict, image_data: bytes, pm: ProfileManager,
                           family_snapshot_dir: Path) -> tuple[dict, dict]:
    files: dict[str, Path] = {}
    summaries: dict[str, dict] = {}

    image_target = family_snapshot_dir / f"image-{fixed['image_sha256'][:16]}.bin"
    _copy_as_snapshot(image_data, image_target, fixed["image_sha256"])
    files["image"] = image_target

    if not fixed["use_embedded"]:
        source_target = family_snapshot_dir / f"source-{source_sha[:16]}.icc"
        if source_target.exists():
            source_data = source_target.read_bytes()
            if hashlib.sha256(source_data).hexdigest() != source_sha:
                raise ReleaseError("已固定的源配置快照 SHA-256 不匹配", 409)
        else:
            source_data = pm.load_bytes(fixed["source_profile"])
            if hashlib.sha256(source_data).hexdigest() != source_sha:
                raise ReleaseError("源配置在基线保存后发生变化，拒绝重跑", 409)
        _copy_as_snapshot(source_data, source_target, source_sha)
        source_id = f"snapshot:source:{source_sha[:16]}"
        files[source_id] = source_target
        summaries["source"] = {"sha256": source_sha, "embedded": False}
    else:
        source_id = "embedded-source"
        summaries["source"] = {"sha256": fixed["embedded_sha256"], "embedded": True}

    display_sha = fixed["display_profile_sha256"]
    display_target = family_snapshot_dir / f"display-{display_sha[:16]}.icc"
    if display_target.exists():
        display_data = display_target.read_bytes()
        if hashlib.sha256(display_data).hexdigest() != display_sha:
            raise ReleaseError("已固定的显示配置快照 SHA-256 不匹配", 409)
    else:
        display_data = pm.load_bytes(fixed["display_profile"])
        if hashlib.sha256(display_data).hexdigest() != display_sha:
            raise ReleaseError("显示配置在基线保存后发生变化，拒绝重跑", 409)
    _copy_as_snapshot(display_data, display_target, display_sha)
    display_id = f"snapshot:display:{display_sha[:16]}"
    files[display_id] = display_target
    summaries["display"] = {"sha256": display_sha, "embedded": False}
    return files, summaries


def _make_check(name: str, key: str, candidate: float, baseline: float, limit: float,
                passed: bool, reason: str, extra: dict | None = None) -> dict:
    check = {
        "key": key,
        "name": name,
        "passed": passed,
        "candidate": round(float(candidate), 6),
        "baseline": round(float(baseline), 6),
        "delta_from_baseline": round(float(candidate - baseline), 6),
        "limit": round(float(limit), 6),
        "reason": reason,
    }
    if extra:
        check.update(extra)
    return check


def execute_gate(conn: sqlite3.Connection, draft_row: sqlite3.Row, state) -> dict:
    draft = db.row_to_dict(draft_row)
    family = _require(conn, db.get_family(conn, draft["family_id"]), "配置族不存在", 404)
    pm = state.pm

    candidate_path = Path(draft["candidate_path"])
    if not candidate_path.exists():
        raise ReleaseError("草稿 ICC 文件不存在", 409)
    candidate_bytes = candidate_path.read_bytes()
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    if candidate_sha != draft["candidate_sha256"]:
        raise ReleaseError("草稿 ICC 在保存后被修改，拒绝重跑", 409)

    fixed, baseline_dir, image_data = _load_baseline(
        conn, draft["baseline_job_id"], pm, verify_live_profiles=False
    )
    _, _, snapshot_root, _ = _roots()
    family_snapshot_dir = snapshot_root / draft["family_id"]
    image_snapshot = family_snapshot_dir / f"image-{fixed['image_sha256'][:16]}.bin"
    if image_snapshot.exists():
        if _sha256(image_snapshot) != fixed["image_sha256"]:
            raise ReleaseError("固定基线图像快照 SHA-256 不匹配", 409)
        image_data = image_snapshot.read_bytes()
    else:
        _copy_as_snapshot(image_data, image_snapshot, fixed["image_sha256"])
    baseline_press_target = family_snapshot_dir / f"baseline-press-{fixed['baseline_press_sha256'][:16]}.icc"
    if baseline_press_target.exists():
        baseline_press_data = baseline_press_target.read_bytes()
        baseline_press_sha = hashlib.sha256(baseline_press_data).hexdigest()
        if baseline_press_sha != fixed["baseline_press_sha256"]:
            raise ReleaseError("基线印刷配置快照 SHA-256 不匹配", 409)
    else:
        baseline_press_data = pm.load_bytes(fixed["baseline_press_profile"])
        baseline_press_sha = hashlib.sha256(baseline_press_data).hexdigest()
        if baseline_press_sha != fixed["baseline_press_sha256"]:
            raise ReleaseError("基线印刷配置在任务保存后发生变化，拒绝重跑", 409)
        _copy_as_snapshot(baseline_press_data, baseline_press_target, baseline_press_sha)
    candidate_target = family_snapshot_dir / f"press-{candidate_sha[:16]}.icc"
    _copy_as_snapshot(candidate_bytes, candidate_target, candidate_sha)
    snapshot_files, _ = _build_input_snapshots(fixed, image_data, pm, family_snapshot_dir)
    candidate_id = f"snapshot:press:{candidate_sha[:16]}"
    snapshot_files[candidate_id] = candidate_target
    snapshot_files["baseline:press"] = baseline_press_target

    source_id = "embedded-source" if fixed["use_embedded"] else f"snapshot:source:{fixed['source_profile_sha256'][:16]}"
    display_id = f"snapshot:display:{fixed['display_profile_sha256'][:16]}"
    snapshot_pm = SnapshotProfileManager(pm, snapshot_files)

    inputs = pl.PipelineInputs(
        image_data=image_data,
        source_profile_id=source_id,
        press_profile_id=candidate_id,
        display_profile_id=display_id,
        source_intent=fixed["source_intent"],
        proof_intent=fixed["proof_intent"],
        threshold=float(fixed["threshold"]),
        use_embedded=bool(fixed["use_embedded"]),
        max_dimension=config.MAX_DIMENSION,
    )
    try:
        result = pl.run_pipeline(inputs, snapshot_pm)
    except (pl.PipelineError, ProfileError) as exc:
        raise ReleaseError(f"快照任务重跑失败: {exc}", 422) from exc

    candidate_job_id = pl.compute_job_id(result)
    run_id = uuid.uuid4().hex
    out_dir = _roots()[3] / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_base = out_dir / "candidate-job"
    storage.persist_pipeline_result(
        result=result,
        job_id=candidate_job_id,
        image_data=image_data,
        source_profile=source_id,
        press_profile=candidate_id,
        display_profile=display_id,
        base=candidate_base,
        pm=snapshot_pm,
        srgb_bytes=state.srgb_bytes,
    )

    baseline_de = np.load(baseline_dir / "de.npy")
    baseline_mask = np.load(baseline_dir / "mask3.npy")[..., 0] > 0
    candidate_de = result.de
    candidate_mask = result.mask[..., 0] > 0
    if baseline_de.shape != candidate_de.shape:
        raise ReleaseError("基线与候选图像尺寸不一致，无法比较遮罩", 422)

    added = candidate_mask & ~baseline_mask
    removed = baseline_mask & ~candidate_mask
    changed = added | removed
    changed_count = int(changed.sum())
    added_count = int(added.sum())
    removed_count = int(removed.sum())
    change_locations = _sample_coordinates(changed, 250)
    added_locations = _sample_coordinates(added, 100)
    removed_locations = _sample_coordinates(removed, 100)
    over_locations = _sample_coordinates(candidate_mask, 250)

    limits = draft["limits"]
    stats = result.stats
    baseline_job = db.get_job(conn, draft["baseline_job_id"])
    baseline_values = {
        "mean": baseline_job["de_mean"],
        "p95": baseline_job["de_p95"],
        "over_ratio": baseline_job["over_ratio"],
    }

    checks = [
        _make_check(
            "ΔE00 均值", "de_mean", stats["mean"], baseline_values["mean"],
            limits["max_de_mean"], stats["mean"] <= limits["max_de_mean"],
            (f"候选均值 {stats['mean']:.4f}，允许上限 "
             f"{limits['max_de_mean']:.4f}，相对基线变化 "
             f"{stats['mean'] - baseline_values['mean']:+.4f}"),
            {"localizations": {"highest_delta_e": _top_delta_e_locations(candidate_de)}},
        ),
        _make_check(
            "ΔE00 P95", "de_p95", stats["p95"], baseline_values["p95"],
            limits["max_de_p95"], stats["p95"] <= limits["max_de_p95"],
            (f"候选 P95 {stats['p95']:.4f}，允许上限 "
             f"{limits['max_de_p95']:.4f}，相对基线变化 "
             f"{stats['p95'] - baseline_values['p95']:+.4f}"),
            {"localizations": {"highest_delta_e": _top_delta_e_locations(candidate_de)}},
        ),
        _make_check(
            "超阈面积比例", "over_threshold_ratio", stats["over_ratio"],
            baseline_values["over_ratio"], limits["max_over_threshold_ratio"],
            stats["over_ratio"] <= limits["max_over_threshold_ratio"],
            (f"候选超阈比例 {stats['over_ratio']:.6f}，允许上限 "
             f"{limits['max_over_threshold_ratio']:.6f}，超阈像素 "
             f"{stats['over_pixels']} / {stats['total_pixels']}"),
            {"localizations": {"over_threshold_pixels": over_locations}},
        ),
        _make_check(
            "遮罩变化像素", "mask_changed_pixels", changed_count, 0,
            limits["max_mask_changed_pixels"],
            changed_count <= limits["max_mask_changed_pixels"],
            (f"遮罩变化 {changed_count} 像素（新增 {added_count}、消失 {removed_count}），"
             f"允许上限 {limits['max_mask_changed_pixels']}"),
            {
                "changed_pixel_count": changed_count,
                "added_pixel_count": added_count,
                "removed_pixel_count": removed_count,
                "localizations": {
                    "changed_pixels": change_locations,
                    "added_pixels": added_locations,
                    "removed_pixels": removed_locations,
                },
            },
        ),
    ]
    # Replace generic pass/fail wording with an explicit reason prefix.
    for check in checks:
        check["reason"] = ("通过：" if check["passed"] else "失败：") + check["reason"]

    passed = all(item["passed"] for item in checks)
    np.save(out_dir / "changed_mask.npy", changed.astype(np.uint8))
    (out_dir / "changes.png").write_bytes(_change_overlay(np.asarray(result.proof), added, removed))

    metrics = {
        "candidate": stats,
        "baseline": {
            "mean": baseline_values["mean"],
            "p95": baseline_values["p95"],
            "over_ratio": baseline_values["over_ratio"],
        },
        "mask": {
            "changed_pixels": changed_count,
            "added_pixels": added_count,
            "removed_pixels": removed_count,
        },
    }
    localizations = {
        "changed_pixels": change_locations,
        "added_pixels": added_locations,
        "removed_pixels": removed_locations,
        "over_threshold_pixels": over_locations,
    }
    run = {
        "run_id": run_id,
        "draft_id": draft["draft_id"],
        "family_id": draft["family_id"],
        "candidate_job_id": candidate_job_id,
        "baseline_job_id": draft["baseline_job_id"],
        "candidate_sha256": candidate_sha,
        "passed": passed,
        "checks": checks,
        "metrics": metrics,
        "localizations": localizations,
    }
    db.insert_gate_run(conn, run)
    db.update_draft_status(conn, draft["draft_id"], "passed" if passed else "failed", run_id)
    (out_dir / "gate-report.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True), "utf-8"
    )
    manifest = {
        "run_id": run_id,
        "family_id": draft["family_id"],
        "family_name": family["name"],
        "baseline": fixed,
        "candidate_sha256": candidate_sha,
        "baseline_press_sha256": baseline_press_sha,
        "snapshots": {key: str(path) for key, path in snapshot_files.items()},
        "limits": draft["limits"],
    }
    (out_dir / "snapshot-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), "utf-8"
    )
    conn.commit()
    return gate_response(db.row_to_dict(db.get_gate_run(conn, run_id)))


def gate_response(run: dict) -> dict:
    run = dict(run)
    run["passed"] = bool(run["passed"])
    run["images"] = {
        "changes": f"/api/releases/gate-runs/{run['run_id']}/changes.png",
        "candidate_softproof": f"/api/releases/gate-runs/{run['run_id']}/candidate/images/softproof.png",
        "candidate_mask": f"/api/releases/gate-runs/{run['run_id']}/candidate/images/mask.png",
        "candidate_preview": f"/api/releases/gate-runs/{run['run_id']}/candidate/images/preview.png",
        "candidate_heatmap": f"/api/releases/gate-runs/{run['run_id']}/candidate/images/heatmap.png",
        "candidate_overlay": f"/api/releases/gate-runs/{run['run_id']}/candidate/images/overlay.png",
        "baseline_softproof": f"/api/jobs/{run['baseline_job_id']}/images/softproof.png",
        "baseline_mask": f"/api/jobs/{run['baseline_job_id']}/images/mask.png",
    }
    run["candidate_report"] = f"/api/releases/gate-runs/{run['run_id']}/candidate/report"
    run["snapshot_manifest"] = f"/api/releases/gate-runs/{run['run_id']}/snapshot-manifest"
    run["report"] = f"/api/releases/gate-runs/{run['run_id']}/report"
    return run


# ---------------------------------------------------------------------------
# API: overview, drafts and gates
# ---------------------------------------------------------------------------

@router.get("/overview")
def overview(request: Request):
    conn = request.app.state.db
    return {
        "families": [dict(r) for r in conn.execute(
            "SELECT * FROM release_families ORDER BY updated_at DESC, family_id"
        ).fetchall()],
        "drafts": [db.row_to_dict(r) for r in db.list_drafts(conn)],
        "versions": [version_response(r) for r in conn.execute(
            "SELECT * FROM release_versions ORDER BY published_at DESC, version_id DESC LIMIT 100"
        ).fetchall()],
        "gate_runs": [gate_response(db.row_to_dict(r)) for r in db.list_gate_runs(conn)],
        "audits": [dict(r) for r in db.list_audits(conn)],
        "jobs": [dict(r) for r in db.recent_jobs(conn, 50)],
    }


@router.post("/drafts")
async def create_draft(
    request: Request,
    file: UploadFile = File(...),
    family_name: str = Form(""),
    family_id: str = Form(""),
    baseline_job_id: str = Form(...),
    max_de_mean: float = Form(...),
    max_de_p95: float = Form(...),
    max_over_threshold_ratio: float = Form(...),
    max_mask_changed_pixels: int = Form(...),
):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "候选 ICC 配置文件过大（上限 10MB）")
    header = _validate_candidate_press(data)
    limits = _limits(max_de_mean, max_de_p95, max_over_threshold_ratio, max_mask_changed_pixels)
    conn = request.app.state.db
    pm = request.app.state.pm

    with _release_lock:
        if family_id:
            family = db.get_family(conn, family_id)
            if family is None:
                raise HTTPException(404, "配置族不存在")
        else:
            name = family_name.strip()
            if not name:
                raise HTTPException(422, "新配置族必须填写名称")
            if db.get_family_by_name(conn, name) is not None:
                raise HTTPException(409, f"配置族名称已存在: {name}")
            family_id = "fam-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            db.insert_family(conn, family_id, name)
            conn.commit()
            family = db.get_family(conn, family_id)

        fixed, _, baseline_image = _load_baseline(conn, baseline_job_id, pm)
        candidate_sha = hashlib.sha256(data).hexdigest()
        family_snapshot_dir = _roots()[2] / family_id
        baseline_press_data = pm.load_bytes(fixed["baseline_press_profile"])
        baseline_press_sha = hashlib.sha256(baseline_press_data).hexdigest()
        if baseline_press_sha != fixed["baseline_press_sha256"]:
            raise HTTPException(409, "基线印刷配置在任务保存后发生变化，拒绝固定快照")
        baseline_press_target = family_snapshot_dir / f"baseline-press-{baseline_press_sha[:16]}.icc"
        _copy_as_snapshot(baseline_press_data, baseline_press_target, baseline_press_sha)
        candidate_target = family_snapshot_dir / f"press-{candidate_sha[:16]}.icc"
        _copy_as_snapshot(data, candidate_target, candidate_sha)
        snapshot_files, _ = _build_input_snapshots(fixed, baseline_image, pm, family_snapshot_dir)
        snapshot_files["baseline:press"] = baseline_press_target
        snapshot_files[f"snapshot:press:{candidate_sha[:16]}"] = candidate_target

        draft_id = "draft-" + uuid.uuid4().hex
        draft_target = _roots()[1] / f"{draft_id}.icc"
        _copy_as_snapshot(candidate_target, draft_target, candidate_sha)
        (family_snapshot_dir / f"draft-{draft_id}-manifest.json").write_text(
            json.dumps({
                "draft_id": draft_id,
                "family_id": family_id,
                "baseline": fixed,
                "limits": limits,
                "candidate_sha256": candidate_sha,
                "snapshots": {key: str(path) for key, path in snapshot_files.items()},
            }, ensure_ascii=False, indent=2, sort_keys=True),
            "utf-8",
        )
        db.insert_draft(conn, {
            "draft_id": draft_id,
            "family_id": family_id,
            "candidate_sha256": candidate_sha,
            "candidate_path": str(draft_target),
            "candidate_name": header["name"],
            "baseline_job_id": baseline_job_id,
            "fixed_baseline": fixed,
            "limits": limits,
        })
        conn.commit()
        return db.row_to_dict(db.get_draft(conn, draft_id))


@router.get("/drafts")
def list_drafts(request: Request, family_id: str | None = None):
    return [db.row_to_dict(r) for r in db.list_drafts(request.app.state.db, family_id)]


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str, request: Request):
    row = db.get_draft(request.app.state.db, draft_id)
    if row is None:
        raise HTTPException(404, "草稿不存在")
    return db.row_to_dict(row)


@router.post("/drafts/{draft_id}/gate")
def rerun_gate(draft_id: str, request: Request):
    conn = request.app.state.db
    with _release_lock:
        row = db.get_draft(conn, draft_id)
        if row is None:
            raise HTTPException(404, "草稿不存在")
        try:
            return execute_gate(conn, row, request.app.state)
        except ReleaseError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/gate-runs/{run_id}")
def get_gate_run(run_id: str, request: Request):
    row = db.get_gate_run(request.app.state.db, run_id)
    if row is None:
        raise HTTPException(404, "门禁运行不存在")
    return gate_response(db.row_to_dict(row))


@router.get("/gate-runs/{run_id}/changes.png")
def get_gate_changes(run_id: str):
    path = _run_dir(run_id) / "changes.png"
    if not path.exists():
        raise HTTPException(404, "变化定位图不存在")
    return FileResponse(path, media_type="image/png")


@router.get("/gate-runs/{run_id}/candidate/images/{name}")
def get_gate_candidate_image(run_id: str, name: str):
    allowed = {"preview.png", "softproof.png", "heatmap.png", "overlay.png", "mask.png"}
    if name not in allowed:
        raise HTTPException(404, "图像不存在")
    path = _run_dir(run_id) / "candidate-job" / name
    if not path.exists():
        raise HTTPException(404, "候选任务图像不存在")
    return FileResponse(path, media_type="image/png")


@router.get("/gate-runs/{run_id}/candidate/report")
def get_gate_candidate_report(run_id: str):
    path = _run_dir(run_id) / "candidate-job" / "report.json"
    if not path.exists():
        raise HTTPException(404, "候选任务报告不存在")
    return FileResponse(path, media_type="application/json", filename=f"{run_id[:12]}-candidate.json")


@router.get("/gate-runs/{run_id}/report")
def get_gate_report(run_id: str):
    path = _run_dir(run_id) / "gate-report.json"
    if not path.exists():
        raise HTTPException(404, "门禁报告不存在")
    return FileResponse(path, media_type="application/json", filename=f"{run_id[:12]}-gate.json")


@router.get("/gate-runs/{run_id}/snapshot-manifest")
def get_gate_snapshot_manifest(run_id: str):
    path = _run_dir(run_id) / "snapshot-manifest.json"
    if not path.exists():
        raise HTTPException(404, "快照清单不存在")
    return FileResponse(path, media_type="application/json", filename=f"{run_id[:12]}-manifest.json")


@router.get("/drafts/{draft_id}/snapshot-manifest")
def get_draft_snapshot_manifest(draft_id: str, request: Request):
    row = db.get_draft(request.app.state.db, draft_id)
    if row is None:
        raise HTTPException(404, "草稿不存在")
    family_id = row["family_id"]
    path = _roots()[2] / family_id / f"draft-{draft_id}-manifest.json"
    if not path.exists():
        raise HTTPException(404, "草稿快照清单不存在")
    return FileResponse(path, media_type="application/json", filename=f"{draft_id[:12]}-manifest.json")


# ---------------------------------------------------------------------------
# API: transactional publish / withdraw / restore / rollback
# ---------------------------------------------------------------------------

def _audit_conflict(conn, family_id: str, action: str, expected: int, observed: int,
                    **extra: Any) -> None:
    _write_audit(
        conn,
        family_id=family_id,
        action=action,
        expected_version=expected,
        observed_version=observed,
        result="denied",
        reason=f"期望当前版本 v{expected}，实际为 v{observed}",
        **extra,
    )


@router.post("/drafts/{draft_id}/publish")
def publish_draft(draft_id: str, request: Request, payload: ExpectedVersion):
    conn = request.app.state.db
    with _release_lock:
        draft_row = db.get_draft(conn, draft_id)
        if draft_row is None:
            raise HTTPException(404, "草稿不存在")
        draft = db.row_to_dict(draft_row)
        family_id = draft["family_id"]
        observed = _current_number(conn, family_id)
        if observed != payload.expected_version:
            _audit_conflict(conn, family_id, "publish", payload.expected_version, observed,
                            draft_id=draft_id, profile_sha256=draft["candidate_sha256"])
            raise HTTPException(409, f"版本已变化：期望 v{payload.expected_version}，实际 v{observed}")

        run_row = db.get_gate_run(conn, draft["latest_run_id"]) if draft["latest_run_id"] else None
        if run_row is None:
            _write_audit(conn, family_id=family_id, action="publish",
                         expected_version=payload.expected_version, observed_version=observed,
                         draft_id=draft_id, profile_sha256=draft["candidate_sha256"],
                         result="denied", reason="草稿尚未运行回归门禁")
            raise HTTPException(409, "请先运行回归门禁")
        run = db.row_to_dict(run_row)
        if not run["passed"]:
            _write_audit(conn, family_id=family_id, action="publish",
                         expected_version=payload.expected_version, observed_version=observed,
                         draft_id=draft_id, gate_run_id=run["run_id"],
                         profile_sha256=draft["candidate_sha256"],
                         result="denied", reason="门禁存在失败项，草稿保留未发布")
            raise HTTPException(409, "门禁未全部通过，保留草稿")
        if run["candidate_sha256"] != draft["candidate_sha256"]:
            _write_audit(
                conn,
                family_id=family_id,
                action="publish",
                expected_version=payload.expected_version,
                observed_version=observed,
                draft_id=draft_id,
                gate_run_id=run["run_id"],
                profile_sha256=draft["candidate_sha256"],
                result="denied",
                reason="门禁快照与草稿 SHA-256 不一致",
            )
            raise HTTPException(409, "门禁快照与草稿 SHA-256 不一致")

        snapshot_path = _roots()[2] / family_id / f"press-{draft['candidate_sha256'][:16]}.icc"
        if not snapshot_path.exists() or _sha256(snapshot_path) != draft["candidate_sha256"]:
            _write_audit(
                conn,
                family_id=family_id,
                action="publish",
                expected_version=payload.expected_version,
                observed_version=observed,
                draft_id=draft_id,
                gate_run_id=run["run_id"],
                profile_sha256=draft["candidate_sha256"],
                result="denied",
                reason="不可变候选快照缺失或摘要不匹配",
            )
            raise HTTPException(409, "不可变候选快照缺失或摘要不匹配")

        try:
            conn.execute("BEGIN IMMEDIATE")
            next_row = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number FROM release_versions WHERE family_id=?",
                (family_id,),
            ).fetchone()
            next_number = int(next_row["next_number"])
            version_id = "ver-" + uuid.uuid4().hex
            if observed:
                current = conn.execute(
                    "SELECT version_id FROM release_versions WHERE family_id=? AND version_number=? AND status='active'",
                    (family_id, observed),
                ).fetchone()
                if current is not None:
                    conn.execute(
                        "UPDATE release_versions SET status='superseded' WHERE version_id=?",
                        (current["version_id"],),
                    )
            db.insert_version(conn, {
                "version_id": version_id,
                "family_id": family_id,
                "version_number": next_number,
                "draft_id": draft_id,
                "gate_run_id": run["run_id"],
                "profile_sha256": draft["candidate_sha256"],
                "snapshot_path": str(snapshot_path),
                "profile_name": draft["candidate_name"],
                "status": "active",
            })
            conn.execute(
                "UPDATE release_families SET current_version=?, updated_at=datetime('now') WHERE family_id=?",
                (next_number, family_id),
            )
            db.update_draft_status(conn, draft_id, "published", run["run_id"])
            db.insert_audit(conn, {
                "audit_id": uuid.uuid4().hex,
                "family_id": family_id,
                "action": "publish",
                "expected_version": payload.expected_version,
                "observed_version": observed,
                "version_id": version_id,
                "version_number": next_number,
                "draft_id": draft_id,
                "gate_run_id": run["run_id"],
                "profile_sha256": draft["candidate_sha256"],
                "result": "allowed",
                "reason": "门禁全部通过，事务内发布为当前版本",
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return dict(db.get_version(conn, version_id))


@router.post("/versions/{version_id}/withdraw")
def withdraw_version(version_id: str, request: Request, payload: ExpectedVersion):
    conn = request.app.state.db
    with _release_lock:
        row = db.get_version(conn, version_id)
        if row is None:
            raise HTTPException(404, "版本不存在")
        family_id = row["family_id"]
        observed = _current_number(conn, family_id)
        if observed != payload.expected_version:
            _audit_conflict(conn, family_id, "withdraw", payload.expected_version, observed,
                            version_id=version_id, version_number=row["version_number"])
            raise HTTPException(409, f"版本已变化：期望 v{payload.expected_version}，实际 v{observed}")
        if row["status"] != "active" or observed != row["version_number"]:
            reason = "仅可撤回当前 active 版本"
            _write_audit(conn, family_id=family_id, action="withdraw",
                         expected_version=payload.expected_version, observed_version=observed,
                         version_id=version_id, version_number=row["version_number"],
                         profile_sha256=row["profile_sha256"], result="denied", reason=reason)
            raise HTTPException(409, reason)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE release_versions SET status='withdrawn', withdrawn_at=datetime('now') WHERE version_id=?", (version_id,))
            conn.execute("UPDATE release_families SET current_version=NULL, updated_at=datetime('now') WHERE family_id=?", (family_id,))
            db.insert_audit(conn, {
                "audit_id": uuid.uuid4().hex,
                "family_id": family_id,
                "action": "withdraw",
                "expected_version": payload.expected_version,
                "observed_version": observed,
                "version_id": version_id,
                "version_number": row["version_number"],
                "profile_sha256": row["profile_sha256"],
                "result": "allowed",
                "reason": "当前版本已撤回，配置族暂无当前版本",
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return dict(db.get_version(conn, version_id))


def _activate_existing(version_id: str, expected: int, required_status: str, action: str,
                       reason_text: str, conn: sqlite3.Connection) -> dict:
    row = db.get_version(conn, version_id)
    if row is None:
        raise HTTPException(404, "版本不存在")
    family_id = row["family_id"]
    observed = _current_number(conn, family_id)
    if observed != expected:
        _audit_conflict(conn, family_id, action, expected, observed,
                        version_id=version_id, version_number=row["version_number"])
        raise HTTPException(409, f"版本已变化：期望 v{expected}，实际 v{observed}")
    if row["status"] != required_status:
        reason = f"该操作要求版本状态为 {required_status}，当前为 {row['status']}"
        _write_audit(conn, family_id=family_id, action=action, expected_version=expected,
                     observed_version=observed, version_id=version_id,
                     version_number=row["version_number"], profile_sha256=row["profile_sha256"],
                     result="denied", reason=reason)
        raise HTTPException(409, reason)

    try:
        conn.execute("BEGIN IMMEDIATE")
        if observed:
            conn.execute(
                "UPDATE release_versions SET status='superseded' WHERE family_id=? AND version_number=? AND status='active'",
                (family_id, observed),
            )
        conn.execute("UPDATE release_versions SET status='active', withdrawn_at=NULL WHERE version_id=?", (version_id,))
        conn.execute(
            "UPDATE release_families SET current_version=?, updated_at=datetime('now') WHERE family_id=?",
            (row["version_number"], family_id),
        )
        db.insert_audit(conn, {
            "audit_id": uuid.uuid4().hex,
            "family_id": family_id,
            "action": action,
            "expected_version": expected,
            "observed_version": observed,
            "version_id": version_id,
            "version_number": row["version_number"],
            "profile_sha256": row["profile_sha256"],
            "result": "allowed",
            "reason": reason_text,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(db.get_version(conn, version_id))


@router.post("/versions/{version_id}/restore")
def restore_version(version_id: str, request: Request, payload: ExpectedVersion):
    with _release_lock:
        try:
            return _activate_existing(
                version_id, payload.expected_version, "withdrawn", "restore",
                "已恢复撤回版本为当前版本", request.app.state.db,
            )
        except ReleaseError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/versions/{version_id}/rollback")
def rollback_version(version_id: str, request: Request, payload: ExpectedVersion):
    with _release_lock:
        try:
            return _activate_existing(
                version_id, payload.expected_version, "superseded", "rollback",
                "已回滚到指定历史不可变快照", request.app.state.db,
            )
        except ReleaseError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc


def version_response(row: sqlite3.Row) -> dict:
    version = dict(row)
    version["gate_report"] = f"/api/releases/versions/{version['version_id']}/gate-report"
    version["snapshot"] = f"/api/releases/versions/{version['version_id']}/snapshot.icc"
    return version


@router.get("/versions/{version_id}")
def get_release_version(version_id: str, request: Request):
    row = db.get_version(request.app.state.db, version_id)
    if row is None:
        raise HTTPException(404, "版本不存在")
    return version_response(row)


@router.get("/versions/{version_id}/snapshot.icc")
def download_version_snapshot(version_id: str, request: Request):
    row = db.get_version(request.app.state.db, version_id)
    if row is None:
        raise HTTPException(404, "版本不存在")
    path = Path(row["snapshot_path"])
    if not path.exists() or _sha256(path) != row["profile_sha256"]:
        raise HTTPException(409, "版本快照缺失或 SHA-256 不匹配")
    return FileResponse(path, media_type="application/vnd.iccprofile",
                        filename=f"{row['family_id']}-v{row['version_number']}.icc")


@router.get("/versions/{version_id}/gate-report")
def download_version_gate_report(version_id: str, request: Request):
    row = db.get_version(request.app.state.db, version_id)
    if row is None:
        raise HTTPException(404, "版本不存在")
    path = _run_dir(row["gate_run_id"]) / "gate-report.json"
    if not path.exists():
        raise HTTPException(404, "版本绑定的门禁报告不存在")
    return FileResponse(path, media_type="application/json",
                        filename=f"{row['family_id']}-v{row['version_number']}-gate.json")


@router.get("/versions")
def list_versions(request: Request, family_id: str | None = None):
    return [version_response(r) for r in db.list_versions(request.app.state.db, family_id)]


@router.get("/audits")
def audits(request: Request, family_id: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 500))
    return [dict(r) for r in db.list_audits(request.app.state.db, family_id, limit)]
