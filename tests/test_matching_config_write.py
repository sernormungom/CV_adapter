"""Config replacement failures must preserve the consultant's usable config."""
import copy
from unittest.mock import AsyncMock

import pytest
import yaml

from backend.matching import config_reader
from backend.opportunity_pipeline.learning_module import config_bootstrapper
from tests.test_matching_config import _VALID


@pytest.fixture
def configs(tmp_path, monkeypatch):
    monkeypatch.setattr(config_reader, "DATA_DIR", tmp_path)
    (tmp_path / "alice").mkdir()
    cfg = copy.deepcopy(_VALID)
    cfg.update(metadata={}, exploration={"slots": 1})
    config_reader.save_config("alice", cfg)
    return tmp_path / "alice", cfg


def test_successful_regeneration_preserves_previous_valid_config(configs):
    directory, cfg = configs
    previous = (directory / "matching_config.yaml").read_bytes()
    cfg["thresholds"]["keep"] = 90
    config_reader.save_config("alice", cfg)
    assert (directory / "matching_config.backup.yaml").read_bytes() == previous
    assert config_reader.load_config("alice")["thresholds"]["keep"] == 90


def test_invalid_replacement_leaves_current_and_backup_unchanged(configs):
    directory, cfg = configs
    config_reader.save_config("alice", cfg)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError):
        config_reader.save_config("alice", {"thresholds": []})
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_failed_rename_preserves_current_and_removes_temporary_file(configs, monkeypatch):
    directory, cfg = configs
    before = (directory / "matching_config.yaml").read_bytes()
    def fail(*args):
        raise OSError("simulated filesystem failure")
    monkeypatch.setattr(config_reader.os, "replace", fail)
    with pytest.raises(OSError):
        config_reader.save_config("alice", cfg, make_backup=False)
    assert (directory / "matching_config.yaml").read_bytes() == before
    assert list(directory.glob("*.tmp")) == []


def test_corrupt_current_does_not_destroy_valid_backup(configs):
    directory, cfg = configs
    config_reader.save_config("alice", cfg)
    previous = (directory / "matching_config.backup.yaml").read_bytes()
    (directory / "matching_config.yaml").write_text("[broken yaml")
    config_reader.save_config("alice", cfg)
    assert (directory / "matching_config.backup.yaml").read_bytes() == previous


@pytest.mark.parametrize("failure", [RuntimeError("generation failed"), {"schema_version": "1.0"}])
def test_failed_generation_preserves_valid_config(configs, monkeypatch, failure):
    directory, cfg = configs
    before = (directory / "matching_config.yaml").read_bytes()
    monkeypatch.setattr(config_bootstrapper, "load_profile", lambda _: {})
    monkeypatch.setattr(config_bootstrapper, "profile_to_yaml", lambda _: "identity: {}")
    generate = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    monkeypatch.setattr(config_bootstrapper, "_generate_config", generate)
    with pytest.raises((RuntimeError, ValueError)):
        config_bootstrapper.bootstrap_config("alice")
    assert (directory / "matching_config.yaml").read_bytes() == before


def test_bootstrap_regeneration_keeps_backup(configs, monkeypatch):
    directory, cfg = configs
    before = (directory / "matching_config.yaml").read_bytes()
    monkeypatch.setattr(config_bootstrapper, "load_profile", lambda _: {})
    monkeypatch.setattr(config_bootstrapper, "profile_to_yaml", lambda _: "identity: {}")
    monkeypatch.setattr(config_bootstrapper, "_generate_config", AsyncMock(return_value=cfg))
    config_bootstrapper.bootstrap_config("alice")
    assert (directory / "matching_config.backup.yaml").read_bytes() == before
    assert yaml.safe_load((directory / "matching_config.yaml").read_text())["metadata"]["source"] == "bootstrap"
