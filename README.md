# restore-guard

**Beweist, dass deine Backups wiederherstellbar sind — statt zu hoffen.**

Fast jedes Homelab hat funktionierende Backups. Fast keines hat *getestete*
Backups. Der Unterschied fällt genau einmal auf, und dann zum denkbar
schlechtesten Zeitpunkt.

restore-guard stellt regelmäßig ein **zufällig gewähltes** Snapshot in ein
Wegwerf-Verzeichnis wieder her, prüft es mit echten Checks — bis hin zum
Einspielen eines Postgres-Dumps in einen Container und einem HTTP-Request gegen
den echten Dienst — und merkt sich pro Job:

> *Wann wurde dieses Backup zuletzt nachweislich wiederhergestellt?*

Wird diese Zahl zu alt oder schlägt ein Check fehl, gibt es einen Push aufs Handy
und eine rote Metrik in Prometheus.

```
JOB                    STATUS  VERIFIED   SNAPSHOT      SIZE     TOOK    DETAIL
---------------------  ------  ---------  ------------  -------  ------  ----------------------------------
immich-db              OK      14h ago    a3f19c2b8e01  2.4GiB   4m12s
homeassistant-config   OK      2d06h ago  ha-2026-08-06 412.0MiB 38s
vaultwarden            FAIL    9d02h ago  vw-2026-07-30 1.2MiB   12s     db.sqlite3: integrity_check: page
                                                                         3 is never used
```

---

## Was es prüft (und warum genau das)

| Verifier   | Prüft                                                           | Braucht Docker |
|------------|-----------------------------------------------------------------|:--------------:|
| `files`    | Dateizahl, Größe, Pflichtpfade, Alter des neuesten Files         | nein           |
| `sqlite`   | `PRAGMA integrity_check`, Fremdschlüssel, eigene Count-Queries   | nein           |
| `command`  | beliebiges Skript (`sha256sum -c`, `gpg --verify`, …)            | nein           |
| `postgres` | Dump in Wegwerf-Container einspielen + SQL-Checks                | **ja**         |
| `mysql`    | dito für MySQL/MariaDB, optional `CHECK TABLE` über alles        | **ja**         |
| `http`     | echtes Image gegen die Daten starten und HTTP abfragen           | **ja**         |

Die Reihenfolge ist Absicht. `files` mit `min_bytes` und `newer_than` fängt den
mit Abstand häufigsten realen Ausfall: Der Job läuft jede Nacht brav durch und
sichert seit Monaten fast nichts, weil ein Pfad umgezogen ist oder eine
Exclude-Regel zu gierig wurde. `postgres`, `mysql` und `http` beantworten die
eigentliche Frage — *würde der Dienst damit wieder hochkommen?*

Backup-Quellen:

| Quelle   | Für                                                                        |
|----------|----------------------------------------------------------------------------|
| `restic` | restic-Repos, optional mit `read_data_subset`-Stichprobe gegen Bit Rot      |
| `borg`   | BorgBackup, lokal oder über SSH                                            |
| `local`  | Verzeichnisse und Tarballs: rsnapshot, `pg_dump \| gzip`, Proxmox, Cron    |
| `zfs`    | ZFS-Snapshots per Clone — kein Kopieren, auch bei mehreren TB in Sekunden  |

Btrfs braucht bewusst keine eigene Quelle: dessen Snapshots *sind* Verzeichnisse,
`type: local` mit `pattern` deckt sie ab.

## Installation

```bash
git clone <dieses-repo> /opt/restore-guard
cd /opt/restore-guard
pip install -e .          # oder: pip install pyyaml && python3 -m restore_guard ...
```

Voraussetzungen: Python ≥ 3.11 und PyYAML. Docker nur für `postgres`/`http`.
`restic`/`borg` nur, wenn du diese Quellen nutzt.

## Schnellstart

```bash
cp examples/config.yml /etc/restore-guard/config.yml
$EDITOR /etc/restore-guard/config.yml

restore-guard validate            # Config prüfen, nichts anfassen
restore-guard run --dry-run       # Repo kontaktieren, Snapshot wählen, nicht wiederherstellen
restore-guard run                 # der echte Lauf
restore-guard status              # "wann zuletzt bewiesen?"
```

Minimale Config:

```yaml
version: 1
defaults:
  workdir: /var/lib/restore-guard

jobs:
  - name: vaultwarden
    source:
      type: local
      repository: /mnt/backup/vaultwarden
      pattern: "vaultwarden-*.tar.gz"
      snapshot: random
    verify:
      - type: files
        min_bytes: 100KB
        newer_than: 36h
        must_exist: ["**/db.sqlite3"]
      - type: sqlite
        path: "**/db.sqlite3"
        queries:
          - sql: "SELECT count(*) FROM users"
            expect_min: 1
```

## Kommandos

| Kommando                     | Zweck                                                      |
|------------------------------|------------------------------------------------------------|
| `run [job…]`                 | wiederherstellen und prüfen (ohne Argument: alle aktiven)   |
| `run --dry-run`              | Repo erreichbar? Snapshot wählbar? Ohne Restore.            |
| `run --parallel N`           | bis zu N Jobs gleichzeitig                                  |
| `status`                     | Tabelle: letzte bewiesene Wiederherstellung je Job          |
| `status --json`              | dasselbe maschinenlesbar                                    |
| `status --html [datei]`      | fertige Statusseite fürs Dashboard                          |
| `history [job] [--limit N]`  | letzte Läufe                                                |
| `validate`                   | Config auf Fehler prüfen                                    |
| `metrics`                    | Prometheus-Textfile ausgeben/schreiben                      |
| `plugins`                    | verfügbare Quellen- und Verifier-Typen                      |

**Exit-Codes** (der Vertrag für cron und Monitoring):
`0` alles verifiziert · `1` mindestens ein Job kaputt oder veraltet · `2` Config-Fehler.

## Konfiguration

### `defaults`

| Schlüssel         | Default                  | Bedeutung                                        |
|-------------------|--------------------------|--------------------------------------------------|
| `workdir`         | `/var/lib/restore-guard` | State-DB und temporäre Restores                  |
| `timeout`         | `30m`                    | pro Job: Restore **plus** alle Checks            |
| `max_age`         | `7d`                     | danach gilt ein Job als `STALE`                  |
| `keep_on_failure` | `true`                   | fehlgeschlagenen Restore liegen lassen           |
| `history_limit`   | `200`                    | behaltene Läufe je Job                           |
| `parallel`        | `1`                      | wie viele Jobs gleichzeitig                      |
| `metrics_file`    | –                        | Pfad für den node_exporter Textfile Collector    |
| `report_file`     | –                        | Pfad für die HTML-Statusseite                    |

Zeiten: `30s`, `45m`, `12h`, `7d`, `1h30m`. Größen: `100KB`, `512KiB`, `2GiB`.

### Secrets

Passwörter gehören nicht in die Config-Datei:

```yaml
passphrase: ${BORG_PASSPHRASE}          # Pflicht — bricht ab, wenn nicht gesetzt
image: ${PG_IMAGE:-postgres:16-alpine}  # optional mit Default
```

Ein fehlendes `${VAR}` ohne Default ist ein Fehler, kein leerer String — ein
Restore mit leerer Passphrase scheitert sonst auf sehr verwirrende Weise.

### Quellen

```yaml
source:
  type: restic
  repository: /mnt/backup/restic     # oder s3:…, sftp:…, rest:…
  password_file: /etc/restore-guard/restic.pass
  snapshot: random                   # latest | oldest | random | <id>
  paths: ["/srv/data"]               # Teil-Restore statt alles
  exclude: ["*.tmp"]
  env: { AWS_ACCESS_KEY_ID: "${AWS_KEY}" }
```

```yaml
source:
  type: borg
  repository: ssh://borg@nas.lan/./repos/ha
  passphrase: ${BORG_PASSPHRASE}
  prefix: nightly                    # nur Archive mit diesem Präfix
```

```yaml
source:
  type: local
  repository: /mnt/backup/photos
  pattern: "backup-*.tar.zst"        # jeder Treffer = ein Snapshot
  mode: auto                         # auto | copy | tar
```

```yaml
source:
  type: zfs
  dataset: rpool/data/vmdata         # Datasetname, kein Pfad
  snapshot: random
  clone_base: rpool/restore-guard    # muss "restore-guard" enthalten
```

Der ZFS-Source kopiert nichts: er legt einen read-only Clone an, mountet ihn aufs
Restore-Verzeichnis und zerstört ihn danach. Damit werden auch mehrere TB in
Sekunden prüfbar. `zfs destroy` ist der einzige destruktive Befehl im ganzen
Programm und läuft nur, wenn drei unabhängige Bedingungen gleichzeitig gelten:
Der Name enthält `restore-guard`, das Dataset hat ein `origin` (ist also
wirklich ein Clone), und es hat keine Kind-Datasets. Schlägt eine davon fehl,
bleibt der Clone stehen und es gibt eine Warnung — verschenkter Plattenplatz ist
billiger als ein falsches `destroy`.

Der ZFS-Source braucht root (oder passende `zfs allow`-Delegation) und ist als
einziger Teil **nicht in der Testsuite mit echtem ZFS erprobt** — die Tests
faken die CLI und prüfen Namensberechnung und Sicherheitscheck. Vor dem
produktiven Einsatz einmal mit einem Wegwerf-Dataset gegenprüfen.

**`snapshot: random` ist der wichtigste Schalter.** Immer nur das neueste
Snapshot zu prüfen beweist, dass letzte Nacht funktioniert hat — nicht, dass die
Retention-Kette intakt ist. Genau die brauchst du, wenn du merkst, dass die
Ransomware schon seit drei Wochen mitgesichert wird.

Ergänzend für restic: `read_data_subset: 2%` lässt vor dem Restore
`restic check --read-data-subset` laufen. Das prüft die Pack-Dateien selbst, also
auch Blobs, die kein wiederhergestelltes Snapshot anfasst. Ein voller
`--read-data`-Lauf ist auf mehreren TB ein Wochenendprojekt; 2% pro Nacht laufen
in einem Monat einmal komplett durch.

### Verifier

<details>
<summary><code>files</code> — strukturell, kostenlos, fängt den häufigsten Fehler</summary>

```yaml
- type: files
  min_files: 50
  min_bytes: 5MB
  newer_than: 36h                    # neueste Datei darf nicht älter sein
  must_exist: ["**/configuration.yaml", "etc/*.conf"]
  must_not_exist: ["**/*.corrupt"]
```
</details>

<details>
<summary><code>sqlite</code> — Home Assistant, *arr, Vaultwarden, Grafana …</summary>

```yaml
- type: sqlite
  path: "**/home-assistant_v2.db"    # Glob relativ zum Restore
  integrity_check: true              # Default
  foreign_key_check: false
  queries:
    - name: recent states
      sql: "SELECT count(*) FROM states"
      expect_min: 100
```
Die Datei wird schreibgeschützt geöffnet (`mode=ro`), damit kein WAL-Replay den
wiederhergestellten Stand verändert.
</details>

<details>
<summary><code>postgres</code> — der stärkste Routine-Check</summary>

```yaml
- type: postgres
  dump: "**/immich*.sql.gz"          # .sql, .sql.gz/.bz2/.xz/.zst oder pg_dump -Fc
  image: postgres:16-alpine
  min_tables: 20
  checks:
    - name: assets present
      sql: "SELECT count(*) FROM assets"
      expect_min: 1000
```
Startet einen Wegwerf-Container, spielt den Dump mit `ON_ERROR_STOP=1` ein und
fragt ab. Ein Dump, der existiert und sauber entpackt, kann trotzdem eine
abgeschnittene Transaktion sein — das merkst du nur beim echten Einspielen.
</details>

<details>
<summary><code>mysql</code> — MySQL und MariaDB</summary>

```yaml
- type: mysql
  dump: "*.sql.gz"                   # .sql oder .sql.gz/.bz2/.xz/.zst
  image: mariadb:11                  # oder mysql:8
  min_tables: 50
  check_tables: true                 # CHECK TABLE über alle Tabellen
  checks:
    - name: users present
      sql: "SELECT count(*) FROM oc_users"
      expect_min: 1
```
`check_tables` ist der Grund, diesen Verifier dem reinen `files`-Check
vorzuziehen: es findet den `mysqldump`, der ohne `--single-transaction` auf
einer belasteten Datenbank lief und Tabellen mitten im Schreiben erwischt hat.
Der Client wird zur Laufzeit gewählt (`mariadb` oder `mysql`), damit beide
Image-Familien funktionieren.
</details>

<details>
<summary><code>http</code> — den echten Dienst gegen die Daten starten</summary>

```yaml
- type: http
  image: vaultwarden/server:latest
  mount: /data                       # Restore wird hierhin gemountet
  subdir: "srv/vaultwarden"          # optional: nur ein Unterordner
  port: 80
  path: /alive
  expect_status: 200
  contains: ["Vaultwarden"]
  read_only: false
  env: { I_REALLY_WANT_VOLATILE_STORAGE: "true" }
```
</details>

<details>
<summary><code>command</code> — alles andere</summary>

```yaml
- type: command
  name: manifest matches
  run: ["sha256sum", "--quiet", "-c", "manifest.sha256"]
  expect_returncode: 0
  stdout_contains: ["OK"]
```
Platzhalter `{restore_dir}`, `{snapshot}`, `{job}` werden ersetzt, dieselben
Werte stehen als `RESTORE_DIR`, `RESTORE_SNAPSHOT`, `RESTORE_JOB` in der
Umgebung. Arbeitsverzeichnis ist der Restore. Für Shell-Syntax `shell: true`.
</details>

### Benachrichtigungen

```yaml
notify:
  ntfy:
    url: https://ntfy.sh/${NTFY_TOPIC}
    on: [failure, recovery]          # failure | recovery | success
    token: ${NTFY_TOKEN}
  webhook:
    url: https://homeassistant.lan/api/webhook/restore-guard
  command:
    run: ["/usr/local/bin/alert.sh"]
```

`recovery` feuert, wenn ein Job nach einem Fehlschlag wieder durchläuft — so
bleibt es bei zwei Nachrichten pro Vorfall statt einer pro Nacht.

## Betrieb

### systemd (empfohlen)

```bash
cp deploy/restore-guard.service deploy/restore-guard.timer /etc/systemd/system/
systemctl enable --now restore-guard.timer
systemctl list-timers restore-guard.timer
journalctl -u restore-guard -n 50
```

Der Timer läuft täglich nachts mit `RandomizedDelaySec`, damit nicht alle Jobs
gleichzeitig auf die NAS eindreschen.

### cron

```cron
30 3 * * * /usr/local/bin/restore-guard -c /etc/restore-guard/config.yml run --quiet
```

Bei `--quiet` gibt es nur bei Problemen Ausgabe — cron mailt dann von selbst.

### Monitoring

```yaml
defaults:
  metrics_file: /var/lib/node_exporter/textfile/restore_guard.prom
```

Exportierte Metriken:

```
restore_guard_last_success_timestamp_seconds{job="immich-db"}
restore_guard_stale{job="immich-db"}
restore_guard_last_run_success{job="immich-db"}
restore_guard_last_run_duration_seconds{job="immich-db"}
restore_guard_last_restore_bytes{job="immich-db"}
```

Die eine Alert-Regel, die zählt:

```yaml
- alert: BackupNotProvenRestorable
  expr: restore_guard_stale == 1
  for: 1h
  annotations:
    summary: "{{ $labels.job }} wurde zu lange nicht erfolgreich wiederhergestellt"
```

### Statusseite

```yaml
defaults:
  report_file: /var/www/homelab/restore-guard.html
```

Schreibt nach jedem Lauf eine fertige HTML-Seite: keine externen Assets, kein
JavaScript, hell/dunkel je nach System, alle fünf Minuten Auto-Refresh. Damit
lässt sie sich in Homer, Dashy oder ein `iframe` einhängen — und rendert auch
dann noch, wenn gerade das halbe Netz steht. Einmalig geht auch
`restore-guard status --html seite.html`.

### Parallel

```yaml
defaults:
  parallel: 3
```

Standard ist seriell, und das ist bei wenigen großen Jobs auch richtig: drei
gleichzeitige Restores von derselben NAS sind langsamer als drei nacheinander.
Bei vielen kleinen Jobs lohnt es sich. `--parallel N` übersteuert die Config für
einen einzelnen Lauf.

## Platzbedarf und Last

Ein Restore braucht kurzzeitig so viel Platz wie die wiederhergestellten Daten.
Bei großen Datasets:

- `paths:` im Source setzen und nur den kritischen Teil wiederherstellen,
- `workdir` auf eine Platte mit Reserve legen (nicht `/`),
- große Jobs seltener laufen lassen (`max_age: 30d` + eigener Timer).

Erfolgreiche Restores werden sofort gelöscht, fehlgeschlagene bleiben unter
`workdir/restores/` zur Analyse liegen (`keep_on_failure: false` schaltet das ab).

## Sicherheitshinweise

- `command`-Verifier führen aus, was in der Config steht — behandle sie wie ein
  root-Cronjob und schütze die Datei (`chmod 640`, root-eigen).
- Tar-Archive werden vor dem Entpacken auf Pfade geprüft, die aus dem
  Zielverzeichnis ausbrechen (`../`).
- Verifier-Pfade können das Restore-Verzeichnis nicht verlassen.
- Der Prozess braucht Lesezugriff aufs Backup-Repository. Wenn du
  Ransomware-Resistenz willst, gib ihm einen **read-only** Zugang (restic
  `--repository` per append-only REST-Server, borg `--append-only` auf der
  Gegenseite).
- Die Postgres-Container laufen ohne Netzwerkfreigabe nach außen; der
  HTTP-Verifier published nur auf `127.0.0.1`.

## Grenzen

- Kein Scheduler an Bord — das macht systemd oder cron besser.
- `postgres`, `mysql` und `http` brauchen einen erreichbaren Docker-Daemon; ohne
  ihn scheitert der Check mit klarer Meldung statt zu crashen.
- Docker-Mounts brauchen Host-Pfade: läuft restore-guard selbst im Container,
  muss `workdir` ein Bind-Mount mit identischem Pfad auf dem Host sein.
- Der `zfs`-Source braucht root und ist nicht gegen echtes ZFS getestet (siehe
  oben) — die anderen Quellen und Verifier sind es.
- Ein grüner Lauf beweist, dass die *Daten* heil sind. Er beweist nicht, dass du
  weißt, in welcher Reihenfolge du deine 14 Dienste wieder hochziehst.

## Entwicklung

```bash
pip install -e ".[dev]"
python3 -m pytest -q
```

Die Tests kommen ohne Docker, restic oder borg aus: sie fahren die komplette
Kette über die `local`-Quelle und die Verifier `files`, `sqlite` und `command`.

Architektur und Designentscheidungen: **[DESIGN.md](DESIGN.md)**.

## Lizenz

MIT
