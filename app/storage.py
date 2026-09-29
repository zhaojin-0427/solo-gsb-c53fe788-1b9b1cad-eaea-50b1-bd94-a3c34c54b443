"""Durable storage for deterministic soft-proofing pipeline results."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import pipeline as pl
from .profiles import ProfileManager


def persist_pipeline_result(
    *,
    result: pl.PipelineResult,
    job_id: str,
    image_data: bytes,
    source_profile: str,
    press_profile: str,
    display_profile: str,
    base: Path,
    pm: ProfileManager,
    srgb_bytes: bytes,
) -> dict:
    """Write arrays, images, source image and JSON report for one job.

    Pixel arrays retain float64 precision so later region analysis and
    regression gates examine exactly the values that decided the masks.
    """
    base.mkdir(parents=True, exist_ok=True)

    np.save(base / "lab_src.npy", result.lab_src.astype(np.float64))
    np.save(base / "lab_press.npy", result.lab_press.astype(np.float64))
    np.save(base / "cmyk.npy", result.cmyk.astype(np.uint8))
    np.save(base / "de.npy", result.de.astype(np.float64))
    np.save(base / "heatmap.npy", result.heatmap.astype(np.uint8))
    np.save(base / "mask3.npy", result.mask.astype(np.uint8))
    (base / "original.bin").write_bytes(image_data)

    display_icc_bytes = pm.load_bytes(display_profile)
    pngs = pl.export_pngs(result, srgb_bytes, display_icc_bytes)
    for name, blob in pngs.items():
        (base / name).write_bytes(blob)

    report = pl.build_report(result, job_id)
    (base / "report.json").write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2), "utf-8"
    )

    meta = {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "mode": result.mode,
        "image_sha256": result.image_sha,
        "embedded_sha256": result.embedded_sha,
        "source_profile": source_profile,
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": result.inputs.source_intent,
        "proof_intent": result.inputs.proof_intent,
        "threshold": result.inputs.threshold,
        "use_embedded": result.inputs.use_embedded,
    }
    (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")

    return {
        "job_id": job_id,
        "image_sha256": result.image_sha,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "image_mode": result.mode,
        "source_profile": source_profile if not result.inputs.use_embedded else "<embedded>",
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": result.inputs.source_intent,
        "proof_intent": result.inputs.proof_intent,
        "threshold": result.inputs.threshold,
        "use_embedded": result.inputs.use_embedded,
        "embedded_sha256": result.embedded_sha,
        "parameters": report["parameters"],
        "profile_digests": {k: v["sha256"] for k, v in result.profile_summaries.items()},
        "stats": result.stats,
    }


def response_from_result(result: pl.PipelineResult, job_id: str) -> dict:
    report = pl.build_report(result, job_id)
    return {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "stats": result.stats,
        "report": report,
        "images": {
            "preview": f"/api/jobs/{job_id}/images/preview.png",
            "softproof": f"/api/jobs/{job_id}/images/softproof.png",
            "heatmap": f"/api/jobs/{job_id}/images/heatmap.png",
            "overlay": f"/api/jobs/{job_id}/images/overlay.png",
            "mask": f"/api/jobs/{job_id}/images/mask.png",
        },
    }
