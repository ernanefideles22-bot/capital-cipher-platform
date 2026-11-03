"""Exploratory dependent-series uncertainty and whole-family Holm correction.

Stationary bootstrap follows Politis/Romano's circular, geometric-block scheme.
It is an asymptotic diagnostic conditional on stationarity/weak dependence, not
an exact market p-value or White's global Reality Check implementation.
"""
from __future__ import annotations

import math
import random
from statistics import fmean

BLOCK_LENGTHS = (8, 16, 32)
REPLICATIONS = 9999
SEED = 20261103
ALPHA = .05


def holm_adjust(p_values):
    """Holm step-down adjusted p-values; no independence assumption across tests."""
    if not p_values or len(p_values) > 648:
        raise ValueError("Unsupported hypothesis-family budget")
    if any(type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1 for p in p_values):
        raise ValueError("Invalid marginal p-value")
    order = sorted(range(len(p_values)), key=lambda i: (p_values[i], i))
    output, running = [1.] * len(order), 0.
    for rank, index in enumerate(order):
        running = max(running, min(1., (len(order) - rank) * p_values[index]))
        output[index] = running
    return output


def _circular_sum(prefix, start, length):
    n = len(prefix) - 1
    end = start + length
    if end <= n:
        return prefix[end] - prefix[start]
    return prefix[n] - prefix[start] + prefix[end - n]


def bootstrap_means(folds, *, block_length, replications, seed):
    """Resample each validation fold separately, never bridging its embargo gap.

    Geometric chunks plus prefix sums are equivalent to sampling every circular
    index individually, but preserve a strict CPU/replication budget.
    """
    if (type(block_length) is not int or not 1 <= block_length <= 64
            or type(replications) is not int or not 99 <= replications <= REPLICATIONS
            or type(seed) is not int or len(folds) != 2
            or any(not 64 <= len(fold) <= 1024 for fold in folds)
            or any(not math.isfinite(x) for fold in folds for x in fold)):
        raise ValueError("Unsupported bootstrap evidence/budget")
    prefixes = []
    for fold in folds:
        prefix = [0.]
        for value in fold:
            prefix.append(prefix[-1] + value)
        prefixes.append(prefix)
    rng = random.Random(seed)
    probability = 1 / block_length
    log_tail = math.log1p(-probability) if block_length > 1 else None
    total = sum(map(len, folds))
    output = []
    for _ in range(replications):
        value = 0.
        for prefix in prefixes:
            n = len(prefix) - 1
            remaining = n
            while remaining:
                start = rng.randrange(n)
                length = (1 if log_tail is None else
                          1 + int(math.log1p(-rng.random()) / log_tail))
                length = min(length, remaining)
                value += _circular_sum(prefix, start, length)
                remaining -= length
        output.append(value / total)
    return output


def mean_diagnostic(folds, *, replications=REPLICATIONS, seed=SEED):
    """Least-favorable centered bootstrap for H0: mean net excess <= 0.

    Use the largest p-value / lowest lower bound across predeclared block lengths.
    Zero/nonpositive evidence gets conservative p=1 rather than a discovery.
    Confidence bounds are pointwise, not simultaneous family intervals.
    """
    if len(folds) != 2 or any(not f or any(not math.isfinite(x) for x in f) for f in folds):
        raise ValueError("Two finite validation folds required")
    mean = fmean(x for fold in folds for x in fold)
    base = {"mean_net_excess_per_bar": mean, "marginal_p_value": 1.,
            "lower_pointwise_95_mean": None, "sensitivity": [],
            "stationarity_and_mixing_verified": False,
            "exact_finite_sample_test": False}
    if mean <= 0:
        return {**base, "status": "NONPOSITIVE_MEAN_NO_DISCOVERY"}
    if min(map(len, folds)) < 256:
        return {**base, "status": "INSUFFICIENT_TIME_BLOCKS"}
    if all(max(fold) == min(fold) for fold in folds):
        return {**base, "status": "DEGENERATE_TIME_SERIES"}
    sensitivity = []
    centered = []
    for fold in folds:
        center = fmean(fold)
        centered.append(tuple(x - center for x in fold))
    for length in BLOCK_LENGTHS:
        # Center independently by fold, preserving each fold's null mean.
        draws = bootstrap_means(centered, block_length=length,
                                replications=replications, seed=seed + length)
        p = (1 + sum(x >= mean for x in draws)) / (1 + replications)
        draws.sort()
        quantile = draws[min(len(draws) - 1, math.ceil(.95 * len(draws)) - 1)]
        sensitivity.append({"expected_block_length": length,
                            "marginal_p_value": p,
                            "lower_pointwise_95_mean": mean - quantile})
    return {**base, "status": "EXPLORATORY_BOOTSTRAP_ONLY", "sensitivity": sensitivity,
            "marginal_p_value": max(v["marginal_p_value"] for v in sensitivity),
            "lower_pointwise_95_mean": min(v["lower_pointwise_95_mean"] for v in sensitivity),
            "replications_each_block_length": replications}
