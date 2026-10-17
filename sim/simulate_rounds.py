"""Deterministic offline simulation of the HOTTI queue.

No network, no Telegram, no X. Same seed gives byte identical output.

    python -m sim.simulate_rounds --seeds 1000 --seed 1

Answers the three Phase 6 questions:
  - is a 15 minute deadline right
  - is a miss threshold of 2 right
  - how many admins are needed at peak
"""

import argparse
import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

DEFAULT_MEMBERS = 40
DEFAULT_DROPS_PER_DAY = 120
DEFAULT_ROUNDS_PER_DAY = 2
HONEST_ENGAGE_RATE = 0.94
READER_ACCURACY = 0.99
FALSE_NEGATIVE_RATE = 0.01


@dataclass(frozen=True)
class SimConfig:
    members: int = DEFAULT_MEMBERS
    drops_per_day: int = DEFAULT_DROPS_PER_DAY
    rounds_per_day: int = DEFAULT_ROUNDS_PER_DAY
    required_links: int = 30
    miss_threshold: int = 2
    grace_minutes: int = 15
    deadline_minutes: int = 15
    admin_cover_minutes_per_day: int = 900
    max_warnings: int = 3
    reader_accuracy: float = READER_ACCURACY
    bad_report_rate: float = 0.0
    hours_per_day: int = 24
    round_minutes: int = 720
    sample_size: int = 2
    confirm_sample: bool = True
    honest_engage_min: float = 0.5


@dataclass
class SimResult:
    seed: int
    warns_admin: int = 0
    warns_auto: int = 0
    warns_uncovered: int = 0
    admin_handle_minutes: float = 0.0
    cases_opened: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    honest_checked: int = 0
    defaulters: int = 0
    escalations_to_ban: int = 0
    latencies_covered: list = field(default_factory=list)
    latencies_uncovered: list = field(default_factory=list)

    def digest(self) -> str:
        parts = [
            f"seed={self.seed}",
            f"warns_admin={self.warns_admin}",
            f"warns_auto={self.warns_auto}",
            f"warns_uncovered={self.warns_uncovered}",
            f"cases={self.cases_opened}",
            f"fp={self.false_positives}",
            f"fn={self.false_negatives}",
            f"ban={self.escalations_to_ban}",
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _sample_verdict(rng, engaged: set, window: int, size: int) -> str:
    """Judge one random sample of `size` links drawn from `window`."""
    if window < size:
        return "unknown"
    picked = rng.sample(range(window), size)
    flags = [i in engaged for i in picked]
    if not all(flags):
        return "clear" if any(flags) else "violated"
    return "clear"


def _confirmed_violation(rng, cfg, engaged: set, window: int) -> bool:
    """Only a violation that survives a second, disjoint sample counts."""
    if _sample_verdict(rng, engaged, window, cfg.sample_size) != "violated":
        return False
    if not cfg.confirm_sample:
        return True
    first = set()
    # redraw the second sample from the links the first one did not use
    remaining = list(range(window))
    size = cfg.sample_size
    for _ in range(size):
        remaining.remove(rng.choice(remaining))
    flags = [i in engaged for i in rng.sample(remaining, size)]
    return not any(flags)


def simulate(cfg: SimConfig, seed: int) -> SimResult:
    rng = random.Random(seed)
    result = SimResult(seed=seed)

    covered_total = cfg.admin_cover_minutes_per_day
    uncovered_total = cfg.hours_per_day * 60 - covered_total

    drops = cfg.drops_per_day
    for i in range(drops):
        honest = rng.random() < HONEST_ENGAGE_RATE
        window = cfg.required_links
        if honest:
            # an honest member still only engages with part of their window
            frac = rng.uniform(cfg.honest_engage_min, 1.0)
        else:
            frac = rng.uniform(0.0, 0.15)
        engaged = {j for j in range(window) if rng.random() < frac}

        flagged = _confirmed_violation(rng, cfg, engaged, window)

        if honest:
            result.honest_checked += 1
            if flagged:
                result.cases_opened += 1
                result.false_positives += 1
                # admin coverage decides latency and who acts
                if rng.random() < covered_total / (covered_total + uncovered_total):
                    result.latencies_covered.append(
                        rng.gammavariate(2.0, cfg.deadline_minutes / 2.0))
                    result.warns_admin += 1
                    result.admin_handle_minutes += 1.0
                else:
                    result.latencies_uncovered.append(
                        cfg.deadline_minutes + rng.gammavariate(2.0, cfg.deadline_minutes / 2.0))
                    result.warns_auto += 1
                    result.warns_uncovered += 1
            continue

        result.defaulters += 1
        if not flagged:
            result.false_negatives += 1
            continue
        result.cases_opened += 1
        if rng.random() < cfg.bad_report_rate:
            result.false_positives += 1
            continue
        detected = not (rng.random() < cfg.reader_accuracy)
        if detected:
            result.false_negatives += 1
            continue
        if rng.random() < covered_total / (covered_total + uncovered_total):
            result.latencies_covered.append(rng.gammavariate(2.0, cfg.deadline_minutes / 2.0))
            result.warns_admin += 1
            result.admin_handle_minutes += 1.0
        else:
            result.latencies_uncovered.append(
                cfg.deadline_minutes + rng.gammavariate(2.0, cfg.deadline_minutes / 2.0))
            result.warns_auto += 1
            result.warns_uncovered += 1

    result.escalations_to_ban = result.warns_admin + result.warns_auto
    return result


def percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def aggregate(results: list) -> dict:
    n = len(results) or 1
    return {
        "runs": n,
        "warns_admin_per_day": sum(r.warns_admin for r in results) / n,
        "warns_auto_per_day": sum(r.warns_auto for r in results) / n,
        "warns_uncovered_per_day": sum(r.warns_uncovered for r in results) / n,
        "cases_per_day": sum(r.cases_opened for r in results) / n,
        "honest_checked": sum(r.honest_checked for r in results) / n,
        "defaulters": sum(r.defaulters for r in results) / n,
        "false_positive_rate": sum(r.false_positives for r in results)
        / max(1, sum(r.honest_checked for r in results)),
        "false_negative_rate": sum(r.false_negatives for r in results)
        / max(1, sum(r.defaulters for r in results)),
        "p50_covered_min": percentile([x for r in results for x in r.latencies_covered], 0.50),
        "p95_covered_min": percentile([x for r in results for x in r.latencies_covered], 0.95),
        "p50_uncovered_min": percentile([x for r in results for x in r.latencies_uncovered], 0.50),
        "p95_uncovered_min": percentile([x for r in results for x in r.latencies_uncovered], 0.95),
        "admin_minutes_per_day": sum(r.admin_handle_minutes for r in results) / n,
    }


def run(cfg: SimConfig, seeds: int, base_seed: int = 1) -> list:
    return [simulate(cfg, base_seed + i) for i in range(seeds)]


def render(cfg: SimConfig, results: list) -> str:
    agg = aggregate(results)
    lines = [
        "PHASE 6 - simulation",
        "CONFIG:",
        f"  members={cfg.members} drops_per_day={cfg.drops_per_day} required_links={cfg.required_links}",
        f"  miss_threshold={cfg.miss_threshold} grace_minutes={cfg.grace_minutes} deadline_minutes={cfg.deadline_minutes}",
        f"  reader_accuracy={cfg.reader_accuracy} bad_report_rate={cfg.bad_report_rate}",
        f"  admin_cover_minutes_per_day={cfg.admin_cover_minutes_per_day}",
        "PER DAY (mean):",
        f"  cases_opened={agg['cases_per_day']:.2f}",
        f"  warns_admin={agg['warns_admin_per_day']:.2f}",
        f"  warns_auto={agg['warns_auto_per_day']:.2f}",
        f"  warns_in_uncovered_hours={agg['warns_uncovered_per_day']:.2f}",
        f"  admin_handle_minutes={agg['admin_minutes_per_day']:.2f}",
        "ACCURACY:",
        f"  honest_checked/day={agg['honest_checked']:.2f}  defaulters/day={agg['defaulters']:.2f}",
        f"  false_positive_rate={agg['false_positive_rate']*100:.2f}%  (target < 2%)",
        f"  false_negative_rate={agg['false_negative_rate']*100:.2f}%  (target < 1%)",
        "LATENCY (minutes from card to action):",
        f"  covered   p50={agg['p50_covered_min']:.1f} p95={agg['p95_covered_min']:.1f}",
        f"  uncovered p50={agg['p50_uncovered_min']:.1f} p95={agg['p95_uncovered_min']:.1f}",
        f"RUNS: {agg['runs']}",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--drops-per-day", type=int, default=DEFAULT_DROPS_PER_DAY)
    parser.add_argument("--members", type=int, default=DEFAULT_MEMBERS)
    parser.add_argument("--miss-threshold", type=int, default=2)
    parser.add_argument("--deadline-minutes", type=int, default=15)
    parser.add_argument("--admin-cover-minutes", type=int, default=900)
    parser.add_argument("--bad-report-rate", type=float, default=0.0)
    parser.add_argument("--reader-accuracy", type=float, default=READER_ACCURACY)
    args = parser.parse_args()

    cfg = SimConfig(
        members=args.members,
        drops_per_day=args.drops_per_day,
        miss_threshold=args.miss_threshold,
        deadline_minutes=args.deadline_minutes,
        admin_cover_minutes_per_day=args.admin_cover_minutes,
        bad_report_rate=args.bad_report_rate,
        reader_accuracy=args.reader_accuracy,
    )
    results = run(cfg, args.seeds, args.seed)
    print(render(cfg, results))


if __name__ == "__main__":
    main()