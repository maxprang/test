# Architektur und Designentscheidungen

Diese Datei erklärt, *warum* restore-guard so gebaut ist. Die Bedienung steht im
[README](README.md).

## Die Kernidee

Backup-Tools beantworten: „Hat der Job durchgelaufen?"
Das ist die falsche Frage. Die richtige ist:

> **Wann wurde dieses Backup zuletzt nachweislich wiederhergestellt?**

Alles im Programm ist auf diese eine Zahl ausgerichtet. Sie ist der Grund, warum
es eine persistente History-Datenbank gibt (ohne Historie keine Zahl), warum
`max_age` existiert (eine Zahl ohne Verfallsdatum ist wertlos), und warum die
Prometheus-Metrik `restore_guard_stale` das einzige ist, worauf man alarmieren
muss.

Ein Backup-Tool, das seine eigenen Backups prüft, hat einen Interessenkonflikt.
restore-guard ist deshalb bewusst **kein** Backup-Tool: es schreibt nie in ein
Repository, es kennt nur Lesen und Wiederherstellen.

## Ablauf eines Laufs

```
                 ┌──────────┐
   config.yml ──►│  Runner  │
                 └────┬─────┘
                      │  je Job:
        ┌─────────────┼──────────────┬──────────────┬─────────────┐
        ▼             ▼              ▼              ▼             ▼
   preflight    list_snapshots     select       restore        verify
   (Repo da?)   (was gibt es?)   (welches?)   (in temp dir)  (Checks)
        │                                          │             │
        │                                          ▼             ▼
        │                                    dir_stats     VerifyResult[]
        └──────────────────────────────────────────┴──────┬──────┘
                                                          ▼
                                              RunRecord ─► SQLite
                                                          │
                                     ┌────────────────────┼────────────────┐
                                     ▼                    ▼                ▼
                              Statustabelle          notify()        Prometheus
                              (+ Exit-Code)      (ntfy/webhook)       Textfile
```

Geteilte Bausteine, bewusst je an genau einer Stelle:

| Modul                        | Enthält                                                          |
|------------------------------|------------------------------------------------------------------|
| `util.py`                    | `as_list`, `shell_quote`, `parse_iso_time`, `ensure_within`, `dir_stats`, `run` |
| `verifiers/checks.py`        | die Erwartungs-Sprache (`expect_min/max/equals/contains`)         |
| `verifiers/container_db.py`  | Dump finden, Container hochfahren, einspielen, abfragen           |

Das ist keine Kosmetik: als die Vergleichslogik dreimal kopiert existierte, hatte
die SQLite-Kopie bereits still `expect_contains` verloren. Eine Implementierung
kann nicht auseinanderlaufen.

Zwei Erweiterungspunkte, beide über ein Registry-Decorator (`@register`):

- **Source** (`restore_guard/sources/`) — kann Snapshots auflisten und einen in
  ein Verzeichnis wiederherstellen. Aktuell `restic`, `borg`, `local`, `zfs`.
- **Verifier** (`restore_guard/verifiers/`) — bekommt ein wiederhergestelltes
  Verzeichnis und sagt ja oder nein. Aktuell `files`, `sqlite`, `command`,
  `postgres`, `mysql`, `http`.

Ein neuer Typ ist eine Datei plus ein Import in `_load_builtins()` bzw.
`build_source()`. Beide Basisklassen validieren ihre Config im Konstruktor, damit
`restore-guard validate` Fehler findet, bevor nachts irgendwas 40 Minuten lang
Daten schaufelt.

## Entscheidungen, die etwas kosten

### Zufällige Snapshot-Auswahl statt „immer das neueste"

`snapshot: random` ist der wichtigste Schalter des Programms. Nur das neueste
Snapshot zu prüfen beweist, dass letzte Nacht funktioniert hat. Es beweist
nichts über die Retention-Kette — und genau die brauchst du, wenn du am Dienstag
merkst, dass seit drei Wochen verschlüsselte Dateien mitgesichert werden.

Der Preis: einzelne Läufe sind nicht reproduzierbar und ein Fehlschlag kann ein
altes, längst irrelevantes Snapshot betreffen. Deshalb steht die Snapshot-ID in
jedem `RunRecord` und in jeder Benachrichtigung.

### `FAILED` und `ERROR` sind verschiedene Dinge

- `FAILED` — der Restore lief, ein Check sagt nein. **Dein Backup ist kaputt.**
- `ERROR` — der Restore selbst kam nicht zustande: Repo nicht erreichbar,
  Passphrase falsch, Timeout, Docker tot. **Dein Prüfer ist kaputt.**

Beide sind Alarme, aber mit völlig verschiedenen nächsten Schritten. Ein Tool,
das beides zu „rot" zusammenfasst, trainiert dir Alarm-Müdigkeit an. Ein
unerwarteter Python-Fehler in einem Job wird ebenfalls zu `ERROR` und stoppt die
restlichen Jobs nicht — der eine kaputte Job darf nicht die Prüfung der anderen
zwölf verhindern.

### Erfolgreiche Restores werden gelöscht, gescheiterte nicht

Nach einem grünen Lauf ist das Verzeichnis wertlos und würde nur die Platte
füllen. Nach einem roten ist es das Beweismittel: du willst in die kaputte
`db.sqlite3` schauen können, ohne den 40-Minuten-Restore zu wiederholen.

### Kein eingebauter Scheduler

systemd-Timer und cron können das seit Jahrzehnten, inklusive Jitter,
Persistenz über Reboots und Logging. Ein eigener Scheduler wäre ein Daemon, der
laufen muss, damit die Prüfung läuft, die prüft, ob Backups laufen. Stattdessen:
klare Exit-Codes und ein `--quiet`-Modus, der nur bei Problemen redet.

### Subprocess statt SDKs

`restic`, `borg` und `docker` werden über ihre CLI angesprochen. Das
Docker-SDK würde eine Abhängigkeit und eine API-Versionsfrage einführen für
einen Funktionsumfang, den sechs `subprocess`-Aufrufe abdecken. Einzige
Laufzeitabhängigkeit ist PyYAML.

### Wer mountet, räumt selbst auf

Die ersten drei Quellen kopierten Dateien; der Runner konnte den Restore danach
einfach löschen. Der ZFS-Source bricht diese Annahme: unter dem
Restore-Verzeichnis liegt ein gemountetes Dataset, und ein `rm -rf` würde durch
den Mount hindurch in echte Daten laufen.

Deshalb gibt es `Source.manages_destination`. Ist es gesetzt, ruft der Runner
`source.cleanup()` statt zu löschen, und entfernt das Verzeichnis erst, wenn es
nachweislich leer ist. Ein Fehler im Cleanup wird protokolliert, überschreibt
aber nie das Urteil des Jobs — ein hängengebliebener Clone ist ein Platzproblem,
ein verfälschtes Urteil ein Vertrauensproblem.

`zfs destroy` ist der einzige destruktive Befehl im Programm und läuft
unbeaufsichtigt um 03:30. Er ist dreifach abgesichert: Name enthält
`restore-guard`, `origin`-Property ist gesetzt (es ist wirklich ein Clone), keine
Kind-Datasets. Fällt eine Prüfung durch, bleibt der Clone stehen. Die Tests
faken die ZFS-CLI und prüfen genau diese Logik — die riskante Stelle ist, *welchen
Namen* wir an `destroy` übergeben, nicht ob das Kernelmodul funktioniert.

### Threads für Parallelität, Locks an genau zwei Stellen

Jobs verbringen ihre Zeit im Warten auf restic, tar oder docker — der GIL ist nie
der Engpass, also Threads statt Prozesse. Thread-safe gemacht wurden nur zwei
Dinge: die SQLite-Verbindung (ein `RLock` um jeden Zugriff; die kritischen
Abschnitte sind Mikrosekunden, nie ein Restore) und der Logger (sonst
verschränken sich Zeilen genau dann, wenn etwas schiefgeht und man sie lesen
muss). Ergebnisse werden in Config-Reihenfolge zurückgegeben, damit die
Statustabelle zwischen Läufen nicht durcheinanderpurzelt.

Default bleibt `parallel: 1`. Bei wenigen großen Jobs sind drei gleichzeitige
Restores von derselben NAS langsamer als drei nacheinander.

### Stichprobe statt Vollprüfung

Ursprünglich als „Teil-Restore per Stichprobe" geplant, dann verworfen: dafür
müsste jede Quelle ihre Dateien einzeln auflisten und adressieren können, was
für borg und tar unschön wird. restic kann das Richtige bereits selbst, also
gibt es stattdessen `read_data_subset: 2%` — `restic check --read-data-subset`
im Preflight. Das prüft die Pack-Dateien direkt, also auch Blobs, die kein
wiederhergestelltes Snapshot berührt. Über einen Monat läuft das Repository
einmal komplett durch, ohne dass je eine Nacht blockiert ist.

Ein fremdes Feature zu benutzen statt ein eigenes halb zu bauen, ist hier die
bessere Lösung — auch wenn es bedeutet, dass borg und local diese Prüfung nicht
haben.

### Der Restore-Baum wird einmal pro Job gelaufen

`dir_stats()` kostet ein `lstat` pro Datei. Der Runner läuft den Baum ohnehin,
um „restored 12.483 files (2,4 GiB)" zu protokollieren, und reicht das Ergebnis
über `VerifyContext.stats` weiter. Bei einem Job mit drei `files`-Checks waren
das vorher vier vollständige Walks über womöglich mehrere TB.

`VerifyContext.tree_stats()` fällt auf einen eigenen Walk zurück, wenn niemand
vorgerechnet hat — Verifier bleiben damit außerhalb des Runners benutzbar, etwa
in Tests.

### Timeouts als Budget, nicht pro Schritt

`timeout` gilt für den ganzen Job. Der Restore verbraucht davon, die Verifier
teilen sich den Rest, und ein Verifier, der nichts mehr übrig hat, wird als
Fehlschlag mit klarer Begründung gemeldet statt still übersprungen. Sonst
konfiguriert man fünf Timeouts und weiß nie, welcher gegriffen hat.

## Fehlermodi, die das Tool gezielt fängt

| Realer Ausfall | Der Check, der ihn fängt |
|---|---|
| Job läuft, sichert seit Monaten fast nichts (Pfad umgezogen, Exclude zu gierig) | `files` mit `min_bytes` |
| Backup ist eingefroren, Inhalte sind alt, Dateien aber vorhanden | `files` mit `newer_than` |
| SQLite wurde im laufenden Betrieb kopiert und ist korrupt | `sqlite` `integrity_check` |
| Postgres-Dump ist abgeschnitten (Platte voll beim Dump) | `postgres` mit `ON_ERROR_STOP=1` |
| Dump lädt sauber, ist aber die leere Schema-Version | `postgres` `checks` mit `expect_min` |
| Ein einzelner kritischer Pfad fehlt (Keyfile, Config) | `files` `must_exist` |
| Retention hat alte Snapshots beschädigt | `snapshot: random` |
| `mysqldump` ohne `--single-transaction` erwischte Tabellen im Schreiben | `mysql` mit `check_tables: true` |
| Bit Rot in Blobs, die kein geprüftes Snapshot anfasst | restic `read_data_subset` |
| Verschlüsselung ohne gültige Passphrase | `preflight` → `ERROR` |

Was es **nicht** fängt, und das ehrlich: ob deine Wiederherstellungs-*Prozedur*
funktioniert. Ein grüner restore-guard heißt „die Daten sind heil", nicht „du
weißt, in welcher Reihenfolge du die 14 Dienste wieder hochziehst".

## Sicherheitsüberlegungen

Die Angriffsfläche ist bewusst klein, aber nicht null:

- **Config = Codeausführung.** `command`-Verifier führen aus, was dort steht.
  Die Datei gehört root und sollte `640` sein.
- **Archive werden vor dem Entpacken geprüft.** Ein Tar-Member mit `../` bricht
  den Restore ab, statt auf dem Host zu landen — ein korruptes Archiv soll das
  System nicht beschädigen.
- **Pfade können den Restore nicht verlassen.** `VerifyContext.resolve()`
  resolved und vergleicht gegen das Wurzelverzeichnis.
- **SQLite wird read-only geöffnet.** Sonst würde ein WAL-Replay den
  wiederhergestellten Stand verändern, den wir gerade beurteilen wollen.
- **Container publishen nur auf `127.0.0.1`** und werden im `finally` entfernt,
  auch wenn der Check abstürzt.
- **Empfohlen: read-only Repo-Zugang.** restore-guard braucht nur Lesen. Ein
  kompromittierter Prüfer soll nicht die Backups löschen können.
- **`zfs destroy` ist dreifach abgesichert** (siehe oben) und ist der einzige
  Befehl im Programm, der etwas zerstören kann.
- **Die HTML-Seite escaped alle Werte** und lädt nichts nach. Jobnamen und
  Fehlermeldungen landen darin, und die Seite hängt womöglich öffentlich im
  Dashboard.

## Stand und offene Punkte

Umgesetzt: alle ursprünglich geplanten Erweiterungen — MySQL/MariaDB-Verifier,
`--parallel N`, ZFS-Source, HTML-Statusseite, Stichprobenprüfung (als restic
`read_data_subset`, siehe oben).

Was bewusst offen bleibt:

1. **Btrfs bekommt keine eigene Quelle.** Dessen Snapshots sind bereits
   Verzeichnisse — `type: local` mit `pattern` deckt sie ohne neuen Code ab.
   Eine eigene Quelle wäre Dopplung ohne Gewinn.
2. **Der ZFS-Source ist nicht gegen echtes ZFS getestet.** Die Tests faken die
   CLI und decken Namensberechnung und Sicherheitscheck ab; ein Lauf gegen ein
   Wegwerf-Dataset auf echter Hardware fehlt.
3. **Kein Verifier für Restic-interne Konsistenz bei borg.** borg hat mit
   `borg check --verify-data` ein Äquivalent zu `read_data_subset`, aber ohne
   Teilmengen-Option — es gibt nur ganz oder gar nicht, und „ganz" ist auf
   großen Repos keine nächtliche Option.
4. **Wiederherstellungs-Reihenfolge.** Ein grüner Lauf sagt „die Daten sind
   heil", nicht „du weißt, wie du die 14 Dienste wieder hochziehst". Das wäre
   eher ein Runbook-Generator als ein Prüfer — und damit ein eigenes Projekt.
