from sim import simulate_rounds as sim


def test_same_seed_gives_identical_digest():
    cfg = sim.SimConfig()
    a = sim.simulate(cfg, 7)
    b = sim.simulate(cfg, 7)
    assert a.digest() == b.digest()
    assert (a.warns_admin, a.warns_auto, a.cases_opened) == (b.warns_admin, b.warns_auto, b.cases_opened)


def test_different_seeds_differ():
    cfg = sim.SimConfig()
    a = sim.simulate(cfg, 1)
    b = sim.simulate(cfg, 2)
    assert a.digest() != b.digest()


def test_render_is_byte_stable():
    cfg = sim.SimConfig()
    r1 = sim.render(cfg, sim.run(cfg, 50, 1))
    r2 = sim.render(cfg, sim.run(cfg, 50, 1))
    assert r1 == r2


def test_honest_members_open_no_cases():
    cfg = sim.SimConfig(drops_per_day=50, reader_accuracy=1.0)
    r = sim.run(cfg, 200, 3)
    total = sum(x.cases_opened for x in r)
    assert total < 50 * 200 * 0.1, "defaulter rate should stay near the honest engagement rate"


def test_confirmed_sampling_keeps_false_positives_low():
    """Sampling two links can miss an honest member, but confirming with a second
    disjoint sample has to keep that under the 2% target."""
    cfg = sim.SimConfig(bad_report_rate=0.0)
    agg = sim.aggregate(sim.run(cfg, 300, 5))
    assert agg["false_positive_rate"] < 0.02


def test_without_confirmation_false_positives_rise():
    """Shows what the second sample is actually buying us."""
    confirmed = sim.aggregate(sim.run(sim.SimConfig(confirm_sample=True), 300, 5))
    single = sim.aggregate(sim.run(sim.SimConfig(confirm_sample=False), 300, 5))
    assert single["false_positive_rate"] > confirmed["false_positive_rate"]


def test_sampling_catches_most_but_not_all_defaulters():
    cfg = sim.SimConfig()
    agg = sim.aggregate(sim.run(cfg, 300, 5))
    caught = 1.0 - agg["false_negative_rate"]
    assert caught > 0.6, "the checker should still catch the large majority"
    assert caught < 1.0, "sampling can never catch everyone, and pretending otherwise is dishonest"


def test_bad_reports_create_false_positives():
    cfg = sim.SimConfig(bad_report_rate=0.10)
    agg = sim.aggregate(sim.run(cfg, 300, 5))
    assert agg["false_positive_rate"] > 0.0


def test_reader_miss_creates_false_negatives():
    cfg = sim.SimConfig(reader_accuracy=0.95)
    agg = sim.aggregate(sim.run(cfg, 300, 5))
    assert agg["false_negative_rate"] > 0.0


def test_zero_coverage_pushes_all_action_to_auto():
    cfg = sim.SimConfig(admin_cover_minutes_per_day=0)
    agg = sim.aggregate(sim.run(cfg, 200, 9))
    assert agg["warns_admin_per_day"] == 0.0
    assert agg["warns_auto_per_day"] > 0.0


def test_full_coverage_pushes_all_action_to_admin():
    cfg = sim.SimConfig(admin_cover_minutes_per_day=24 * 60)
    agg = sim.aggregate(sim.run(cfg, 200, 9))
    assert agg["warns_auto_per_day"] == 0.0
    assert agg["warns_admin_per_day"] > 0.0


def test_percentile_basic():
    assert sim.percentile([], 0.5) == 0.0
    assert sim.percentile([1, 2, 3, 4], 0.0) == 1
    assert sim.percentile([1, 2, 3, 4], 1.0) == 4
    mid = sim.percentile([1, 2, 3, 4], 0.5)
    assert 2.0 <= mid <= 3.0


def test_digest_changes_with_results():
    cfg = sim.SimConfig()
    r = sim.simulate(cfg, 1)
    before = r.digest()
    r.warns_admin += 1
    assert r.digest() != before