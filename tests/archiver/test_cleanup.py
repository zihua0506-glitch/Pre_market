from datetime import date, timedelta
from pathlib import Path
from archiver.cleanup import old_warm_dirs


def test_old_warm_dirs_picks_only_old(tmp_path: Path):
    today = date.today()
    for n in range(20):
        (tmp_path / (today - timedelta(days=n)).isoformat()).mkdir()
    olds = old_warm_dirs(tmp_path, retention_days=7)
    assert all(int((today - date.fromisoformat(p.name)).days) > 7 for p in olds)
    assert not any(int((today - date.fromisoformat(p.name)).days) <= 7 for p in olds)
