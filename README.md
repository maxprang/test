# Open Directory Checker

Ein defensives Security-Werkzeug, das prüft, ob eine einzelne, angegebene URL
ein **offenes Verzeichnis** (Directory Listing) preisgibt — und falls ja, dessen
Inhalt auflistet. Gedacht für autorisierte Selbstprüfung: teste nur Systeme, die
dir gehören oder für die du eine Testerlaubnis hast.

Es handelt sich bewusst **nicht** um einen Massen-Scanner: das Tool sendet genau
eine HTTP-Anfrage an das von dir angegebene Ziel.

## Start

```bash
python3 app.py           # läuft auf http://0.0.0.0:8000
python3 app.py 8123      # optional: eigener Port
```

Dann im Browser öffnen und eine URL eingeben (z. B. `example.com/files/`).

Keine externen Abhängigkeiten — nur die Python-Standardbibliothek (Python 3.8+).

## Was ist ein offenes Verzeichnis?

Hat ein Webserver *Directory Listing* aktiviert und fehlt in einem Ordner eine
Index-Datei, generiert er eine öffentliche HTML-Liste aller Dateien ("Index of /").
So landen oft versehentlich Backups, Logs oder Zugangsdaten im Netz.

**Härtung:** Apache `Options -Indexes`, nginx `autoindex off;`.

## Sicherheitshinweise

- **SSRF-Schutz:** Anfragen an private, interne, Loopback- und Link-Local-Adressen
  (inkl. Cloud-Metadaten-Endpunkte) werden blockiert.
- Nur `http://` und `https://` werden akzeptiert; Antwort-Body wird auf 2 MiB und
  die Ausgabe auf 500 Einträge begrenzt.
- Nur für autorisierte Prüfungen verwenden.
