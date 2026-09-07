#!/usr/bin/env python3
"""Hosted Streamlit frontend for MuseScore Ensemble Transposer."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import PurePosixPath
from typing import Any

import pandas as pd
import requests
import streamlit as st

from transpose_scores import locate_main_mscx

MAX_SCORE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_SCORES = 100
TARGET_DISPLAY = {"keine / C": "C", "B♭": "Bb", "E♭": "Eb"}
TARGET_BACK = {v: k for k, v in TARGET_DISPLAY.items()}

st.set_page_config(page_title="MuseScore Ensemble Transposer", page_icon="🎼", layout="wide")


def secret(name: str, default: str = "") -> str:
    env = os.environ.get(name)
    if env:
        return env
    try:
        worker = st.secrets.get("worker", {})
        key = {"TRANSPOSER_WORKER_URL": "url", "TRANSPOSER_WORKER_TOKEN": "token"}.get(name, name)
        return str(worker.get(key, default))
    except Exception:
        return default


def valid_mscz(data: bytes) -> tuple[bool, str]:
    if not data:
        return False, "Datei ist leer"
    if len(data) > MAX_SCORE_BYTES:
        return False, "Datei ist größer als 100 MB"
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            locate_main_mscx(zf)
    except Exception as exc:
        return False, f"ungültige MSCZ: {exc}"
    return True, "OK"


def unique_name(name: str, used: set[str]) -> str:
    clean = PurePosixPath(name.replace("\\", "/")).name or "score.mscz"
    stem = clean[:-5] if clean.lower().endswith(".mscz") else clean
    candidate = f"{stem}.mscz"
    i = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({i}).mscz"
        i += 1
    used.add(candidate.casefold())
    return candidate


def scan_uploads(uploaded) -> tuple[list[dict[str, Any]], list[str]]:
    scores: list[dict[str, Any]] = []
    errors: list[str] = []
    used: set[str] = set()

    def add_score(name: str, data: bytes, origin: str) -> None:
        ok, msg = valid_mscz(data)
        if not ok:
            errors.append(f"{origin}: {name} – {msg}")
            return
        final_name = unique_name(name, used)
        scores.append({
            "filename": final_name,
            "display_name": final_name[:-5],
            "bytes": data,
            "sha256": hashlib.sha256(data).hexdigest(),
            "origin": origin,
        })

    for up in uploaded or []:
        raw = up.getvalue()
        name = up.name
        if name.lower().endswith(".mscz"):
            add_score(name, raw, "Upload")
            continue
        if not name.lower().endswith(".zip"):
            errors.append(f"{name}: Dateityp wird nicht unterstützt")
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(raw), "r") as outer:
                entries = [x for x in outer.infolist() if not x.is_dir() and x.filename.lower().endswith(".mscz")]
                if len(entries) > MAX_ARCHIVE_SCORES:
                    errors.append(f"{name}: mehr als {MAX_ARCHIVE_SCORES} MSCZ-Dateien im ZIP")
                    entries = entries[:MAX_ARCHIVE_SCORES]
                for info in entries:
                    if info.file_size > MAX_SCORE_BYTES:
                        errors.append(f"{name}: {info.filename} ist größer als 100 MB")
                        continue
                    add_score(info.filename, outer.read(info), f"ZIP {name}")
        except zipfile.BadZipFile:
            errors.append(f"{name}: kein gültiges ZIP")
    return scores, errors


def initial_rows(scores: list[dict[str, Any]], treble: str, bass: str, parts: bool) -> list[dict[str, Any]]:
    return [
        {
            "Auswahl": True,
            "Reihenfolge": i + 1,
            "Stück": score["display_name"],
            "Partitur": True,
            "Auszüge": parts,
            "Violinschlüssel": TARGET_BACK[treble],
            "Bassschlüssel": TARGET_BACK[bass],
            "_filename": score["filename"],
            "_sha256": score["sha256"],
        }
        for i, score in enumerate(scores)
    ]


def jobs_from_editor(df: pd.DataFrame, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup = {row["Stück"]: row for row in rows}
    jobs = []
    for _, row in df.iterrows():
        base = lookup.get(str(row["Stück"]))
        if not base:
            continue
        jobs.append({
            "filename": base["_filename"],
            "selected": bool(row["Auswahl"]),
            "order": int(row["Reihenfolge"]),
            "export_score": bool(row["Partitur"]),
            "export_parts": bool(row["Auszüge"]),
            "treble_target": TARGET_DISPLAY[str(row["Violinschlüssel"])],
            "bass_target": TARGET_DISPLAY[str(row["Bassschlüssel"])],
        })
    return jobs


def worker_request(scores: list[dict[str, Any]], jobs: list[dict[str, Any]], dry_run: bool) -> bytes:
    url = secret("TRANSPOSER_WORKER_URL").rstrip("/")
    token = secret("TRANSPOSER_WORKER_TOKEN")
    if not url:
        raise RuntimeError("Worker-URL ist noch nicht konfiguriert.")
    if not token:
        raise RuntimeError("Worker-Token ist noch nicht konfiguriert.")
    selected_names = {j["filename"] for j in jobs if j["selected"]}
    selected_scores = [s for s in scores if s["filename"] in selected_names]
    if not selected_scores:
        raise RuntimeError("Keine Stücke ausgewählt.")
    files = [
        ("files", (s["filename"], s["bytes"], "application/octet-stream"))
        for s in selected_scores
    ]
    response = requests.post(
        f"{url}/process",
        headers={"Authorization": f"Bearer {token}"},
        data={"config": json.dumps(jobs), "dry_run": "true" if dry_run else "false"},
        files=files,
        timeout=1800,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Worker-Fehler {response.status_code}: {response.text[:2000]}")
    return response.content


def parse_result_zip(data: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        return json.loads(zf.read("result.json"))


def show_results(summary: dict[str, Any]) -> None:
    st.subheader("Ergebnis")
    ms = summary.get("musescore_version", "?")
    st.caption(f"MuseScore Worker-Version: {ms}")
    rows = []
    for item in summary.get("results", []):
        report = item.get("report") or {}
        targets = report.get("targets") or {}
        rows.append({
            "Reihenfolge": item.get("order"),
            "Stück": item.get("source"),
            "Status": item.get("status"),
            "Violin": targets.get("treble", "?"),
            "Bass": targets.get("bass", "?"),
            "PDFs": len(item.get("pdfs") or []),
            "Warnungen": len(item.get("warnings") or []) + len(report.get("warnings") or []),
            "Fehler": len(item.get("errors") or []),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows).sort_values("Reihenfolge"), hide_index=True, use_container_width=True)

    for item in summary.get("results", []):
        report = item.get("report") or {}
        with st.expander(f"{item.get('source')} – {item.get('status')}"):
            if item.get("errors"):
                for msg in item["errors"]:
                    st.error(msg)
            warnings = list(item.get("warnings") or []) + list(report.get("warnings") or [])
            for msg in warnings:
                st.warning(msg)
            staff_rows = []
            for staff in report.get("staves") or []:
                if isinstance(staff, dict):
                    staff_rows.append({
                        "Staff": staff.get("index"),
                        "Name": staff.get("name"),
                        "Schlüssel": staff.get("clef"),
                        "Ziel": staff.get("target"),
                        "Entscheidung": staff.get("decision"),
                        "Grund": staff.get("reason", ""),
                    })
            if staff_rows:
                st.dataframe(pd.DataFrame(staff_rows), hide_index=True, use_container_width=True)
            excerpts = report.get("excerpts") or []
            if excerpts:
                st.write("Vorhandene Auszüge:", ", ".join(str(x.get("title") or "(ohne Titel)") for x in excerpts if isinstance(x, dict)))


st.title("🎼 MuseScore Ensemble Transposer")
st.caption("Gehostete Oberfläche – Upload → Auswahl → Reihenfolge → Transposition → PDF-ZIP")

with st.sidebar:
    st.header("Standardwerte")
    default_treble_display = st.selectbox("Violinschlüssel", list(TARGET_DISPLAY), index=1)
    default_bass_display = st.selectbox("Bassschlüssel", list(TARGET_DISPLAY), index=0)
    default_parts = st.checkbox("Auszüge standardmäßig exportieren", value=True)
    worker_url = secret("TRANSPOSER_WORKER_URL")
    st.divider()
    if worker_url:
        st.success("Worker konfiguriert")
    else:
        st.warning("Worker noch nicht konfiguriert")
    st.caption("Uploads und temporäre Dateien werden nicht als dauerhafte Bibliothek gespeichert.")

uploaded = st.file_uploader(
    "MuseScore-Dateien hochladen",
    type=["mscz", "zip"],
    accept_multiple_files=True,
    help="Du kannst einzelne .mscz-Dateien oder ein ZIP mit mehreren .mscz-Dateien hochladen.",
)

col_scan, col_clear = st.columns([1, 1])
with col_scan:
    scan_clicked = st.button("Dateien einlesen", type="primary", disabled=not uploaded)
with col_clear:
    if st.button("Auswahl zurücksetzen"):
        for key in ("scores", "rows", "last_zip", "last_summary"):
            st.session_state.pop(key, None)
        st.rerun()

if scan_clicked:
    scores, errors = scan_uploads(uploaded)
    st.session_state.scores = scores
    st.session_state.rows = initial_rows(
        scores,
        TARGET_DISPLAY[default_treble_display],
        TARGET_DISPLAY[default_bass_display],
        default_parts,
    )
    st.session_state.scan_errors = errors
    st.session_state.pop("last_zip", None)
    st.session_state.pop("last_summary", None)

for msg in st.session_state.get("scan_errors", []):
    st.warning(msg)

scores = st.session_state.get("scores", [])
rows = st.session_state.get("rows", [])

if scores and rows:
    st.subheader(f"{len(scores)} gültige Stücke")
    if st.button("Standardwerte auf alle Stücke anwenden"):
        for row in rows:
            row["Violinschlüssel"] = default_treble_display
            row["Bassschlüssel"] = default_bass_display
            row["Auszüge"] = default_parts
        st.session_state.rows = rows
        st.rerun()

    visible_df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    edited = st.data_editor(
        visible_df,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Auswahl": st.column_config.CheckboxColumn("✓", help="Stück verarbeiten"),
            "Reihenfolge": st.column_config.NumberColumn("Pos", min_value=1, step=1, required=True),
            "Stück": st.column_config.TextColumn("Stück", disabled=True),
            "Partitur": st.column_config.CheckboxColumn("Partitur"),
            "Auszüge": st.column_config.CheckboxColumn("Auszüge"),
            "Violinschlüssel": st.column_config.SelectboxColumn("Violin", options=list(TARGET_DISPLAY), required=True),
            "Bassschlüssel": st.column_config.SelectboxColumn("Bass", options=list(TARGET_DISPLAY), required=True),
        },
        key="song_editor",
    )

    jobs = jobs_from_editor(edited, rows)
    duplicate_positions = edited[edited["Auswahl"]].duplicated("Reihenfolge", keep=False).any()
    if duplicate_positions:
        st.warning("Mehrere ausgewählte Stücke haben dieselbe Positionsnummer. Der Worker sortiert dann stabil nach Upload-Reihenfolge.")

    selected_count = sum(1 for j in jobs if j["selected"])
    st.caption(f"Ausgewählt: {selected_count} Stück(e)")

    c1, c2 = st.columns(2)
    dry = c1.button("🔎 Dry-Run auf Worker", disabled=not selected_count or not worker_url)
    run = c2.button("🎵 PDFs erstellen", type="primary", disabled=not selected_count or not worker_url)

    if dry or run:
        try:
            with st.status("Worker verarbeitet die Stücke …", expanded=True) as status:
                st.write("Dateien werden nur für diesen Auftrag übertragen.")
                payload = worker_request(scores, jobs, dry_run=dry)
                summary = parse_result_zip(payload)
                st.session_state.last_zip = payload
                st.session_state.last_summary = summary
                status.update(label="Verarbeitung abgeschlossen", state="complete")
        except Exception as exc:
            st.error(str(exc))

if st.session_state.get("last_summary"):
    show_results(st.session_state.last_summary)
    if not st.session_state.last_summary.get("dry_run"):
        st.download_button(
            "⬇️ PDFs als ZIP herunterladen",
            data=st.session_state.last_zip,
            file_name="musescore_transponiert.zip",
            mime="application/zip",
            type="primary",
        )
