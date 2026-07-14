from pathlib import Path

from combo_arb.backtest.replay import (
    generate_sample_frames,
    load_frames,
    replay,
)
from combo_arb.persistence.db import Database


def test_gen_sample_roundtrip(tmp_path, cfg):
    path = tmp_path / "frames.jsonl"
    generate_sample_frames(path, n=50, seed=3)
    frames = load_frames(path)
    assert len(frames) == 50
    assert "legs" in frames[0] and "rfqs" in frames[0]


def test_replay_report(tmp_path, cfg):
    path = tmp_path / "frames.jsonl"
    generate_sample_frames(path, n=150, seed=3)
    report = replay(path, cfg)
    assert report.n_frames == 150
    assert report.n_signals > 0
    assert report.n_executed > 0
    assert 0.0 <= report.fill_rate <= 1.0
    assert report.total_expected_pnl > 0  # overpriced combos are net positive edge


def test_replay_persists_to_db(tmp_path, cfg):
    path = tmp_path / "frames.jsonl"
    generate_sample_frames(path, n=60, seed=5)
    db = Database(str(tmp_path / "t.db"))
    replay(path, cfg, db=db)
    counts = db.counts()
    assert counts["arb_signals"] > 0
    assert counts["fills"] > 0
    assert counts["pnl"] > 0
    db.close()


def test_replay_empty_file(tmp_path, cfg):
    path = tmp_path / "empty.jsonl"
    Path(path).write_text("")
    report = replay(path, cfg)
    assert report.n_frames == 0
