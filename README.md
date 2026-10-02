# QC Worker mit Marathon-Adapter

Prüft Clips mit AQC. Der QC-Worker ist universell einsetzbar. Der Marathon-Adapter macht daraus einen Worker für Marathon: Er holt QC-Jobs aus dem Pool, lässt sie prüfen und gibt den Report an Marathon zurück. Mehrere Rechner können parallel am selben Pool arbeiten; jeder Job wird nur einmal bearbeitet.

## Aufbau

| Pfad | Aufgabe |
|---|---|
| `aqc/` | Core: Prüfmethoden, `check()` |
| `res/` | `ffmpeg` und `ffprobe` |
| `qc_worker.py` | QC-Worker: `process()` |
| `marathon/marathon_adapter.py` | allgemeiner Teil aller Marathon-Adapter, **nicht verändern** |
| `marathon/qc_marathon.py` | QC-Adapter: Einstellungen und Prüfplan, Einstieg der EXE |
| `marathon/configs/` | TOML-Dateien der Prüfmethoden und Referenzbilder |
| `marathon/build_qc.py` | baut die EXE |

## Installation

Python 3.11+, `numpy`, `toolbox`. `ffmpeg` und `ffprobe` liegen in `res/`, sonst werden sie im Systempfad gesucht. Für den Build zusätzlich `pyinstaller`.

## Marathon-Worker starten

Als Skript aus dem Projektordner:

```
python -m marathon.qc_marathon
```

Als EXE: `qc_marathon_<version>.exe` auf den Rechner kopieren und starten. Der Rechner braucht Lese- und Schreibzugriff auf die Marathon-Freigabe.

- Der Worker heißt wie der Rechner. Je Rechner läuft ein Worker; ein zweiter Start mit demselben Namen bricht ab.
- Ohne Jobs schaut er alle `poll_seconds` erneut in den Pool.
- Beenden mit `Strg+C`. Ein laufender Job wird abgebrochen und beim nächsten Start als `failed` gemeldet; Marathon legt ihn neu aus.
- Log: `qc_marathon.log` neben der EXE bzw. in `marathon/`.

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
| `qc_plan` | Standard-Prüfplan für alle Clips: Liste der Tests |
| `ffe_plan_name` | Name der FFE-Tafel-Tests; im Report als `<qc_plan_name>+<ffe_plan_name>` |
| `ffe_ids_file` | ID-Liste der FFE-Clips in `marathon/configs/` |
| `ffe_plan` | zusätzliche Tests für Clips auf der ID-Liste |

Ein Eintrag in `qc_plan` oder `ffe_plan`:

```python
{"test": "reference_frame", "config": "FFE_tafel_ende_not_ok", "must_fail": True}
```

| Feld | Bedeutung |
|---|---|
| `test` | Prüfmethode aus `aqc.tests`, z. B. `stream_specs`, `digital_silence`, `reference_frame` |
| `config` | Name der TOML-Datei in `marathon/configs/` ohne Endung |
| `must_fail` | optional; `True`, wenn der Test nicht bestehen darf |

Beim Start prüft der Adapter, ob alle Prüfmethoden bekannt, alle Configs und Referenzbilder vorhanden und die ID-Liste lesbar ist.

## FFE-Tafel

Clips, deren `clip_id` in der ID-Liste steht, werden nach dem Standard-Prüfplan zusätzlich auf die Tafel `FFE-Filmerbe-DEFA-Titel_Tafel.png` geprüft. Alle anderen Clips erhalten keine Tafel-Tests.

| Test | Config | Prüfbereich | bestanden, wenn |
|---|---|---|---|
| Tafel am Anfang | `FFE_tafel_start_ok` | erste 10 s (`start+10s`) | Tafel vorhanden |
| Tafel am Ende | `FFE_tafel_ende_not_ok` | letzte 10 s (`end-10s`), `must_fail` | keine Tafel |

Fehlt die Tafel am Anfang oder ist sie am Ende vorhanden, ist der Job `rejected`; der Kurztext nennt den Test.

Dateien in `marathon/configs/`:

| Datei | Inhalt |
|---|---|
| `FFE-Filmerbe-DEFA-Titel_Tafel.png` | Referenzbild der Tafel |
| `FFE-Filmerbe-DEFA-Titel_ids.json` | ID-Liste |

ID-Liste: unter `titel` je Titel ein Eintrag. Berücksichtigt werden nur Einträge mit `clip_id` (Text oder Zahl); Einträge mit `"clip_id": null` werden übersprungen. Übrige Felder dienen nur der Pflege.

```json
{"titel": [
  {"title": "Sabine Wulff", "clip_id": "439074", "status": "eindeutig"},
  {"title": "Dornröschen", "clip_id": null, "status": "offen"}
]}
```

Zum Ergänzen die `clip_id` eintragen und eine neue EXE bauen.

## Ergebnis eines Marathon-Jobs

- Im `output_folder` des Jobs: `<clip>.aqc.json` mit dem vollständigen Prüfergebnis.
- Report an Marathon in `report_folder`: `<job_id>.json` mit `status`, `result` (Kurztext), `preset`, `worker` und `versions`.

| Prüfergebnis | `status` |
|---|---|
| alle Tests bestanden | `ok` |
| mindestens ein Test nicht bestanden | `rejected` |
| Test nicht ausführbar, Clip unlesbar, Job ungültig oder Fehler | `failed` |

`preset` im Report: `proxy_standard`, für Clips auf der ID-Liste `proxy_standard+ffe_tafel`. Jobs ohne `input` oder `clip_id` sind ungültig.

## Dateien auf der Marathon-Freigabe

| Pfad | Verwendung |
|---|---|
| `<work_dir>/qc/offen/` | Jobs abholen |
| `<work_dir>/qc/laufend/<Rechnername>/` | übernommene Jobs |
| `<work_dir>/qc/fertig/` | Reports an Marathon |
| `<work_dir>/worker/<Rechnername>.json` | Heartbeat mit laufendem Job und Versionen |
| `input` aus dem Job | Proxy, wird nur gelesen |
| `output_folder` aus dem Job | Prüfberichte |

Der Adapter verarbeitet nur Jobs mit `schema_version = 3`.

## QC-Worker direkt einbinden

```python
from qc_worker import process

result = process(["D:/clips"], [{"test": "stream_specs", "config": "C:/qc/video_1080p.toml"}],
                 "D:/berichte", recursive=True, log_path=None)
print(result.state, result.summary)
```

| Parameter | Bedeutung |
|---|---|
| `sources` | Liste von Dateien und/oder Ordnern; Systemdateien, Dateien mit Punkt am Anfang und `*.aqc.json` werden übersprungen |
| `tests` | Liste wie `qc_plan`; `config` als Pfad zur TOML-Datei |
| `report_folder` | Ziel der Berichte `<clip>.aqc.json`; wird angelegt; vorhandene Berichte bleiben erhalten, neue erhalten `-1`, `-2` … |
| `recursive` | `True`: Ordner samt Unterordnern |
| `log_path` | optionale Log-Datei |

| Ergebnis | Bedeutung |
|---|---|
| `state` | `ok`, `rejected` oder `error` (schlechtester Clip; `error` auch bei ungültigen Quellen oder Tests) |
| `summary` | Kurztext |
| `clips` | je Clip `source`, `state`, `summary`, `report` |
| `versions` | Versionen von QC-Worker und AQC |
