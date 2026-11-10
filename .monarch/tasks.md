# Tasks – AQC: Stille-Ausnahme wie Transcoder

## Globale Vorgaben
- expected_silence = {"LYNXarchive": ("LA",), "Historiathek": ("H",)}
- Kollektion exakt; Identifier mit strip(); Präfix groß-/kleinschreibungssensitiv; fehlt ein Feld oder kein str → Regel greift nicht
- Stille-Ausnahme (unverändert): nur wenn Regel greift UND alle Kanäle laut keine_stille stumm → digital_silence entfällt, bitrate aus audio_aac_stereo entfällt
- qc_marathon app_version = "2.2.5"
- Verpolung: im AQC keine Änderung

## Pakete
| ID | Paket | Dateien | Abhängig von | Modus | Review | Status |
|---|---|---|---|---|---|---|
| T1 | Historiathek/H als erwartete Stille, Regel als Config-Dict wie im Transcoder, Tests, README | marathon/qc_marathon.py, tests/test_qc_expected_silence.py, README.md | – | direct | A | fertig |

## Entscheidungen
- T1: Verpolung entfällt im AQC – Martin; AQC hat keine Polungs-/Dublettenprüfung, L/−L-Proxy besteht lokal alle Audio-Tools
- T1: Namen wie Transcoder: Config-Dict expected_silence, Funktion _expected_silence – einheitliche Regeldefinition in beiden Repos
- T1: Version qc_marathon 2.2.4 → 2.2.5 – Verhaltensänderung braucht neuen Build
