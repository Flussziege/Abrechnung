#!/usr/bin/env python3
"""Batch-transpose selected MuseScore Studio 4.7.x staves and export PDFs.

Safety model:
- The input .mscz master files are never given to MuseScore directly.
- Every score is first copied into a TemporaryDirectory.
- MuseScore only reads/writes temporary copies.
- A SHA-256 checksum of every master is verified after processing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET

try:
    from pypdf import PdfReader
except ImportError:  # handled cleanly in main
    PdfReader = None  # type: ignore[assignment]


APP_NAME = "MuseScore Ensemble Transposer"
EXTENSION_URI = "musescore://extensions/ensemble-transposer"
REPORT_TAG = "ensembleTransposerReport"
SUPPORTED_MAJOR = 4
SUPPORTED_MINOR = 7
INVALID_WINDOWS_CHARS = re.compile(r'[\\/:*?"<>|]')
RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ToolError(RuntimeError):
    pass


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


@dataclass
class PdfInfo:
    filename: str
    kind: str
    part_name: str | None
    pages: int
    size_bytes: int
    status: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class PieceResult:
    source: Path
    report: dict[str, Any] | None = None
    pdfs: list[PdfInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    master_hash_before: str = ""
    master_hash_after: str = ""

    @property
    def status(self) -> str:
        if self.errors:
            return "FEHLER"
        if self.warnings or (self.report and self.report.get("warnings")):
            return "WARNUNG"
        return "OK"


class HumanLog:
    def __init__(self, path: Path):
        self.path = path
        self._fh = path.open("w", encoding="utf-8", newline="\n")

    def line(self, text: str = "") -> None:
        self._fh.write(text + "\n")
        self._fh.flush()

    def block(self, text: str) -> None:
        for line in text.splitlines():
            self.line(line)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "HumanLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_windows_component(name: str) -> str:
    value = INVALID_WINDOWS_CHARS.sub("_", name)
    value = "".join(ch if ord(ch) >= 32 else "_" for ch in value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    if not value:
        value = "Auszug"
    stem = value.split(".", 1)[0].upper()
    if stem in RESERVED_WINDOWS_NAMES:
        value = "_" + value
    # Keep ample room for directory + prefix + .pdf on legacy Windows paths.
    if len(value) > 120:
        value = value[:120].rstrip(". ")
    return value or "Auszug"


def unique_part_filename(base_stem: str, target: str, part_name: str, used: set[str]) -> str:
    safe = sanitize_windows_component(part_name)
    root = f"{base_stem}_{target}_{safe}"
    candidate = root + ".pdf"
    n = 2
    while candidate.casefold() in used:
        candidate = f"{root}_{n}.pdf"
        n += 1
    used.add(candidate.casefold())
    return candidate


def bytes_human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def quote_cmd(args: Iterable[str]) -> str:
    def q(s: str) -> str:
        return f'"{s}"' if any(c.isspace() for c in s) else s
    return " ".join(q(str(a)) for a in args)


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> CommandResult:
    start = time.monotonic()
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"MuseScore-Zeitüberschreitung nach {timeout} Sekunden: {quote_cmd(args)}") from exc
    except OSError as exc:
        raise ToolError(f"MuseScore konnte nicht gestartet werden: {exc}") from exc
    return CommandResult(args, cp.returncode, cp.stdout, cp.stderr, time.monotonic() - start)


def executable_candidates_from_registry() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore
    except ImportError:
        return []

    result: list[Path] = []
    roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    uninstall_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in roots:
        for upath in uninstall_paths:
            try:
                with winreg.OpenKey(root, upath) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for i in range(count):
                        try:
                            subname = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subname) as sub:
                                display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                                if "musescore" not in display.casefold():
                                    continue
                                for val_name in ("DisplayIcon", "InstallLocation"):
                                    try:
                                        raw = str(winreg.QueryValueEx(sub, val_name)[0]).strip('" ')
                                    except OSError:
                                        continue
                                    if not raw:
                                        continue
                                    p = Path(raw.split(",", 1)[0])
                                    if p.suffix.lower() == ".exe":
                                        result.append(p)
                                    elif p.is_dir():
                                        for name in ("MuseScore4.exe", "MuseScore.exe", "MuseScore Studio 4.exe"):
                                            result.append(p / "bin" / name)
                                            result.append(p / name)
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def find_musescore(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise ToolError(f"Der mit --musescore angegebene Pfad existiert nicht: {p}")
        return p

    candidates: list[Path] = []
    for name in ("MuseScore4.exe", "MuseScore.exe", "MuseScore Studio 4.exe", "mscore4.exe", "mscore", "musescore", "MuseScore4", "mscore4portable"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    candidates.extend(executable_candidates_from_registry())

    env_roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        os.environ.get("LOCALAPPDATA"),
    ]
    for root_s in env_roots:
        if not root_s:
            continue
        root = Path(root_s)
        direct_patterns = [
            "MuseScore*/bin/MuseScore4.exe",
            "MuseScore*/bin/MuseScore.exe",
            "MuseScore*/MuseScore4.exe",
            "Programs/MuseScore*/bin/MuseScore4.exe",
            "Programs/MuseScore*/MuseScore4.exe",
        ]
        for pattern in direct_patterns:
            candidates.extend(root.glob(pattern))

    seen: set[str] = set()
    for p in candidates:
        key = str(p).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            pass

    raise ToolError(
        "MuseScore Studio wurde nicht automatisch gefunden. "
        "Bitte den tatsächlichen EXE-Pfad mit --musescore angeben, z. B. "
        ' --musescore "C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe".'
    )


def detect_musescore_version(exe: Path, timeout: int) -> tuple[str, tuple[int, int, int]]:
    result = run_command([str(exe), "--long-version"], timeout=timeout)
    text = (result.stdout + "\n" + result.stderr).strip()
    match = re.search(r"(?<!\d)(4\.\d+(?:\.\d+)?)(?!\d)", text)
    if not match:
        result2 = run_command([str(exe), "--version"], timeout=timeout)
        text2 = (result2.stdout + "\n" + result2.stderr).strip()
        match = re.search(r"(?<!\d)(4\.\d+(?:\.\d+)?)(?!\d)", text2)
        text = text2 or text
    if not match:
        raise ToolError(f"MuseScore-Version konnte nicht ermittelt werden. Ausgabe:\n{text[:1200]}")
    version_str = match.group(1)
    nums = [int(x) for x in version_str.split(".")]
    while len(nums) < 3:
        nums.append(0)
    return version_str, (nums[0], nums[1], nums[2])


def ensure_supported_version(version: tuple[int, int, int], allow_untested: bool) -> None:
    if version[0] == SUPPORTED_MAJOR and version[1] == SUPPORTED_MINOR:
        return
    message = (
        f"Diese Implementierung ist gegen MuseScore Studio {SUPPORTED_MAJOR}.{SUPPORTED_MINOR}.x verifiziert, "
        f"installiert ist {version[0]}.{version[1]}.{version[2]}."
    )
    if allow_untested and version[0] == 4:
        print("WARNUNG: " + message + " Fortsetzung wegen --allow-untested-version.", file=sys.stderr)
        return
    raise ToolError(message + " Abbruch. Für bewusstes Testen: --allow-untested-version.")


def extension_install_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ToolError("LOCALAPPDATA ist nicht gesetzt; MuseScore-Extension-Ordner kann nicht bestimmt werden.")
    return Path(local) / "MuseScore" / "MuseScore4" / "extensions" / "ensemble_transposer"


def install_extension(project_dir: Path) -> Path:
    source = project_dir / "extension"
    manifest = source / "manifest.json"
    script = source / "main.js"
    if not manifest.is_file() or not script.is_file():
        raise ToolError(f"Extension-Dateien fehlen unter {source}")
    dest = extension_install_dir()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest, dest / "manifest.json")
    shutil.copy2(script, dest / "main.js")
    return dest


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def locate_main_mscx(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    container_name = "META-INF/container.xml"
    if container_name in names:
        try:
            root = ET.fromstring(zf.read(container_name))
            for elem in root.iter():
                if local_name(elem.tag) == "rootfile":
                    full_path = elem.attrib.get("full-path")
                    if full_path and full_path in names and full_path.lower().endswith(".mscx"):
                        return full_path
        except ET.ParseError:
            pass
    mscx = [n for n in names if n.lower().endswith(".mscx")]
    if not mscx:
        raise ToolError("MSCZ enthält keine MSCX-Datei.")
    preferred = [n for n in mscx if "excerpt" not in n.casefold() and not n.startswith("Excerpts/")]
    return sorted(preferred or mscx, key=lambda s: (s.count("/"), len(s), s.casefold()))[0]


def extract_report_from_mscz(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            mscx_name = locate_main_mscx(zf)
            root = ET.fromstring(zf.read(mscx_name))
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise ToolError(f"Temporäre MSCZ konnte nicht gelesen werden: {exc}") from exc

    for elem in root.iter():
        if local_name(elem.tag) == "metaTag" and elem.attrib.get("name") == REPORT_TAG:
            raw = elem.text or ""
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ToolError(f"MuseScore-Report ist ungültiges JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ToolError("MuseScore-Report hat unerwarteten Typ.")
            return value
    raise ToolError(
        "MuseScore-Extension hat keinen Report hinterlassen. "
        "Prüfe, ob die Extension erkannt wurde und ob MuseScore 4.7.x verwendet wird."
    )


def run_extension(
    exe: Path,
    input_mscz: Path,
    output_mscz: Path,
    action: str,
    *,
    cwd: Path,
    timeout: int,
) -> CommandResult:
    uri = f"{EXTENSION_URI}?action={action}"
    result = run_command(
        [str(exe), "-o", str(output_mscz), "--extension", uri, str(input_mscz)],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0 or not output_mscz.is_file() or output_mscz.stat().st_size == 0:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(
            f"MuseScore-Extension-Aktion '{action}' fehlgeschlagen (Code {result.returncode}).\n"
            f"{detail[-3000:]}"
        )
    return result


def prepare_temp_score(
    exe: Path,
    master: Path,
    target: str,
    temp_dir: Path,
    timeout: int,
) -> tuple[Path, dict[str, Any], CommandResult]:
    source_copy = temp_dir / "source_master_copy.mscz"
    output = temp_dir / f"prepared_{target}.mscz"
    shutil.copy2(master, source_copy)
    action = {"C": "c", "Bb": "bb", "Eb": "eb"}[target]
    cmd = run_extension(exe, source_copy, output, action, cwd=temp_dir, timeout=timeout)
    report = extract_report_from_mscz(output)
    if report.get("status") == "FEHLER":
        warnings = report.get("warnings") or []
        raise ToolError("MuseScore hat die Transposition zurückgerollt: " + " | ".join(map(str, warnings)))
    return output, report, cmd


def export_score_pdf(exe: Path, prepared: Path, output_pdf: Path, *, cwd: Path, timeout: int) -> CommandResult:
    result = run_command([str(exe), "-o", str(output_pdf), str(prepared)], cwd=cwd, timeout=timeout)
    if result.returncode != 0 or not output_pdf.is_file() or output_pdf.stat().st_size == 0:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(f"Partitur-PDF-Export fehlgeschlagen (Code {result.returncode}): {detail[-2500:]}")
    return result


def export_parts_payload(
    exe: Path,
    prepared: Path,
    payload_path: Path,
    *,
    cwd: Path,
    timeout: int,
) -> tuple[dict[str, Any], CommandResult]:
    """Ask MuseScore 4.7 to render score/part PDFs into a JSON payload.

    MuseScore's backend may include potential (not-yet-saved) excerpts in this
    payload.  We never export those.  ``export_existing_parts`` below filters
    the payload against the exact stored excerpt titles reported by the
    extension before any PDF is written to the user's output directory.
    """
    result = run_command(
        [str(exe), "-o", str(payload_path), "--score-parts-pdf", str(prepared)],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0 or not payload_path.is_file() or payload_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(
            f"MuseScore-Part-Backend-Export fehlgeschlagen (Code {result.returncode}): {detail[-2500:]}"
        )
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"MuseScore-Part-Backend lieferte ungültiges JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolError("MuseScore-Part-Backend lieferte keinen JSON-Objekt-Report.")
    return payload, result


def _select_stored_part_bins(
    payload: dict[str, Any], stored_titles: list[str]
) -> list[tuple[str, str]]:
    """Return ``(stored title, base64 PDF)`` pairs for stored excerpts only.

    The MuseScore 4.7 backend returns existing excerpts first and can append
    potential excerpts afterwards.  Matching is therefore stable by title and
    occurrence: for duplicate titles we consume the earliest unused matching
    payload entry.  This keeps the existing excerpt version and discards any
    extra potential excerpts.
    """
    names = payload.get("parts")
    bins = payload.get("partsBin")
    if not isinstance(names, list) or not isinstance(bins, list) or len(names) != len(bins):
        raise ToolError("MuseScore-Part-Backend enthält keine konsistenten Felder 'parts'/'partsBin'.")

    normalized_names = [str(x) for x in names]
    used_indices: set[int] = set()
    selected: list[tuple[str, str]] = []

    for title in stored_titles:
        found = None
        for i, backend_title in enumerate(normalized_names):
            if i in used_indices:
                continue
            if backend_title == title:
                found = i
                break
        if found is None:
            raise ToolError(
                "Ein gespeicherter Auszug fehlt in MuseScores Part-Backend-Ausgabe: " + title
            )
        encoded = bins[found]
        if not isinstance(encoded, str) or not encoded:
            raise ToolError(f"MuseScore lieferte für den Auszug '{title}' keine PDF-Daten.")
        used_indices.add(found)
        selected.append((title, encoded))

    return selected


def export_existing_parts(
    exe: Path,
    prepared: Path,
    stored_excerpts: list[dict[str, Any]],
    source_stem: str,
    target: str,
    output_dir: Path,
    temp_dir: Path,
    timeout: int,
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Export exactly the already-stored linked excerpts.

    No excerpt is renamed, created or saved.  The temporary MuseScore backend
    payload can contain additional potential excerpts; those are discarded.
    """
    warnings: list[str] = []
    stored_titles = [str(ex.get("title") or "") for ex in stored_excerpts]
    if any(not title for title in stored_titles):
        raise ToolError(
            "Mindestens ein vorhandener Auszug besitzt keinen eindeutigen Titel. "
            "Bitte den Auszug in MuseScore benennen; sonst ist ein sicherer automatischer Dateiname nicht möglich."
        )

    payload_path = temp_dir / "parts_payload.json"
    payload, _ = export_parts_payload(exe, prepared, payload_path, cwd=temp_dir, timeout=timeout)
    selected = _select_stored_part_bins(payload, stored_titles)

    payload_names = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    if len(payload_names) > len(stored_titles):
        warnings.append(
            f"MuseScore stellte zusätzlich {len(payload_names) - len(stored_titles)} potentielle/nicht gespeicherte "
            "Auszüge bereit; sie wurden bewusst verworfen."
        )

    used: set[str] = set()
    exported: list[tuple[Path, str]] = []
    for title, encoded in selected:
        try:
            pdf_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ToolError(f"PDF-Daten des Auszugs '{title}' sind kein gültiges Base64: {exc}") from exc
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ToolError(f"MuseScore lieferte für den Auszug '{title}' keine gültigen PDF-Daten.")

        final_name = unique_part_filename(source_stem, target, title, used)
        final_path = output_dir / final_name
        try:
            final_path.write_bytes(pdf_bytes)
        except OSError as exc:
            raise ToolError(f"Auszug-PDF kann nicht geschrieben werden ({final_path.name}): {exc}") from exc
        exported.append((final_path, title))

    return exported, warnings

def validate_pdf(path: Path, kind: str, part_name: str | None = None) -> PdfInfo:
    if PdfReader is None:
        raise ToolError("Python-Paket 'pypdf' fehlt. Bitte 'pip install -r requirements.txt' ausführen.")
    if not path.is_file():
        raise ToolError(f"PDF fehlt: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise ToolError(f"PDF ist leer: {path.name}")
    with path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise ToolError(f"Datei hat keinen PDF-Header: {path.name}")
    try:
        reader = PdfReader(str(path), strict=False)
        pages = len(reader.pages)
    except Exception as exc:
        raise ToolError(f"PDF lässt sich nicht öffnen: {path.name}: {exc}") from exc
    if pages <= 0:
        raise ToolError(f"PDF besitzt 0 Seiten: {path.name}")
    warnings: list[str] = []
    if size < 2048:
        warnings.append("PDF ist ungewöhnlich klein (< 2 KB).")
    return PdfInfo(path.name, kind, part_name, pages, size, "OK", warnings)


def create_output_dir(input_dir: Path, target: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = input_dir / f"Transponiert_{target}_{stamp}"
    if not base.exists():
        base.mkdir()
        return base
    i = 2
    while True:
        candidate = input_dir / f"Transponiert_{target}_{stamp}_{i:02d}"
        if not candidate.exists():
            candidate.mkdir()
            return candidate
        i += 1


def collect_mscz_files(folder: Path) -> list[Path]:
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == ".mscz"]
    except OSError as exc:
        raise ToolError(f"Eingabeordner kann nicht gelesen werden: {exc}") from exc
    return sorted(files, key=lambda p: p.name.casefold())


def planned_filenames(source: Path, target: str, export_mode: str, report: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if export_mode in ("score", "all"):
        result.append(f"{source.stem}_{target}_Partitur.pdf")
    if export_mode in ("parts", "all"):
        used: set[str] = set()
        for ex in report.get("excerpts") or []:
            if isinstance(ex, dict):
                result.append(unique_part_filename(source.stem, target, str(ex.get("title") or "Auszug"), used))
    return result


def report_warnings(report: dict[str, Any]) -> list[str]:
    raw = report.get("warnings") or []
    return [str(x) for x in raw]


def log_report(log: HumanLog, report: dict[str, Any], target: str, export_mode: str, dry_run: bool) -> None:
    log.line(f"TARGET: {target}")
    log.line(f"STAVES: {report.get('nstaves', '?')}")
    for st in report.get("staves") or []:
        if not isinstance(st, dict):
            continue
        voices = ",".join(map(str, st.get("voicesUsed") or [])) or "-"
        log.line(
            f"  {st.get('index')} | {st.get('name') or '-'} | {st.get('instrument') or '-'} | "
            f"{st.get('clef') or '?'} | Voices {voices} | {st.get('decision') or '?'}"
        )
        if st.get("reason"):
            log.line(f"      Grund: {st.get('reason')}")
    excerpts = report.get("excerpts") or []
    log.line("VORHANDENE AUSZÜGE:")
    if not excerpts:
        log.line("  (keine)")
    for ex in excerpts:
        if not isinstance(ex, dict):
            continue
        log.line(f"  - {ex.get('title') or '(ohne Namen)'}")
        for es in ex.get("staffs") or []:
            if isinstance(es, dict):
                msi = es.get("masterStaffIndex")
                mapping = f"Master-Staff {msi}" if isinstance(msi, int) and msi > 0 else "Master-Zuordnung unbekannt"
                vv = ",".join(map(str, es.get("visibleVoices") or [])) or "-"
                log.line(f"      {mapping} | {es.get('name') or '-'} | Voices {vv}")
    trans = report.get("transposition")
    if isinstance(trans, dict):
        log.line(
            "TRANSPOSITION: "
            f"{trans.get('semitones', 0):+} Halbtöne, "
            f"{trans.get('diatonicSteps', 0):+} diatonische Schritte, "
            f"Tonart-Delta {trans.get('keyFifthsDelta', 0):+} Quintenschritte"
        )
        log.line(f"  geänderte Staves: {trans.get('changedStaffs') or []}")
        log.line(f"  übersprungene Staves: {trans.get('skippedStaffs') or []}")
        log.line(f"  geänderte Noten: {trans.get('changedNotes', 0)}")
        log.line(f"  geänderte vorhandene Tonartvorzeichnungen: {trans.get('changedKeySignatures', 0)}")
        log.line(f"  neu eingefügte Anfangstonarten: {trans.get('insertedInitialKeySignatures', 0)}")
    log.line(f"EXPORTMODUS: {export_mode.upper()}")
    log.line(f"DRY-RUN: {'JA' if dry_run else 'NEIN'}")
    for w in report_warnings(report):
        log.line("WARNUNG: " + w)


def print_dry_run(source: Path, target: str, export_mode: str, report: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print(source.name)
    print(f"TARGET: {target}")
    print("\nSTAVES:")
    for st in report.get("staves") or []:
        if isinstance(st, dict):
            voices = ",".join(map(str, st.get("voicesUsed") or [])) or "-"
            print(
                f"{str(st.get('index')).rjust(2)} | {(st.get('name') or '-'):24.24} | "
                f"{(st.get('clef') or '?'):22.22} | Voices {voices:7} | {st.get('decision') or '?'}"
            )
    print("\nGEFUNDENE AUSZÜGE:")
    excerpts = report.get("excerpts") or []
    if excerpts:
        for ex in excerpts:
            if isinstance(ex, dict):
                print(f"- {ex.get('title') or '(ohne Namen)'}")
    else:
        print("- keine")
    print(f"\nEXPORTMODUS: {export_mode.upper()}")
    print("\nGEPLANTE AUSGABE:")
    planned = planned_filenames(source, target, export_mode, report)
    if planned:
        for name in planned:
            print(f"- {name}")
    else:
        print("- keine PDF-Dateien")
    warnings = report_warnings(report)
    if warnings:
        print("\nWARNUNGEN:")
        for w in warnings:
            print("- " + w)
    print(f"\nSTATUS: {report.get('status', 'OK')}")


def process_piece(
    source: Path,
    *,
    exe: Path,
    target: str,
    export_mode: str,
    dry_run: bool,
    output_dir: Path,
    timeout: int,
    log: HumanLog,
) -> PieceResult:
    result = PieceResult(source=source)
    result.master_hash_before = sha256_file(source)
    log.line("\n" + "-" * 72)
    log.line(source.name)
    log.line("-" * 72)
    log.line(f"Master SHA-256 vor Verarbeitung: {result.master_hash_before}")

    try:
        with tempfile.TemporaryDirectory(prefix="musescore_transposer_") as td:
            temp_dir = Path(td)
            prepared, report, cmd = prepare_temp_score(exe, source, target, temp_dir, timeout)
            result.report = report
            log.line(f"MuseScore-Vorbereitung: {cmd.elapsed:.2f} s")
            log_report(log, report, target, export_mode, dry_run)

            if dry_run:
                print_dry_run(source, target, export_mode, report)
                return result

            trans = report.get("transposition") or {}
            if target != "C" and not (isinstance(trans, dict) and trans.get("changedStaffs")):
                raise ToolError(
                    "Kein Staff wurde tatsächlich transponiert. Aus Sicherheitsgründen werden keine "
                    f"mit '{target}' beschrifteten PDFs erzeugt. Zuerst Dry-Run/Warnungen prüfen."
                )

            excerpts = report.get("excerpts") or []

            if export_mode in ("score", "all"):
                score_pdf = output_dir / f"{source.stem}_{target}_Partitur.pdf"
                export_score_pdf(exe, prepared, score_pdf, cwd=temp_dir, timeout=timeout)
                info = validate_pdf(score_pdf, "Partitur")
                result.pdfs.append(info)
                log.line(f"PDF Partitur: {info.filename} | Seiten: {info.pages} | Größe: {bytes_human(info.size_bytes)} | {info.status}")
                for w in info.warnings:
                    result.warnings.append(f"{info.filename}: {w}")

            if export_mode in ("parts", "all"):
                if not excerpts:
                    result.warnings.append("Keine vorhandenen Parts/Auszüge gefunden; Part-Export übersprungen.")
                    log.line("WARNUNG: Keine vorhandenen Parts/Auszüge gefunden; Part-Export übersprungen.")
                else:
                    stored_excerpts = [ex for ex in excerpts if isinstance(ex, dict)]
                    exported, part_warnings = export_existing_parts(
                        exe, prepared, stored_excerpts, source.stem, target, output_dir, temp_dir, timeout
                    )
                    result.warnings.extend(part_warnings)
                    for pdf_path, part_name in exported:
                        info = validate_pdf(pdf_path, "Auszug", part_name)
                        result.pdfs.append(info)
                        log.line(
                            f"PDF Auszug: {info.filename} | Auszug: {part_name} | Seiten: {info.pages} | "
                            f"Größe: {bytes_human(info.size_bytes)} | {info.status}"
                        )
                        for w in info.warnings:
                            result.warnings.append(f"{info.filename}: {w}")

    except Exception as exc:
        result.errors.append(str(exc))
        log.line("FEHLER: " + str(exc).replace("\n", " | "))
    finally:
        try:
            result.master_hash_after = sha256_file(source)
            log.line(f"Master SHA-256 nach Verarbeitung: {result.master_hash_after}")
            if result.master_hash_before and result.master_hash_after != result.master_hash_before:
                critical = "KRITISCH: Masterdatei-Hash hat sich während des Durchlaufs verändert."
                result.errors.append(critical)
                log.line(critical)
            else:
                log.line("Masterdatei unverändert: JA")
        except Exception as exc:
            result.errors.append(f"Master-Integritätskontrolle fehlgeschlagen: {exc}")
            log.line("FEHLER bei Master-Integritätskontrolle: " + str(exc))

    return result



# ---------------------------------------------------------------------------
# Hosted/web API helpers (dual clef targets)
# ---------------------------------------------------------------------------

def target_combo_label(treble_target: str, bass_target: str) -> str:
    valid = {"C", "Bb", "Eb"}
    if treble_target not in valid or bass_target not in valid:
        raise ToolError("Ungültiges Transpositionsziel. Erlaubt: C, Bb, Eb.")
    if bass_target == "C":
        return treble_target
    if treble_target == bass_target:
        return treble_target
    return f"G-{treble_target}_F-{bass_target}"


def action_for_targets(treble_target: str, bass_target: str) -> str:
    mapping = {
        ("C", "C"): "c",
        ("Bb", "C"): "bb",
        ("Eb", "C"): "eb",
        ("C", "Bb"): "c_bb",
        ("C", "Eb"): "c_eb",
        ("Bb", "Bb"): "bb_bb",
        ("Bb", "Eb"): "bb_eb",
        ("Eb", "Bb"): "eb_bb",
        ("Eb", "Eb"): "eb_eb",
    }
    try:
        return mapping[(treble_target, bass_target)]
    except KeyError as exc:
        raise ToolError("Ungültige Kombination für Violin-/Bassschlüssel.") from exc


def prepare_temp_score_dual(
    exe: Path,
    master: Path,
    treble_target: str,
    bass_target: str,
    temp_dir: Path,
    timeout: int,
) -> tuple[Path, dict[str, Any], CommandResult]:
    source_copy = temp_dir / "source_master_copy.mscz"
    label = target_combo_label(treble_target, bass_target)
    output = temp_dir / f"prepared_{label}.mscz"
    shutil.copy2(master, source_copy)
    cmd = run_extension(
        exe, source_copy, output, action_for_targets(treble_target, bass_target),
        cwd=temp_dir, timeout=timeout
    )
    report = extract_report_from_mscz(output)
    if report.get("status") == "FEHLER":
        warnings = report.get("warnings") or []
        raise ToolError("MuseScore hat die Transposition zurückgerollt: " + " | ".join(map(str, warnings)))
    return output, report, cmd


def process_piece_dual(
    source: Path,
    *,
    exe: Path,
    treble_target: str,
    bass_target: str,
    export_score: bool,
    export_parts: bool,
    dry_run: bool,
    output_dir: Path,
    timeout: int = 300,
) -> PieceResult:
    """Hosted-worker entrypoint supporting independent G/F-clef targets."""
    result = PieceResult(source=source)
    result.master_hash_before = sha256_file(source)
    label = target_combo_label(treble_target, bass_target)
    try:
        with tempfile.TemporaryDirectory(prefix="musescore_transposer_web_") as td:
            temp_dir = Path(td)
            prepared, report, _ = prepare_temp_score_dual(
                exe, source, treble_target, bass_target, temp_dir, timeout
            )
            result.report = report
            if dry_run:
                return result

            changed = (report.get("transposition") or {}).get("changedStaffs") or []
            if (treble_target != "C" or bass_target != "C") and not changed:
                raise ToolError(
                    "Kein Staff wurde tatsächlich transponiert. Es werden keine falsch beschrifteten PDFs erzeugt."
                )

            excerpts = report.get("excerpts") or []
            if export_score:
                score_pdf = output_dir / f"{sanitize_windows_component(source.stem)}_{label}_Partitur.pdf"
                export_score_pdf(exe, prepared, score_pdf, cwd=temp_dir, timeout=timeout)
                result.pdfs.append(validate_pdf(score_pdf, "Partitur"))

            if export_parts:
                if not excerpts:
                    result.warnings.append("Keine vorhandenen Parts/Auszüge gefunden; Part-Export übersprungen.")
                else:
                    stored = [x for x in excerpts if isinstance(x, dict)]
                    exported, warnings = export_existing_parts(
                        exe, prepared, stored, sanitize_windows_component(source.stem),
                        label, output_dir, temp_dir, timeout
                    )
                    result.warnings.extend(warnings)
                    for pdf_path, part_name in exported:
                        result.pdfs.append(validate_pdf(pdf_path, "Auszug", part_name))
    except Exception as exc:
        result.errors.append(str(exc))
    finally:
        try:
            result.master_hash_after = sha256_file(source)
            if result.master_hash_before != result.master_hash_after:
                result.errors.append("KRITISCH: Masterdatei-Hash hat sich während der Verarbeitung verändert.")
        except Exception as exc:
            result.errors.append(f"Master-Integritätskontrolle fehlgeschlagen: {exc}")
    return result

def ask_choice(prompt: str, mapping: dict[str, str]) -> str:
    while True:
        print(prompt)
        for key, value in mapping.items():
            print(f"  {key} = {value}")
        answer = input("Auswahl: ").strip()
        if answer in mapping:
            return mapping[answer]
        print("Ungültige Auswahl.\n")


def resolve_interactive(args: argparse.Namespace) -> argparse.Namespace:
    prompted = False
    if args.folder is None:
        prompted = True
        args.folder = input("Ordner mit .mscz-Dateien: ").strip().strip('"')
    if args.target is None:
        prompted = True
        args.target = ask_choice("Zieltransposition:", {"1": "C", "2": "Bb", "3": "Eb"})
    if args.export_mode is None:
        prompted = True
        args.export_mode = ask_choice(
            "Was soll exportiert werden?",
            {"1": "parts", "2": "score", "3": "all"},
        )
    if args.dry_run is None:
        if prompted:
            ans = input("Dry-Run durchführen? [J/N]: ").strip().casefold()
            args.dry_run = ans in ("j", "ja", "y", "yes")
        else:
            args.dry_run = False
    return args


def self_test() -> int:
    assert sanitize_windows_component('A/B:C*D?E"F<G>H|I') == "A_B_C_D_E_F_G_H_I"
    assert sanitize_windows_component("CON") == "_CON"
    assert sanitize_windows_component(" ... ") == "Auszug"
    used: set[str] = set()
    a = unique_part_filename("Song", "Bb", "S+A", used)
    b = unique_part_filename("Song", "Bb", "S+A", used)
    assert a == "Song_Bb_S+A.pdf"
    assert b == "Song_Bb_S+A_2.pdf"

    # Synthetic MSCZ report extraction test.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.mscz"
        report = {"schema": 1, "status": "OK", "nstaves": 2}
        xml = (
            '<museScore version="4.70"><Score>'
            f'<metaTag name="{REPORT_TAG}">{json.dumps(report).replace("&", "&amp;").replace("<", "&lt;")}</metaTag>'
            '</Score></museScore>'
        ).encode("utf-8")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("score.mscx", xml)
        assert extract_report_from_mscz(p)["nstaves"] == 2

    # Stored excerpts must win over later potential excerpts with the same title.
    payload = {
        "parts": ["Flöte", "Klarinette", "Flöte"],
        "partsBin": ["AAA=", "QkJC", "Q0ND"],
    }
    selected = _select_stored_part_bins(payload, ["Flöte", "Klarinette"])
    assert selected == [("Flöte", "AAA="), ("Klarinette", "QkJC")]

    print("Self-Test: OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transponiert sichere Violinschlüssel-Staves in MuseScore-Studio-4.7-Masterkopien und exportiert PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            r"""
            Beispiele:
              python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all --dry-run
              python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export parts
              python transpose_scores.py "C:\Noten\Ensemble" --target Eb --export all
              python transpose_scores.py "C:\Noten\Ensemble" --target C --export score
            """
        ),
    )
    parser.add_argument("folder", nargs="?", help="Ordner mit den .mscz-Masterdateien (nicht rekursiv)")
    parser.add_argument("--target", choices=("C", "Bb", "Eb"), help="Zielinstrument/Transpositionspreset")
    parser.add_argument("--export", dest="export_mode", choices=("parts", "score", "all"), help="PDF-Exportmodus")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="Nur vollständige Temp-Probe/Analyse, keine PDFs")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Explizit echter Export")
    parser.add_argument("--musescore", help="Pfad zur installierten MuseScore-Studio-Executable")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout je MuseScore-Aufruf in Sekunden (Standard: 300)")
    parser.add_argument("--no-install-extension", action="store_true", help="Extension nicht automatisch in LOCALAPPDATA kopieren")
    parser.add_argument("--allow-untested-version", action="store_true", help="Andere MuseScore-4.x-Version trotz fehlender Verifikation testen")
    parser.add_argument("--self-test", action="store_true", help="Interne Python-Hilfsfunktionen testen und beenden")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    args = resolve_interactive(args)

    if PdfReader is None and not args.dry_run:
        raise ToolError("Python-Paket 'pypdf' fehlt. Bitte zuerst: pip install -r requirements.txt")

    folder = Path(str(args.folder)).expanduser().resolve()
    if not folder.is_dir():
        raise ToolError(f"Eingabeordner existiert nicht: {folder}")
    files = collect_mscz_files(folder)
    if not files:
        raise ToolError(f"Keine .mscz-Dateien direkt in {folder} gefunden.")

    exe = find_musescore(args.musescore)
    version_str, version_tuple = detect_musescore_version(exe, args.timeout)
    ensure_supported_version(version_tuple, args.allow_untested_version)

    project_dir = Path(__file__).resolve().parent
    if args.no_install_extension:
        ext_dest = extension_install_dir()
        if not (ext_dest / "manifest.json").is_file() or not (ext_dest / "main.js").is_file():
            raise ToolError(
                f"--no-install-extension wurde gewählt, aber die Extension fehlt unter {ext_dest}."
            )
    else:
        ext_dest = install_extension(project_dir)

    output_dir = create_output_dir(folder, args.target)
    log_path = output_dir / "transposition_log.txt"
    started = datetime.now()

    print(f"{APP_NAME}")
    print(f"MuseScore: {version_str}")
    print(f"Gefundene Stücke: {len(files)}")
    print(f"Ausgabeordner: {output_dir}")
    if args.dry_run:
        print("Modus: DRY-RUN (keine PDFs; alle MuseScore-Arbeitsdateien nur temporär)")

    results: list[PieceResult] = []
    with HumanLog(log_path) as log:
        log.line(APP_NAME)
        log.line("=" * 72)
        log.line(f"Datum: {started:%Y-%m-%d}")
        log.line(f"Startzeit: {started:%H:%M:%S}")
        log.line(f"Python-Version: {sys.version.split()[0]}")
        log.line(f"MuseScore-Version: {version_str}")
        log.line(f"MuseScore-Executable: {exe}")
        log.line(f"Extension: {ext_dest}")
        log.line(f"Eingabeordner: {folder}")
        log.line(f"Ausgabeordner: {output_dir}")
        log.line(f"Target: {args.target}")
        interval_text = {"C": "keine Transposition", "Bb": "große Sekunde aufwärts (+2 Halbtöne)", "Eb": "große Sexte aufwärts (+9 Halbtöne)"}[args.target]
        log.line(f"Musikalisches Intervall: {interval_text}")
        log.line(f"Exportmodus: {args.export_mode.upper()}")
        log.line(f"Dry-Run: {'JA' if args.dry_run else 'NEIN'}")
        log.line(f"Anzahl gefundener .mscz-Dateien: {len(files)}")
        log.line("Unterordner durchsucht: NEIN")
        log.line("Master-Sicherheitsmodell: MuseScore erhält ausschließlich temporäre Kopien; SHA-256 wird vor/nach geprüft.")

        for idx, source in enumerate(files, 1):
            print(f"[{idx}/{len(files)}] {source.name}")
            res = process_piece(
                source,
                exe=exe,
                target=args.target,
                export_mode=args.export_mode,
                dry_run=bool(args.dry_run),
                output_dir=output_dir,
                timeout=args.timeout,
                log=log,
            )
            results.append(res)
            if res.errors:
                print("  FEHLER: " + res.errors[-1])
            elif res.status == "WARNUNG":
                print("  WARNUNG")
            elif not args.dry_run:
                print(f"  OK ({len(res.pdfs)} PDF)")

        ended = datetime.now()
        ok = sum(1 for r in results if r.status == "OK")
        warn = sum(1 for r in results if r.status == "WARNUNG")
        err = sum(1 for r in results if r.status == "FEHLER")
        pdf_count = sum(len(r.pdfs) for r in results)

        log.line("\n" + "=" * 72)
        log.line("ZUSAMMENFASSUNG")
        log.line(f"Endzeit: {ended:%H:%M:%S}")
        log.line(f"Gefundene Stücke: {len(results)}")
        log.line(f"Erfolgreich: {ok}")
        log.line(f"Mit Warnungen: {warn}")
        log.line(f"Fehler: {err}")
        log.line(f"Erzeugte PDFs: {pdf_count}")
        log.line(f"Zieltransposition: {args.target}")
        log.line(f"Exportmodus: {args.export_mode.upper()}")
        log.line(f"Ausgabeordner: {output_dir}")
        log.line(f"Log: {log_path.name}")

    ok = sum(1 for r in results if r.status == "OK")
    warn = sum(1 for r in results if r.status == "WARNUNG")
    err = sum(1 for r in results if r.status == "FEHLER")
    pdf_count = sum(len(r.pdfs) for r in results)
    print("\n" + "=" * 72)
    print("FERTIG")
    print(f"Gefundene Stücke: {len(results)}")
    print(f"Erfolgreich: {ok}")
    print(f"Mit Warnungen: {warn}")
    print(f"Fehler: {err}")
    print(f"Erzeugte PDFs: {pdf_count}")
    print(f"Zieltransposition: {args.target}")
    print(f"Exportmodus: {args.export_mode.upper()}")
    print(f"Ausgabeordner: {output_dir}")
    print(f"Log: {log_path}")
    print("=" * 72)
    return 1 if err else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        raise SystemExit(130)
    except ToolError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise SystemExit(2)
