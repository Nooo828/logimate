import pytest

from scripts.recover_docker_runtime import quarantine


def test_preview_does_not_modify(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    (source / "socket").write_bytes(b"preserve")
    plan = quarantine(source, tmp_path, {"socket"}, "test")
    assert not plan["applied"]
    assert (source / "socket").read_bytes() == b"preserve"
    assert not (tmp_path / "run.backup-test").exists()


def test_rename_preserves_backup_and_recreates_empty_runtime(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    (source / "socket").write_bytes(b"preserve")
    plan = quarantine(source, tmp_path, {"socket"}, "test", apply=True)
    assert plan["applied"]
    assert list(source.iterdir()) == []
    assert (tmp_path / "run.backup-test/socket").read_bytes() == b"preserve"


def test_unexpected_contents_block_mutation(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    (source / "important.txt").write_text("keep")
    with pytest.raises(ValueError, match="Unexpected"):
        quarantine(source, tmp_path, {"socket"}, "test", apply=True)
    assert (source / "important.txt").read_text() == "keep"


def test_wrong_parent_is_rejected(tmp_path):
    source = tmp_path / "other/run"
    source.mkdir(parents=True)
    with pytest.raises(ValueError, match="parent mismatch"):
        quarantine(source, tmp_path, set(), "test", apply=True)
    assert source.is_dir()


def test_existing_backup_is_not_overwritten(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    (tmp_path / "run.backup-test").mkdir()
    with pytest.raises(ValueError, match="existing backup"):
        quarantine(source, tmp_path, set(), "test", apply=True)
    assert source.is_dir()
