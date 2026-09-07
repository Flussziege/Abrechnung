#!/usr/bin/env python3
"""HTTP worker for the hosted Streamlit frontend.

The Streamlit app is intentionally separated from MuseScore.  This worker is
meant to run on a Linux/Windows host where MuseScore Studio 4.7.x is installed.
Uploaded masters are written only into a TemporaryDirectory and deleted after
building the response ZIP.
"""
from __future__ import annotations

import hmac
import io
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from transpose_scores import (
    ToolError,
    detect_musescore_version,
    ensure_supported_version,
    find_musescore,
    locate_main_mscx,
    process_piece_dual,
)

APP_DIR = Path(__file__).resolve().parent
MAX_SCORE_BYTES = int(os.environ.get("MAX_SCORE_BYTES", str(100 * 1024 * 1024)))
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_BYTES", str(500 * 1024 * 1024)))

app = FastAPI(title="MuseScore Ensemble Transposer Worker", version="1.1.0")


def _check_auth(authorization: str | None) -> None:
    expected = os.environ.get("WORKER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "WORKER_TOKEN ist auf dem Worker nicht konfiguriert.")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(401, "Worker-Token fehlt.")
    supplied = authorization[len(prefix):]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "Worker-Token ist ungültig.")


def _install_extension() -> Path:
    source = APP_DIR / "extension"
    if not (source / "manifest.json").is_file() or not (source / "main.js").is_file():
        raise ToolError("Extension-Dateien fehlen im Worker-Paket.")

    configured = os.environ.get("MUSESCORE_EXTENSION_DIR")
    if configured:
        dest = Path(configured).expanduser()
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise ToolError("LOCALAPPDATA fehlt; MUSESCORE_EXTENSION_DIR setzen.")
        dest = Path(local) / "MuseScore" / "MuseScore4" / "extensions" / "ensemble_transposer"
    else:
        # Override with MUSESCORE_EXTENSION_DIR if a distribution uses another path.
        dest = Path.home() / ".local" / "share" / "MuseScore" / "MuseScore4" / "extensions" / "ensemble_transposer"

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "manifest.json", dest / "manifest.json")
    shutil.copy2(source / "main.js", dest / "main.js")
    return dest


def _musescore() -> tuple[Path, str]:
    explicit = os.environ.get("MUSESCORE_EXE") or None
    exe = find_musescore(explicit)
    version_str, version = detect_musescore_version(exe, timeout=60)
    ensure_supported_version(version, allow_untested=False)
    return exe, version_str


def _validate_mscz(path: Path) -> None:
    if path.stat().st_size > MAX_SCORE_BYTES:
        raise ToolError(f"Datei ist größer als erlaubt ({MAX_SCORE_BYTES // (1024*1024)} MB).")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            locate_main_mscx(zf)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ToolError(f"Keine gültige MSCZ-Datei: {exc}") from exc


def _result_dict(piece, order: int, original_name: str) -> dict[str, Any]:
    return {
        "order": order,
        "source": original_name,
        "status": piece.status,
        "report": piece.report,
        "warnings": piece.warnings,
        "errors": piece.errors,
        "master_hash_before": piece.master_hash_before,
        "master_hash_after": piece.master_hash_after,
        "pdfs": [asdict(p) for p in piece.pdfs],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        _install_extension()
        exe, version = _musescore()
        return {"ok": True, "musescore": version, "executable": exe.name}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/process")
async def process_batch(
    config: str = Form(...),
    dry_run: bool = Form(False),
    files: list[UploadFile] = File(...),
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)
    try:
        jobs = json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Ungültige Job-Konfiguration: {exc}") from exc
    if not isinstance(jobs, list) or not jobs:
        raise HTTPException(400, "Job-Konfiguration ist leer.")

    _install_extension()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    exe, version = _musescore()

    with tempfile.TemporaryDirectory(prefix="ensemble_transposer_worker_") as td:
        root = Path(td)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        saved: dict[str, Path] = {}
        total_bytes = 0
        for i, upload in enumerate(files):
            original = Path(upload.filename or f"score_{i+1}.mscz").name
            data = await upload.read()
            total_bytes += len(data)
            if total_bytes > MAX_BATCH_BYTES:
                raise HTTPException(413, "Upload-Batch ist zu groß.")
            path = input_dir / original
            path.write_bytes(data)
            _validate_mscz(path)
            saved[original] = path

        results: list[dict[str, Any]] = []
        for job in sorted(jobs, key=lambda x: int(x.get("order", 999999))):
            if not job.get("selected", True):
                continue
            name = Path(str(job.get("filename", ""))).name
            source = saved.get(name)
            if source is None:
                results.append({"source": name, "status": "FEHLER", "errors": ["Upload-Datei fehlt."], "pdfs": []})
                continue
            piece = process_piece_dual(
                source,
                exe=exe,
                treble_target=str(job.get("treble_target", "C")),
                bass_target=str(job.get("bass_target", "C")),
                export_score=bool(job.get("export_score", True)),
                export_parts=bool(job.get("export_parts", True)),
                dry_run=dry_run,
                output_dir=output_dir,
                timeout=int(job.get("timeout", 300)),
            )
            results.append(_result_dict(piece, int(job.get("order", 0)), name))

        summary = {
            "schema": 1,
            "musescore_version": version,
            "dry_run": dry_run,
            "results": results,
        }
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("result.json", json.dumps(summary, ensure_ascii=False, indent=2))
            if not dry_run:
                for pdf in sorted(output_dir.glob("*.pdf"), key=lambda p: p.name.casefold()):
                    zf.write(pdf, arcname=f"pdf/{pdf.name}")
        payload.seek(0)

    headers = {"Content-Disposition": 'attachment; filename="musescore_transposed.zip"'}
    return StreamingResponse(payload, media_type="application/zip", headers=headers)
