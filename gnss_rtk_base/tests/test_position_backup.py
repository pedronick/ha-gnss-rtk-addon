import position_backup


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "backup.json"
    saved = position_backup.save(45.1234567, 9.7654321, 123.456, "ppp", "unicore_um98x",
                                  path=path, duration_hours=6, num_raw_files=24)

    loaded = position_backup.load(path=path)
    assert loaded == saved
    assert loaded["lat"] == 45.1234567
    assert loaded["method"] == "ppp"
    assert loaded["duration_hours"] == 6
    assert loaded["num_raw_files"] == 24
    assert "computed_at" in loaded


def test_load_returns_none_if_missing(tmp_path):
    assert position_backup.load(path=tmp_path / "does_not_exist.json") is None


def test_load_returns_none_on_corrupt_file(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    assert position_backup.load(path=path) is None


def test_default_path_used_when_monkeypatched(tmp_path, monkeypatch):
    fake_default = tmp_path / "default_backup.json"
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", fake_default)

    position_backup.save(1.0, 2.0, 3.0, "manual", "ublox_zedf9p")
    assert fake_default.exists()
    assert position_backup.load()["method"] == "manual"
