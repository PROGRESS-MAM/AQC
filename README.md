# QC Worker mit Marathon-Adapter

Prüft Clips mit AQC. Der QC-Worker ist universell einsetzbar. Der Marathon-Adapter macht daraus einen Worker für Marathon: Er holt QC-Jobs aus dem Pool, lässt sie prüfen und gibt den Report an Marathon zurück. Mehrere Rechner können parallel am selben Pool arbeiten; jeder Job wird nur einmal bearbeitet.

## Aufbau

| Pfad | Aufgabe |
|---|---|
| `aqc/` | Core: `check()` |
| `aqc/tools/` | Prüftools `stream_specs`, `digital_silence`, `reference_frame` |
| `res/` | `ffmpeg` und `ffprobe` |
| `qc_worker.py` | QC-Worker: `process()` |
| `marathon/marathon_adapter.py` | allgemeiner Teil aller Marathon-Adapter, **nicht verändern** |
| `marathon/qc_marathon.py` | QC-Adapter: Einstellungen und Prüfplan, Einstieg der EXE |
| `marathon/configs/` | TOML-Dateien der Prüftools |
| `marathon/build_qc.py` | baut die EXE |

## Installation

Python 3.11+, `numpy`, `toolbox`. `ffmpeg` und `ffprobe` liegen in `res/`, sonst werden sie im Systempfad gesucht. Für den Build zusätzlich `pyinstaller`.

## Marathon-Worker starten

Als Skript aus dem Projektordner:

```
python -m marathon.qc_marathon
```

Als EXE: `qc_marathon_<version>.exe` auf den Rechner kopieren und starten. Der Rechner braucht Lese- und Schreibzugriff auf die Marathon-Freigabe.

- Der Worker heißt `<Rechnername>-QC`. Je Rechner läuft ein QC-Worker; ein zweiter Start auf demselben Rechner bricht ab.
- Vor dem ersten Start mit QC Marathon 2.2.1: alte Datei `<work_dir>/worker/<Rechnername>.json` und Ordner `<work_dir>/qc/laufend/<Rechnername>/` löschen.
- Ohne Jobs schaut er alle `poll_seconds` erneut in den Pool; die Konsole zeigt dann einmal `Warte auf Jobs in …`.
- Beenden mit `Strg+C`. Ein laufender Job wird abgebrochen und beim nächsten Start als `failed` gemeldet; Marathon legt ihn neu aus.
- Log: `qc_marathon.log` neben der EXE bzw. in `marathon/`; enthält alle Konsolenzeilen außer den Laufzeilen.

### Konsolenausgabe

Jede Zeile beginnt mit der Uhrzeit. Je Job: Übernahme, Clip mit FFE-Tafel und Prüfplan, Eingang und bei FFE-Tafel das Referenzbild jeweils mit Größe, gelesene Clipdaten, Ergebnis und Dauer je Tool (`[x/y]`), Prüfbericht und Job-Ergebnis mit Gesamtdauer.

Solange ein Schritt läuft, steht darunter eine Laufzeile: Fortschrittsbalken, Prozent, Medienzeit, Datenrate (MB/s) bzw. Bildrate (fps) mit Tempo, bisherige Laufzeit und Restzeit (`ETA`). Die nächste Zeile ersetzt sie; ist sie breiter als die Konsole, wird sie in der Mitte gekürzt. Pfade in den Zeilen von AQC beginnen ab `.marathon`, z. B. `.marathon\qc\…`.

```text
11:27:33 Job a_ffe übernommen.
11:27:33 Clip 222, FFE-Tafel ja, Prüfplan proxy_standard+ffe_tafel (5 Tools)
11:27:33 Eingang: .marathon\qc\eingang\111_222_10Mbit.mp4, 76.7 MB
11:27:33 111_222_10Mbit.mp4 | Clip gelesen: Länge 00:01:00, 25 fps, Start-TC 00:00:00:00, Streams: 1 video, 1 audio
11:27:33 111_222_10Mbit.mp4 | [1/5] stream_specs(video_1080p)  OK  0.0s
11:27:33 111_222_10Mbit.mp4 | [2/5] stream_specs(audio_aac_stereo)  FEHLER  0.0s
11:27:34 [3/5] digital_silence(keine_stille) [█████░░░░░] 53% 00:00:32/00:01:00 | 40.0 MB/s 104.0x | 00:00 ETA 00:00
```

## EXE bauen

```
python marathon/build_qc.py
```

Ergebnis: `dist/qc_marathon_<app_version>.exe` mit Adapter, Worker, AQC, `marathon/configs/` und `res/`. Die Version steht im Dateinamen und in den Windows-Dateieigenschaften. Änderungen an Einstellungen, Prüfplan oder Configs gelten erst mit einem neuen Build.

## Einstellungen in `qc_marathon.py`

| Eintrag | Bedeutung |
|---|---|
| `app_version` | Version der EXE |
| `root_path` | Wurzel der Marathon-Freigabe, wie `root_path` in Marathons `res/config.ini` |
| `work_dir` | Arbeitsordner von Marathon, wie `work_dir` in Marathons `res/config.ini` |
| `poll_seconds` | Wartezeit, wenn der Pool leer ist |
| `heartbeat_seconds` | Abstand der Lebenszeichen an Marathon, 1 bis 60 |
| `qc_plan_name` | Name des Standard-Prüfplans; steht als `preset` im Report |
| `qc_plan` | Standard-Prüfplan für alle Clips: Liste der Tools |
| `ffe_plan_name` | Name der FFE-Tafel-Prüfung; im Report als `<qc_plan_name>+<ffe_plan_name>` |
| `ffe_plan` | zusätzliche Tools für Jobs mit `"ffe_tafel": true` |

Ein Eintrag in `qc_plan` oder `ffe_plan`:

```python
{"tool": "reference_frame", "config": "FFE_tafel_ende_not_ok", "must_fail": True}
```

| Feld | Bedeutung |
|---|---|
| `tool` | Prüftool aus `aqc.tools`, z. B. `stream_specs`, `digital_silence`, `reference_frame` |
| `config` | Name der TOML-Datei in `marathon/configs/` ohne Endung |
| `must_fail` | optional; `True`, wenn das Tool nicht bestehen darf |

Beim Start prüft der Adapter, ob alle Prüftools bekannt und alle Configs vorhanden und lesbar sind. Referenzbilder von Configs in `qc_plan` müssen neben der Config liegen; das Referenzbild der Tools in `ffe_plan` kommt aus dem Job.

## Prüftool `digital_silence`

Prüft, ob jeder Kanal jedes Audiostreams Ton enthält. Jeder Kanal wird in Fenster der Länge `window` geteilt; ein Fenster zählt als Ton, wenn sein Pegel (RMS) mindestens `threshold` erreicht. Ist der Anteil der Fenster mit Ton an der Länge des Audiostreams kleiner als `min_active`, gilt der Kanal als stumm und das Tool ist nicht bestanden.

```toml
tool       = "digital_silence"
threshold  = "-60dB"
min_active = "1%"
window     = "1s"
```

| Eintrag | Pflicht | Bedeutung |
|---|---|---|
| `tool` | ja | `digital_silence` |
| `threshold` | ja | Pegelschwelle in dBFS (RMS je Fenster, Vollaussteuerung = 0 dBFS), kleiner als 0; z. B. `"-60dB"` oder `-60` |
| `min_active` | ja | Mindestanteil mit Ton je Kanal in Prozent, größer als 0 bis 100; z. B. `"1%"` oder `1` |
| `window` | nein | Fensterlänge, z. B. `"1s"` oder `0.5`; Standard `1s`. Ein Geräusch zählt immer als ganzes Fenster |

Im Prüfbericht:

- `failures`: je stummem Kanal ein Eintrag mit `criterion` `min_active`, `stream`, `channel` (ab 1) und dem gemessenen Anteil unter `actual`.
- `info`: je Audiostream eine Zeile `Stream <n> Ist-Werte: Kanal 1 Ton=99.95% Spitze=-10.2 dBFS, …`; ein Kanal nur mit digitalen Nullen hat `Spitze=-inf dBFS`.

## FFE-Tafel

Marathon entscheidet je Clip, ob er die FFE-Tafel braucht, und schreibt das als `ffe_tafel` in den QC-Job. Jobs mit `"ffe_tafel": true` werden nach dem Standard-Prüfplan zusätzlich mit `ffe_plan` geprüft, alle anderen nur mit `qc_plan`.

Referenzbild ist die Datei aus `ffe_reference_image` im Job (z. B. `.marathon/worker/FFE-Filmerbe-DEFA-Titel_Tafel.png`); sie ersetzt `frames` in den Configs von `ffe_plan`. Marathon hält das Bild dort aktuell, der Worker liest es nur. Eine neue Tafel braucht daher keinen neuen Build.

| Prüfung | Config | Prüfbereich | bestanden, wenn |
|---|---|---|---|
| Tafel am Anfang | `FFE_tafel_start_ok` | erste 10 s (`start+10s`) | Tafel vorhanden |
| Tafel am Ende | `FFE_tafel_ende_not_ok` | letzte 10 s (`end-10s`), `must_fail` | keine Tafel |

Fehlt die Tafel am Anfang oder ist sie am Ende vorhanden, ist der Job `rejected`; der Kurztext nennt die Prüfung. Ist das Referenzbild nicht lesbar, ist der Job `failed`.

## Ergebnis eines Marathon-Jobs

- Im `output_folder` des Jobs: `<clip>.aqc.json` mit dem vollständigen Prüfergebnis, je Tool ein Eintrag unter `tools`.
- Report an Marathon in `report_folder`: `<job_id>.json` mit `status`, `result` (Kurztext), `preset`, `worker` und `versions`.

| Prüfergebnis | `status` |
|---|---|
| alle Tools bestanden | `ok` |
| mindestens ein Tool nicht bestanden | `rejected` |
| Tool nicht ausführbar, Clip unlesbar, Job ungültig oder Fehler | `failed` |

`preset` im Report: `proxy_standard`, für Jobs mit `"ffe_tafel": true` `proxy_standard+ffe_tafel`. Ungültig sind Jobs ohne `input`, ohne `ffe_tafel` (`true`/`false`) und Jobs mit `"ffe_tafel": true` ohne `ffe_reference_image`.

## Dateien auf der Marathon-Freigabe

| Pfad | Verwendung |
|---|---|
| `<work_dir>/qc/offen/` | Jobs abholen |
| `<work_dir>/qc/laufend/<Rechnername>-QC/` | übernommene Jobs |
| `<work_dir>/qc/fertig/` | Reports an Marathon |
| `<work_dir>/worker/<Rechnername>-QC.json` | Heartbeat mit laufendem Job (`job_id`, `job_ids`) und Versionen |
| `ffe_reference_image` aus dem Job | Referenzbild der FFE-Tafel, wird nur gelesen |
| `input` aus dem Job | Proxy, wird nur gelesen |
| `output_folder` aus dem Job | Prüfberichte |

Der Adapter verarbeitet nur Jobs mit `schema_version = 6`.

## QC-Worker direkt einbinden

```python
from qc_worker import process

result = process(["D:/clips"], [{"tool": "stream_specs", "config": "C:/qc/video_1080p.toml"}],
                 "D:/berichte", recursive=True, log_path=None)
print(result.state, result.summary)
```

| Parameter | Bedeutung |
|---|---|
| `sources` | Liste von Dateien und/oder Ordnern; Systemdateien, Dateien mit Punkt am Anfang und `*.aqc.json` werden übersprungen |
| `tools` | Liste wie `qc_plan`; `config` als Pfad zur TOML-Datei |
| `report_folder` | Ziel der Berichte `<clip>.aqc.json`; wird angelegt; vorhandene Berichte bleiben erhalten, neue erhalten `-1`, `-2` … |
| `recursive` | `True`: Ordner samt Unterordnern |
| `log_path` | optionale Log-Datei: Meldungen des Workers und je Clip die Ergebniszeile von AQC |
| `on_progress` | optional `on_progress(text, done)`: erhält alle Zeilen von AQC und Worker; Laufzeilen mit `done=False`, die nächste Zeile ersetzt sie. Ohne `on_progress` gibt AQC seine Zeilen selbst in der Konsole aus. Pfade in AQC-Zeilen sind ab `.marathon` gekürzt |

| Ergebnis | Bedeutung |
|---|---|
| `state` | `ok`, `rejected` oder `error` (schlechtester Clip; `error` auch bei ungültigen Quellen oder Tools) |
| `summary` | Kurztext |
| `clips` | je Clip `source`, `state`, `summary`, `report` |
| `versions` | Versionen von QC-Worker und AQC |
