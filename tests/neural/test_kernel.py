"""Kernel tests on tiny synthetic graphs."""

from __future__ import annotations

import math

import numpy as np
import pytest

from openfly.neural import kernel as K
from openfly.neural.kernel import Kernel, KernelState


def make_kernel(n, edges=(), is_kc=None, modulatory=None, rest=None):
    """edges: iterable of (pre, post, weight_mv)."""
    edges = sorted(edges)
    pre = np.array([e[0] for e in edges], dtype=np.int64)
    post = np.array([e[1] for e in edges], dtype=np.int32)
    weight = np.array([e[2] for e in edges], dtype=np.float32)
    ptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(pre, minlength=n), out=ptr[1:])
    is_kc = np.zeros(n, np.uint8) if is_kc is None else np.asarray(is_kc, np.uint8)
    modulatory = np.zeros(n, np.uint8) if modulatory is None else np.asarray(modulatory, np.uint8)
    return Kernel(ptr, post, weight, modulatory, is_kc, rest=rest)


def step_until_spike(k, counts, neuron, max_steps=5000):
    """Run one step at a time; return the step at which `neuron` first spikes."""
    for _ in range(max_steps):
        before = counts[neuron]
        k.run(1, counts)
        if counts[neuron] > before:
            return k.clock
    raise AssertionError("no spike")


def analytic_first_spike_ms(drive, rest=K.V_REST_MV):
    gap = K.V_THRESH_MV - rest
    return -K.TAU_M_MS * math.log(1.0 - gap / drive)


def test_first_spike_time_matches_analytic_solution():
    for drive in (8.0, 12.0, 20.0):
        k = make_kernel(1)
        k.set_drive(np.array([drive]))
        counts = np.zeros(1, np.int32)
        step = step_until_spike(k, counts, 0)
        expected = analytic_first_spike_ms(drive) / K.DT_MS
        # v > threshold strictly, so the spike is at the first step after the crossing.
        assert step == math.floor(expected) + 1


def test_subthreshold_drive_never_spikes_and_voltage_is_exact():
    k = make_kernel(1)
    k.set_drive(np.array([6.0]))
    counts = np.zeros(1, np.int32)
    k.run(500, counts)
    assert counts[0] == 0
    t = 500 * K.DT_MS
    expected = K.V_REST_MV + 6.0 * (1.0 - math.exp(-t / K.TAU_M_MS))
    assert k.state.v[0] == pytest.approx(expected, abs=1e-9)


def test_spike_arrives_after_delay_and_raises_g_by_weight():
    k = make_kernel(2, edges=[(0, 1, 5.0)])
    k.set_drive(np.array([12.0, 0.0]))
    counts = np.zeros(2, np.int32)
    s = step_until_spike(k, counts, 0)
    for _ in range(K.DELAY_STEPS - 1):
        k.run(1, counts)
        assert k.state.g[1] == 0.0
    k.run(1, counts)
    assert k.clock == s + K.DELAY_STEPS
    assert k.state.g[1] == pytest.approx(5.0)
    assert k.state.v[1] == pytest.approx(K.V_REST_MV)
    k.run(50, counts)
    assert k.state.v[1] > K.V_REST_MV
    assert 0.0 < k.state.g[1] < 5.0


def test_inhibitory_weight_lowers_voltage():
    k = make_kernel(2, edges=[(0, 1, -5.0)])
    k.set_drive(np.array([12.0, 0.0]))
    counts = np.zeros(2, np.int32)
    s = step_until_spike(k, counts, 0)
    k.run(K.DELAY_STEPS + 50, counts)
    assert k.clock == s + K.DELAY_STEPS + 50
    assert k.state.v[1] < K.V_REST_MV
    assert counts[1] == 0


def test_strong_input_makes_target_spike():
    k = make_kernel(2, edges=[(0, 1, 30.0)])
    k.set_drive(np.array([12.0, 0.0]))
    counts = np.zeros(2, np.int32)
    k.run(600, counts)
    assert counts[0] >= 3
    assert counts[1] >= 1


def test_refractory_blocks_input():
    # Both neurons spike at the same step; the spike from 0 arrives at 1 while 1 is refractory.
    k = make_kernel(2, edges=[(0, 1, 5.0)])
    k.set_drive(np.array([12.0, 12.0]))
    counts = np.zeros(2, np.int32)
    s = step_until_spike(k, counts, 0)
    assert counts[1] == 1
    k.run(K.DELAY_STEPS, counts)
    assert k.clock == s + K.DELAY_STEPS < s + K.REFRACTORY_STEPS
    assert k.state.g[1] == 0.0
    # Control: an idle target receives the same spike.
    k2 = make_kernel(2, edges=[(0, 1, 5.0)])
    k2.set_drive(np.array([12.0, 0.0]))
    counts2 = np.zeros(2, np.int32)
    step_until_spike(k2, counts2, 0)
    k2.run(K.DELAY_STEPS, counts2)
    assert k2.state.g[1] == pytest.approx(5.0)


def test_refractory_blocks_spiking_for_22_steps():
    k = make_kernel(1)
    k.set_drive(np.array([200.0]))  # would cross threshold within a step
    counts = np.zeros(1, np.int32)
    s1 = step_until_spike(k, counts, 0)
    s2 = step_until_spike(k, counts, 0)
    assert s2 - s1 == K.REFRACTORY_STEPS


def test_kc_adaptation_slows_repeated_firing():
    rest = np.array([K.V_REST_KC_MV, K.V_REST_KC_MV])
    k = make_kernel(2, is_kc=[1, 0], rest=rest)
    k.set_drive(np.array([25.0, 25.0]))
    counts = np.zeros(2, np.int32)
    k.run(5000, counts)
    assert counts[0] < counts[1]
    assert counts[0] > 0
    assert k.state.adapt[0] > 0.0
    assert k.state.adapt[1] == 0.0


def test_modulatory_neuron_delivers_nothing():
    k = make_kernel(2, edges=[(0, 1, 30.0)], modulatory=[1, 0])
    k.set_drive(np.array([12.0, 0.0]))
    counts = np.zeros(2, np.int32)
    k.run(1000, counts)
    assert counts[0] > 0
    assert counts[1] == 0
    assert k.state.g[1] == 0.0


def test_sleeping_neuron_with_zero_drive_never_fires_or_activates():
    k = make_kernel(3, edges=[(0, 1, 2.0)])
    k.set_drive(np.array([0.0, 0.0, 0.0]))
    counts = np.zeros(3, np.int32)
    k.run(2000, counts)
    assert counts.sum() == 0
    assert k.state.active_count == 0
    np.testing.assert_allclose(k.state.v, K.V_REST_MV)


def test_lazy_evolution_is_exact_for_sleepers():
    # A sleeper that received a sub-threshold input decays exactly like the closed form.
    k = make_kernel(2, edges=[(0, 1, 3.0)])
    k.set_drive(np.array([12.0, 0.0]))
    counts = np.zeros(2, np.int32)
    step_until_spike(k, counts, 0)
    k.run(K.DELAY_STEPS, counts)
    assert k.state.g[1] == pytest.approx(3.0)
    assert k.state.active_pos[1] < 0  # 3 mV cannot reach the 7 mV gap: stays asleep
    k.set_drive(np.array([0.0, 0.0]))  # silence the source so no further input arrives
    d = 300
    k.run(d, counts)
    t = d * K.DT_MS
    a = math.exp(-t / K.TAU_M_MS)
    b = math.exp(-t / K.TAU_S_MS)
    expected_v = K.V_REST_MV + 3.0 * (a - b) * K.G_FACTOR
    assert k.state.v[1] == pytest.approx(expected_v, abs=1e-9)
    assert k.state.g[1] == pytest.approx(3.0 * b, abs=1e-9)
    assert counts[1] == 0


def random_graph(seed, n=200, density=0.05):
    rng = np.random.default_rng(seed)
    mask = rng.random((n, n)) < density
    w = np.where(mask, rng.normal(0.0, 6.0, (n, n)), 0.0)
    is_kc = (rng.random(n) < 0.2).astype(np.uint8)
    modulatory = (rng.random(n) < 0.05).astype(np.uint8)
    drive = np.where(rng.random(n) < 0.3, rng.uniform(5.0, 15.0, n), 0.0)
    pre, post = np.nonzero(w)
    edges = [(int(p), int(q), float(np.float32(w[p, q]))) for p, q in zip(pre, post, strict=True)]
    return n, edges, is_kc, modulatory, drive, w.astype(np.float32).astype(np.float64)


def test_two_runs_with_the_same_input_are_bit_identical():
    results = []
    for _ in range(2):
        n, edges, is_kc, modulatory, drive, _w = random_graph(7)
        k = make_kernel(n, edges, is_kc=is_kc, modulatory=modulatory)
        counts = np.zeros(n, np.int32)
        k.set_drive(drive)
        k.run(1500, counts)
        k.set_drive(drive * 0.7 + 2.0)
        k.run(1500, counts)
        results.append((counts.copy(), k.state.v.copy(), k.state.g.copy(), k.state.adapt.copy()))
    for a, b in zip(results[0], results[1], strict=True):
        assert np.array_equal(a, b)


def test_matches_brute_force_reference():
    n, edges, is_kc, modulatory, drive0, W = random_graph(11)
    k = make_kernel(n, edges, is_kc=is_kc, modulatory=modulatory)
    steps, chunk = 2000, 100
    counts = np.zeros(n, np.int32)
    lazy = []
    drive = drive0.copy()
    for c in range(steps // chunk):
        if c == 8:
            drive = drive * 0.5 + 3.0
        k.set_drive(drive)
        before = counts.copy()
        k.run(chunk, counts)
        lazy.append(counts - before)
    lazy = np.array(lazy)

    rest = np.where(is_kc, K.V_REST_KC_MV, K.V_REST_MV).astype(np.float64)
    a1 = math.exp(-K.DT_MS / K.TAU_M_MS)
    b1 = math.exp(-K.DT_MS / K.TAU_S_MS)
    c1 = math.exp(-K.DT_MS / K.TAU_A_MS)
    v = rest.copy()
    g = np.zeros(n)
    A = np.zeros(n)
    refrac = np.zeros(n, np.int64)
    ring = [[] for _ in range(K.RING_SLOTS)]
    ref = np.zeros((steps // chunk, n), np.int32)
    drive = drive0.copy()
    now = 0
    for c in range(steps // chunk):
        if c == 8:
            drive = drive * 0.5 + 3.0
        for _ in range(chunk):
            now += 1
            slot = now % K.RING_SLOTS
            v = (
                rest
                + (v - rest) * a1
                + drive * (1 - a1)
                + g * (a1 - b1) * K.G_FACTOR
                - A * K.A_FACTOR * (c1 - a1)
            )
            g = g * b1
            A = A * c1
            for i in ring[slot]:
                if modulatory[i]:
                    continue
                targets = np.flatnonzero(W[i])
                for j in targets:
                    if refrac[j] <= now:
                        g[j] += W[i, j]
            ring[slot] = []
            fire = (refrac <= now) & (v > K.V_THRESH_MV)
            idx = np.flatnonzero(fire)
            ref[c, idx] += 1
            ring[(now + K.DELAY_STEPS) % K.RING_SLOTS].extend(idx.tolist())
            v[idx] = rest[idx]
            g[idx] = 0.0
            refrac[idx] = now + K.REFRACTORY_STEPS
            A[idx] += K.KC_ADAPT_MV * is_kc[idx]
    assert lazy.sum() > 50
    assert np.array_equal(lazy, ref)
    np.testing.assert_allclose(k.state.v, v, atol=1e-9)
    np.testing.assert_allclose(k.state.g, g, atol=1e-9)


def test_state_round_trip_continues_identically():
    n, edges, is_kc, modulatory, drive, _w = random_graph(3)
    k = make_kernel(n, edges, is_kc=is_kc, modulatory=modulatory)
    counts = np.zeros(n, np.int32)
    k.set_drive(drive)
    k.run(700, counts)
    saved = {key: val.copy() for key, val in k.state.to_arrays().items()}
    ref_counts = np.zeros(n, np.int32)
    k.run(900, ref_counts)
    k2 = make_kernel(n, edges, is_kc=is_kc, modulatory=modulatory)
    k2.state = KernelState.from_arrays(saved, k2.rest)
    k2.set_drive(drive)
    assert k2.clock == 700
    new_counts = np.zeros(n, np.int32)
    k2.run(900, new_counts)
    assert np.array_equal(ref_counts, new_counts)
    assert np.array_equal(k.state.v, k2.state.v)
    assert np.array_equal(k.state.S, k2.state.S)
