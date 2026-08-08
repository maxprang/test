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

Zwei Erweiterungspunkte, beide über ein Registry-Decorator (`@register`):

- **Source** (`restore_guard/sources/`) — kann Snapshots auflisten und einen in
  ein Verzeichnis wiederherstellen. Aktuell `restic`, `borg`, `local`.
- **Verifier** (`restore_guard/verifiers/`) — bekommt ein wiederhergestelltes
  Verzeichnis und sagt ja oder nein. Aktuell `files`, `sqlite`, `command`,
  `postgres`, `http`.

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

## Nächste sinnvolle Schritte

1. **MySQL/MariaDB-Verifier** — dasselbe Muster wie `postgres`.
2. **`--parallel N`** — Jobs laufen aktuell seriell. Bei vielen kleinen Jobs
   nervt das; bei wenigen großen ist seriell richtig, weil die NAS sonst
   einbricht.
3. **ZFS/Btrfs-Source** — `zfs send` in ein temporäres Dataset statt Dateikopie.
   Deutlich schneller für große Datasets.
4. **Statusseite** — die Tabelle als HTML, in ein Homelab-Dashboard einbindbar.
5. **Teil-Restore per Stichprobe** — bei mehreren TB nicht alles, sondern N
   zufällige Dateien plus Prüfsummenvergleich gegen das Original.
