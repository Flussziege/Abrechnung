# Hosted Web-Version – Streamlit + MuseScore Worker

Diese Variante ist für eine **gehostete Streamlit-Webseite** gedacht. Sie ist
absichtlich zweigeteilt:

1. **Streamlit Community Cloud**: Upload, Stückauswahl, Reihenfolge,
   Partitur/Auszüge und Transpositionsziele.
2. **MuseScore Worker**: FastAPI-Dienst mit MuseScore Studio 4.7.x, der die
   temporären Scorekopien verarbeitet und ein ZIP mit PDFs zurückgibt.

## Warum zwei Dienste?

Eine gehostete Webseite kann nicht auf `C:\Noten\...` des Besuchers zugreifen.
Dateien werden daher hochgeladen. MuseScore Studio selbst ist außerdem eine
umfangreiche Desktop-Anwendung; die Weboberfläche soll nicht davon abhängen,
dass MuseScore in Streamlit Community Cloud zufällig lauffähig ist.

Die Masterdateien werden auf dem Worker nur in einem `TemporaryDirectory`
gespeichert. MuseScore bekommt ausschließlich diese temporären Kopien. Nach
Erstellung der Antwort wird das Arbeitsverzeichnis gelöscht.

## Funktionen des aktuellen MVP

- Upload einzelner `.mscz`-Dateien
- Upload eines ZIP-Archivs mit mehreren `.mscz`-Dateien
- Prüfung, ob die Datei eine lesbare MSCZ/MSCX-Struktur enthält
- Checkbox pro Stück
- frei editierbare Positionsnummer / Reihenfolge
- Partitur ja/nein
- vorhandene Auszüge ja/nein
- Violinschlüssel pro Stück: `keine/C`, `B♭`, `E♭`
- Bassschlüssel pro Stück: `keine/C`, `B♭`, `E♭`
- Dry-Run über den Worker
- PDF-Erstellung über den Worker
- Ergebnisübersicht und detaillierte Staff-Warnungen
- ZIP-Download aller PDFs
- vorhandene MuseScore-Auszüge werden weiterhin gefiltert; potentielle,
  nicht gespeicherte Auszüge werden nicht ausgegeben

## 1. Streamlit-Seite deployen

Repository nach GitHub pushen. **Nicht** committen:

- `.streamlit/secrets.toml`
- `vendor/MuseScore.AppImage`
- Noten/PDFs
- Worker-Token

In Streamlit Community Cloud als Entry Point auswählen:

    streamlit_app.py

Die Python-Abhängigkeiten stehen in `requirements.txt`.

Unter *App settings → Secrets* eintragen:

```toml
[worker]
url = "https://DEIN-WORKER.example.com"
token = "EIN-LANGES-ZUFAELLIGES-TOKEN"
```

`.streamlit/config.toml` setzt das Uploadlimit auf 400 MB. Der Code akzeptiert
pro einzelner MSCZ maximal 100 MB.

## 2. Worker bereitstellen

Der Worker ist `worker_api.py`. Für einen Docker-Build:

1. Offizielle MuseScore Studio **4.7.x x86_64 AppImage** herunterladen.
2. Als `vendor/MuseScore.AppImage` ablegen.
3. Image bauen:

```bash
docker build -f Dockerfile.worker -t musescore-transposer-worker .
```

4. Starten:

```bash
docker run --rm -p 8000:8000 \
  -e WORKER_TOKEN="DEIN_LANGES_TOKEN" \
  musescore-transposer-worker
```

Gesundheitstest:

    GET /health

Die Antwort soll `"ok": true` und eine MuseScore-Version `4.7.x` zeigen.

### Wichtige Worker-Variablen

Siehe `worker.env.example`:

- `WORKER_TOKEN` – Pflicht
- `MUSESCORE_EXE` – Pfad zur MuseScore-Executable/AppRun
- `MUSESCORE_EXTENSION_DIR` – falls die Distribution einen anderen User-Pfad nutzt
- `QT_QPA_PLATFORM=offscreen`
- `MAX_SCORE_BYTES`
- `MAX_BATCH_BYTES`

## Sicherheit

- Der `/process`-Endpunkt erfordert einen Bearer-Token.
- Der Token gehört ausschließlich in Streamlit Secrets und Worker-Environment.
- Upload-Dateinamen werden auf den Basename reduziert.
- Der Worker akzeptiert nur gültige MSCZ-ZIP-Strukturen.
- Die Eingabedateien werden nicht dauerhaft gespeichert.
- Vor und nach der Verarbeitung wird ein SHA-256-Hash der temporären
  Masterkopie geprüft.
- Die echte Originaldatei auf dem Benutzergerät kann von einem Cloud-Worker
  technisch nicht verändert werden, da sie nur als Upload-Kopie vorliegt.

## Noch nicht im MVP

- dauerhafte Online-Bibliothek / gespeicherte Projekte
- Drag-and-drop-Sortierung; aktuell Positionsnummer
- Benutzerkonten/Rollen innerhalb der App
- Zusammenführen mehrerer Stücke zu Musiker-Mappen
- automatischer Cloud-Worker-Deploy per CI

Diese Punkte lassen sich auf der jetzigen Architektur ergänzen, ohne die
Transpositionsengine erneut zu schreiben.
