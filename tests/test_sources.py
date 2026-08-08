import pytest

from restore_guard.config import build_config
from restore_guard.sources import SourceError, build_source
from restore_guard.util import Logger


def build(job_source, workdir, **job_overrides):
    job = {
        "name": "demo",
        "source": job_source,
        "verify": [{"type": "files", "min_files": 1}],
    }
    job.update(job_overrides)
    config = build_config(
        {"version": 1, "defaults": {"workdir": str(workdir)}, "jobs": [job]}
    )
    return build_source(config.jobs[0], Logger(quiet=True))


def test_local_lists_snapshots_in_time_order(backup_dir, tmp_path):
    source = build({"type": "local", "repository": str(backup_dir)}, tmp_path)
    snapshots = source.list_snapshots()
    assert [s.id for s in snapshots] == [
        "snap-2026-08-06",
        "snap-2026-08-07",
        "snap-2026-08-08",
    ]


def test_selection_strategies(backup_dir, tmp_path):
    latest = build({"type": "local", "repository": str(backup_dir)}, tmp_path)
    oldest = build(
        {"type": "local", "repository": str(backup_dir), "snapshot": "oldest"}, tmp_path
    )
    snapshots = latest.list_snapshots()
    assert latest.select(snapshots).id == "snap-2026-08-08"
    assert oldest.select(snapshots).id == "snap-2026-08-06"

    pinned = build(
        {"type": "local", "repository": str(backup_dir), "snapshot": "snap-2026-08-07"},
        tmp_path,
    )
    assert pinned.select(snapshots).id == "snap-2026-08-07"


def test_random_selection_stays_inside_the_repository(backup_dir, tmp_path):
    source = build(
        {"type": "local", "repository": str(backup_dir), "snapshot": "random"}, tmp_path
    )
    snapshots = source.list_snapshots()
    picked = {source.select(snapshots).id for _ in range(30)}
    assert picked <= {s.id for s in snapshots}
    assert len(picked) > 1  # actually random, not always the same one


def test_unknown_snapshot_name_is_an_error(backup_dir, tmp_path):
    source = build(
        {"type": "local", "repository": str(backup_dir), "snapshot": "nope"}, tmp_path
    )
    with pytest.raises(SourceError, match="no snapshot matching"):
        source.select(source.list_snapshots())


def test_pattern_filters_snapshots(tar_backup_dir, tmp_path):
    source = build(
        {"type": "local", "repository": str(tar_backup_dir), "pattern": "app-*.tar.gz"},
        tmp_path,
    )
    assert [s.id for s in source.list_snapshots()] == ["app-2026-08-08.tar.gz"]


def test_restore_copies_directory(backup_dir, tmp_path):
    source = build({"type": "local", "repository": str(backup_dir)}, tmp_path)
    dest = tmp_path / "restore"
    dest.mkdir()
    snapshot = source.select(source.list_snapshots())
    source.restore(snapshot, dest, timeout=60)
    assert (dest / "etc" / "config.yaml").read_text() == "generation: 2\n"


def test_restore_extracts_tarball(tar_backup_dir, tmp_path):
    source = build({"type": "local", "repository": str(tar_backup_dir)}, tmp_path)
    dest = tmp_path / "restore"
    dest.mkdir()
    snapshot = source.select(source.list_snapshots())
    source.restore(snapshot, dest, timeout=60)
    assert (dest / "app" / "settings.json").read_text() == '{"ok": true}'


def test_missing_repository_fails_preflight(tmp_path):
    source = build({"type": "local", "repository": str(tmp_path / "gone")}, tmp_path)
    with pytest.raises(SourceError, match="does not exist"):
        source.preflight()
