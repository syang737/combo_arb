from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import ComboLeg, ComboRFQ
from combo_arb.orchestration.controller import Controller
from combo_arb.persistence.db import Database
from combo_arb.scanner.scanner import Scanner


def _rfq(rid: str, quote: float) -> ComboRFQ:
    return ComboRFQ(
        rfq_id=rid,
        mve_collection_ticker="COMBO_" + rid,
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        quote_yes=quote,
        size=20,
    )


def test_every_combo_is_evaluated(cfg, legs):
    # fair ~0.20: under=flag, mid=near-miss, far=well below (buy direction).
    rfqs = [_rfq("under", 0.10), _rfq("mid", 0.18), _rfq("far", 0.35)]
    scanner = Scanner(MockKalshiClient(leg_prices=legs, rfqs=rfqs), cfg)
    signals = scanner.scan()

    assert len(scanner.last_evaluations) == 3   # recorded for every priceable combo
    assert len(signals) == 1                    # only the underpriced one flags

    by = {e.rfq_id: e for e in scanner.last_evaluations}
    assert by["under"].flagged is True
    assert by["mid"].flagged is False and by["far"].flagged is False
    # Edge ranks: cheaper combo = better buy edge.
    assert (by["under"].arbitrage_margin
            > by["mid"].arbitrage_margin
            > by["far"].arbitrage_margin)
    # gap_to_flag >= 0 iff flagged.
    assert by["under"].gap_to_flag >= 0 and by["mid"].gap_to_flag < 0


def test_near_misses_persisted_far_ones_skipped(cfg, legs, tmp_path):
    cfg.thresholds.near_miss_band = 0.05
    rfqs = [_rfq("under", 0.10), _rfq("mid", 0.18), _rfq("far", 0.35)]
    db = Database(str(tmp_path / "eval.db"))
    Controller(cfg, MockKalshiClient(leg_prices=legs, rfqs=rfqs), db=db).run_once()

    ids = {r["rfq_id"] for r in
           db.conn.execute("SELECT rfq_id FROM combo_evaluations").fetchall()}
    assert "under" in ids   # flagged -> persisted
    assert "mid" in ids     # near miss -> persisted
    assert "far" not in ids # well outside the band -> skipped
    db.close()
