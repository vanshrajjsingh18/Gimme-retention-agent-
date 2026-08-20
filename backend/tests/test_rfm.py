from __future__ import annotations

from app.rfm.engine import RfmInput, classify_rfm_segment, score_population


def make_population(n: int = 100) -> list[RfmInput]:
    """Spread customers evenly across recency / frequency / monetary."""
    return [
        RfmInput(
            customer_id=i,
            recency_days=i * 4,  # 0..396 days
            frequency=1 + (i % 20),
            monetary=25.0 * (1 + (i % 20)),
        )
        for i in range(n)
    ]


def test_scores_within_one_to_five():
    for r in score_population(make_population()):
        assert 1 <= r.recency_score <= 5
        assert 1 <= r.frequency_score <= 5
        assert 1 <= r.monetary_score <= 5
        assert r.rfm_total == r.recency_score + r.frequency_score + r.monetary_score
        assert r.rfm_cell == f"{r.recency_score}{r.frequency_score}{r.monetary_score}"


def test_recency_is_inverted():
    pop = make_population()
    results = {r.customer_id: r for r in score_population(pop)}
    # customer 0 ordered today, customer 99 ordered ~396 days ago
    assert results[0].recency_score > results[99].recency_score
    assert results[0].recency_score == 5
    assert results[99].recency_score == 1


def test_frequency_and_monetary_ascend():
    pop = [
        RfmInput(customer_id=1, recency_days=10, frequency=1, monetary=20.0),
        RfmInput(customer_id=2, recency_days=10, frequency=25, monetary=3000.0),
    ]
    results = {r.customer_id: r for r in score_population(pop)}
    assert results[2].frequency_score > results[1].frequency_score
    assert results[2].monetary_score > results[1].monetary_score


def test_small_population_uses_absolute_fallback():
    pop = [
        RfmInput(customer_id=1, recency_days=5, frequency=12, monetary=2000.0),
        RfmInput(customer_id=2, recency_days=300, frequency=1, monetary=30.0),
    ]
    results = {r.customer_id: r for r in score_population(pop)}
    assert results[1].rfm_cell == "555"
    assert results[2].rfm_cell == "111"


def test_flat_population_does_not_crash():
    """Identical customers produce degenerate quantiles; fallback must engage."""
    pop = [RfmInput(customer_id=i, recency_days=30, frequency=3, monetary=150.0) for i in range(50)]
    results = score_population(pop)
    assert len(results) == 50
    assert len({r.rfm_cell for r in results}) == 1


def test_customer_without_orders_scores_floor():
    pop = [RfmInput(customer_id=1, recency_days=None, frequency=0, monetary=0.0)]
    r = score_population(pop)[0]
    assert r.rfm_cell == "111"
    assert r.rfm_segment == "Prospects"


def test_no_order_customers_do_not_skew_quantiles():
    """Zero-order customers must not drag the distribution for real buyers."""
    real = make_population(60)
    with_ghosts = real + [
        RfmInput(customer_id=1000 + i, recency_days=None, frequency=0, monetary=0.0)
        for i in range(60)
    ]
    a = {r.customer_id: r.rfm_cell for r in score_population(real)}
    b = {r.customer_id: r.rfm_cell for r in score_population(with_ghosts)}
    assert all(a[cid] == b[cid] for cid in a)


def test_champions_and_lost_segments():
    assert classify_rfm_segment(5, 5, 5) == "Champions"
    assert classify_rfm_segment(1, 1, 1) == "Lost"


def test_high_value_lapsed_customer_flagged_cannot_lose():
    assert classify_rfm_segment(1, 5, 5) == "Cannot Lose Them"


def test_every_score_combination_maps_to_a_segment():
    seen = set()
    for r in range(1, 6):
        for f in range(1, 6):
            for m in range(1, 6):
                seg = classify_rfm_segment(r, f, m)
                assert isinstance(seg, str) and seg
                seen.add(seg)
    assert len(seen) >= 6


def test_results_returned_for_every_input():
    pop = make_population(37)
    assert len(score_population(pop)) == 37
