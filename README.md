# MuseScore Ensemble Transposer

Batch-Werkzeug für **MuseScore Studio 4.7.x** unter **Windows 10/11**.

Es verarbeitet alle `.mscz`-Dateien direkt in einem gewählten Ordner, transponiert ausschließlich eindeutig erkannte, durchgehend im Violinschlüssel notierte Staves und exportiert anschließend Partitur und/oder vorhandene verknüpfte Auszüge als PDF.

## Sicherheitsprinzip

Die Masterdateien werden **nie von MuseScore bearbeitet**.

Für jedes Stück geschieht Folgendes:

1. SHA-256 der Masterdatei berechnen.
2. Masterdatei binär in ein temporäres Verzeichnis kopieren.
3. Ausschließlich diese Kopie an MuseScore übergeben.
4. Transposition und PDF-Export nur mit temporären Dateien durchführen.
5. PDFs prüfen.
6. Temporäre Dateien automatisch löschen.
7. SHA-256 der Masterdatei erneut prüfen.

Wenn sich der Hash verändert hat, wird der Durchlauf als kritischer Fehler markiert.

## Unterstützte Transpositions-Presets

| Target | Bearbeitung |
|---|---|
| `C` | keine Tonhöhenänderung |
| `Bb` | Violinschlüssel-Staves große Sekunde aufwärts, +2 Halbtöne |
| `Eb` | Violinschlüssel-Staves große Sexte aufwärts, +9 Halbtöne |

Bassschlüssel-Staves bleiben unverändert.

Das Tool ändert nicht nur MIDI-Pitches, sondern führt die notierte Schreibweise über MuseScores TPC-Daten sowie vorhandene Tonartvorzeichnungen mit. Schlüsselwechsel, ungewöhnliche Schlüssel, nicht eindeutig behandelbare TPC-Daten oder Tonarten außerhalb des sicheren Bereichs werden nicht geraten, sondern übersprungen und protokolliert.

## MuseScore-Version

Die Implementierung wurde gegen den Quellcode und die Extension-/CLI-Dokumentation von **MuseScore Studio 4.7.x** entwickelt, insbesondere 4.7.4.

Beim Start fragt das Programm die tatsächlich installierte MuseScore-Version mit `--long-version`/`--version` ab. Andere 4.x-Versionen werden standardmäßig abgelehnt. Für bewusstes Testen gibt es `--allow-untested-version`.

## Voraussetzungen

- Windows 10 oder Windows 11
- Python 3.10 oder neuer empfohlen
- MuseScore Studio 4.7.x
- Python-Paket `pypdf`

Installation der Python-Abhängigkeit:

```bat
py -3 -m pip install -r requirements.txt
```

Alternativ doppelklicken:

```text
install_requirements.bat
```

## Schnellstart

Empfohlen wird zuerst immer ein Dry-Run:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all --dry-run
```

Danach der echte Export:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all
```

Weitere Beispiele:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export parts
python transpose_scores.py "C:\Noten\Ensemble" --target Eb --export all
python transpose_scores.py "C:\Noten\Ensemble" --target C --export score
```

Ohne Argumente startet ein einfacher interaktiver Dialog im Terminal:

```bat
python transpose_scores.py
```

Oder doppelklicken:

```text
run_interactive.bat
```

## Dry-Run

Beispiel:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all --dry-run
```

Der Dry-Run führt die MuseScore-Bearbeitung vollständig **nur an einer temporären Kopie** aus. Er erzeugt keine PDFs.

Angezeigt und protokolliert werden unter anderem:

- gefundene Staves
- Staff-/Instrumentnamen
- erkannter Schlüssel
- verwendete Voices
- welche Staves tatsächlich sicher transponierbar waren
- Intervall und Tonart-Delta
- vorhandene gespeicherte Auszüge
- geplante PDF-Dateinamen
- Warnungen und übersprungene Staves

## Exportmodi

```text
--export parts   nur vorhandene verknüpfte Auszüge
--export score   nur Gesamtpartitur
--export all     Gesamtpartitur und vorhandene Auszüge
```

### Vorhandene Auszüge

Die Extension liest die **bereits im Score gespeicherten Excerpts** samt Titel und Staff-/Voice-Zuordnung aus.

Für den PDF-Export nutzt Python MuseScores 4.7-Part-Backend. Dieses Backend kann intern zusätzlich potentielle, noch nicht gespeicherte Auszüge bereitstellen. Das Werkzeug filtert die Backend-Ausgabe deshalb strikt gegen die vorher ermittelte Liste der tatsächlich gespeicherten Excerpts und schreibt **nur diese** als PDFs. Zusätzliche potentielle Parts werden verworfen und als Hinweis geloggt.

Dadurch müssen Auszugstitel für den MuseScore-Export nicht umbenannt werden, und Zeichen wie `?`, `:`, `/` oder `*` können den MuseScore-Export nicht blockieren.

## Dateinamen

Partitur:

```text
Amazing Grace_Bb_Partitur.pdf
```

Auszug:

```text
Amazing Grace_Bb_Oberstimmen.pdf
```

Windows-ungültige Zeichen werden im finalen Dateinamen durch `_` ersetzt:

```text
\ / : * ? " < > |
```

Doppelte Auszugstitel erhalten automatisch `_2`, `_3` usw.

## Ausgabeordner

Das Werkzeug durchsucht **keine Unterordner**.

Es erzeugt neben dem Eingabeordner einen neuen Ausgabeordner nach diesem Schema:

```text
Transponiert_Bb_2026-09-07_1330
```

Bei Namenskollisionen wird eine laufende Nummer ergänzt.

Der Ausgabeordner enthält:

- die erzeugten PDFs
- `transposition_log.txt`

## Logdatei

Die Logdatei enthält unter anderem:

- Datum und Start-/Endzeit
- Python-Version
- MuseScore-Version und EXE-Pfad
- Target und Intervall
- Anzahl gefundener Stücke
- SHA-256-Prüfung je Master
- erkannte Staves und Schlüssel
- verwendete Voices
- transponierte und übersprungene Staves
- Anzahl geänderter Noten/Tonartvorzeichnungen
- vorhandene Auszüge und Staff-/Voice-Zuordnung
- erzeugte PDFs mit Seitenzahl und Dateigröße
- Warnungen und Fehler
- Abschlusszusammenfassung

## PDF-Prüfung

Jede erzeugte PDF-Datei wird automatisch geprüft auf:

- Existenz
- Dateigröße > 0
- `%PDF-`-Header
- Lesbarkeit durch `pypdf`
- mindestens eine Seite

PDFs unter 2 KB werden zusätzlich als ungewöhnlich klein gewarnt.

## Automatische MuseScore-Extension

Das Projekt enthält eine MuseScore-4.7-Macro-Extension unter:

```text
extension\manifest.json
extension\main.js
```

Beim normalen Start kopiert Python diese automatisch nach:

```text
%LOCALAPPDATA%\MuseScore\MuseScore4\extensions\ensemble_transposer
```

Die Extension benutzt `apiversion: 1`, weil MuseScore 4.7 die dafür benötigten Low-Level-Engraving-Funktionen in seiner API-v1-Kompatibilitätsschicht weiterhin bereitstellt. Die Extension hat keine Benutzeroberfläche und wird nur über MuseScores CLI-Extension-Pfad auf temporären Scores aufgerufen.

Wenn die Extension bereits manuell installiert wurde:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all --no-install-extension
```

## MuseScore-EXE manuell angeben

Falls die automatische Suche die Installation nicht findet:

```bat
python transpose_scores.py "C:\Noten\Ensemble" --target Bb --export all --musescore "C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
```

Der tatsächliche Installationspfad kann je nach Installer abweichen.

## Self-Test

```bat
python transpose_scores.py --self-test
```

Dieser Test benötigt MuseScore nicht. Er prüft unter anderem Dateinamensbereinigung, Report-Auslesung aus einer synthetischen MSCZ und die Filterung gespeicherter Auszüge aus MuseScores Part-Backend-Ausgabe.

## Verhalten bei problematischen Scores

Das Werkzeug arbeitet absichtlich konservativ.

Es warnt oder überspringt einen Staff beispielsweise bei:

- Schlüsselwechsel innerhalb des Staffs
- anderem/ungewöhnlichem Schlüssel
- bereits unterschiedlich gespeicherten `tpc1`/`tpc2`-Schreibweisen
- Pitch außerhalb des MIDI-Bereichs nach Transposition
- Zielschreibweisen, die mehr als Doppelvorzeichen erfordern würden
- Zieltonarten außerhalb von -7 bis +7 Vorzeichen

Ein Bassschlüssel-Staff wird niemals automatisch transponiert.

Nach der Bearbeitung vergleicht die Extension zusätzlich musikalische Signaturen aller nicht ausgewählten Staves. Wenn ein nicht ausgewählter Staff unerwartet mitverändert wurde, wird die gesamte Transposition in der temporären Scorekopie zurückgerollt.

## Wichtige Grenzen

- Das Tool verarbeitet nur `.mscz` direkt im gewählten Ordner, nicht rekursiv.
- Es erzeugt keine neuen Part-/Excerpt-Definitionen in der Masterdatei.
- Das gespeicherte Part-Layout und bestehende Formatierungselemente werden nicht gezielt verändert. MuseScore kann wegen neuer Vorzeichenbreiten dennoch normale Layout-Neuberechnungen durchführen.
- Ein echter End-to-End-Test mit deiner lokalen Windows-/MuseScore-Installation muss auf dem Zielrechner erfolgen. Deshalb: zuerst `--dry-run` mit repräsentativen Scores verwenden und die erzeugten PDFs anschließend stichprobenartig visuell prüfen.

## Projektdateien

```text
musescore_transposer/
├─ transpose_scores.py
├─ requirements.txt
├─ README.md
├─ START_HERE.txt
├─ install_requirements.bat
├─ run_interactive.bat
└─ extension/
   ├─ manifest.json
   └─ main.js
```
