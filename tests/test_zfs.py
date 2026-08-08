"""ZFS source: parsing and — mostly — the guard around `zfs destroy`.

No ZFS here, so the CLI is faked. That is exactly the right level for these
tests: the risky logic is *which dataset name we pass to destroy*, not whether
the kernel module works.
"""

import pytest

from restore_guard.config import ConfigError, build_config
from restore_guard.sources import SourceError, build_source
from restore_guard.sources import zfs as zfs_module
from restore_guard.util import CommandResult, Logger


def build(spec, tmp_path, name="zfs-job"):
    job = {"name": name, "source": spec, "verify": [{"type": "files", "min_files": 1}]}
    config = build_config(
        {"version": 1, "defaults": {"workdir": str(tmp_path)}, "jobs": [job]}
    )
    return build_source(config.jobs[0], Logger(quiet=True))


class FakeZfs:
    """Records every zfs invocation and replays canned answers."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for match, (rc, out) in self.answers.items():
            if match in " ".join(argv):
                return CommandResult(list(argv), rc, out, "" if rc == 0 else out, 0.01)
        return CommandResult(list(argv), 0, "", "", 0.01)

    @property
    def destroyed(self):
        return [c[-1] for c in self.calls if "destroy" in c]


# -- configuration ----------------------------------------------------


def test_dataset_must_not_be_a_path(tmp_path):
    with pytest.raises(ConfigError, match="not a path"):
        build({"type": "zfs", "dataset": "/tank/data"}, tmp_path)


def test_dataset_must_not_be_a_snapshot(tmp_path):
    with pytest.raises(ConfigError, match="not a path or a snapshot"):
        build({"type": "zfs", "dataset": "tank/data@yesterday"}, tmp_path)


def test_clone_base_must_carry_the_marker(tmp_path):
    with pytest.raises(ConfigError, match="safety check"):
        build({"type": "zfs", "dataset": "tank/data", "clone_base": "tank/scratch"}, tmp_path)


def test_default_clone_base_is_derived_from_the_pool(tmp_path):
    source = build({"type": "zfs", "dataset": "tank/data/immich"}, tmp_path)
    assert source._clone_base == "tank/restore-guard"


# -- listing ----------------------------------------------------------


def test_list_snapshots_parses_zfs_output(tmp_path, monkeypatch):
    fake = FakeZfs(
        {
            "list -H -p -t snapshot": (
                0,
                "tank/data@2026-08-06\t1786000000\n"
                "tank/data@2026-08-07\t1786086400\n"
                "tank/data@2026-08-08\t1786172800\n",
            )
        }
    )
    monkeypatch.setattr(zfs_module, "run", fake)
    source = build({"type": "zfs", "dataset": "tank/data"}, tmp_path)

    snapshots = source.list_snapshots()
    assert [s.label for s in snapshots] == ["2026-08-06", "2026-08-07", "2026-08-08"]
    assert snapshots[0].id == "tank/data@2026-08-06"
    assert source.select(snapshots).label == "2026-08-08"


# -- the destroy guard ------------------------------------------------


def test_guard_accepts_a_genuine_clone(monkeypatch):
    fake = FakeZfs({"get -H -p -o value origin": (0, "tank/data@2026-08-08\n")})
    monkeypatch.setattr(zfs_module, "run", fake)
    zfs_module._assert_safe_to_destroy("zfs", "tank/restore-guard/job-123")


def test_guard_rejects_names_without_the_marker(monkeypatch):
    monkeypatch.setattr(zfs_module, "run", FakeZfs({}))
    with pytest.raises(SourceError, match="does not contain"):
        zfs_module._assert_safe_to_destroy("zfs", "tank/production")


def test_guard_rejects_datasets_that_are_not_clones(monkeypatch):
    fake = FakeZfs({"get -H -p -o value origin": (0, "-\n")})
    monkeypatch.setattr(zfs_module, "run", fake)
    with pytest.raises(SourceError, match="not a clone"):
        zfs_module._assert_safe_to_destroy("zfs", "tank/restore-guard/real-data")


def test_guard_rejects_datasets_with_children(monkeypatch):
    fake = FakeZfs(
        {
            "get -H -p -o value origin": (0, "tank/data@snap\n"),
            "list -H -o name -r": (
                0,
                "tank/restore-guard/job\ntank/restore-guard/job/child\n",
            ),
        }
    )
    monkeypatch.setattr(zfs_module, "run", fake)
    with pytest.raises(SourceError, match="descendant"):
        zfs_module._assert_safe_to_destroy("zfs", "tank/restore-guard/job")


def test_cleanup_destroys_only_after_the_guard_passes(tmp_path, monkeypatch):
    fake = FakeZfs({"get -H -p -o value origin": (0, "tank/data@2026-08-08\n")})
    monkeypatch.setattr(zfs_module, "run", fake)
    source = build({"type": "zfs", "dataset": "tank/data"}, tmp_path)
    source._clone = "tank/restore-guard/zfs-job-1786172800"

    source.cleanup(tmp_path, succeeded=True)
    assert fake.destroyed == ["tank/restore-guard/zfs-job-1786172800"]
    assert source._clone is None


def test_cleanup_refuses_when_the_guard_fails(tmp_path, monkeypatch):
    fake = FakeZfs({"get -H -p -o value origin": (0, "-\n")})
    monkeypatch.setattr(zfs_module, "run", fake)
    source = build({"type": "zfs", "dataset": "tank/data"}, tmp_path)
    source._clone = "tank/restore-guard/suspicious"

    source.cleanup(tmp_path, succeeded=True)
    assert fake.destroyed == []  # leaked disk beats a wrong destroy
    assert source._clone == "tank/restore-guard/suspicious"


def test_source_declares_that_it_manages_the_destination(tmp_path):
    source = build({"type": "zfs", "dataset": "tank/data"}, tmp_path)
    assert source.manages_destination is True
