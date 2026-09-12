"""Brain tests on a small synthetic graph.npz with fake annotations."""

from __future__ import annotations

import numpy as np
import pytest

from openfly.interfaces import REQUIRED_POPULATIONS, BrainProtocol, Stimulus
from openfly.neural.brain import Brain, build_populations, check_population_sizes, load_graph
from openfly.neural.plasticity import KCMBONPlasticity


def synthetic_graph():
    """A few cells of every required type plus a handful of edges."""
    cells = []  # (type, superclass, cell_class, soma_side, root_side)

    def add(t, sc, cls="", soma="", root="", k=1):
        for _ in range(k):
            cells.append((t, sc, cls, soma, root))

    add("R1-R6", "ol_sensory", root="L", k=3)
    add("R1-R6", "ol_sensory", root="R", k=3)
    add("R8p", "ol_sensory", root="L", k=1)
    add("R8p", "ol_sensory", root="R", k=1)
    add("R8y", "ol_sensory", root="L", k=1)
    add("R8y", "ol_sensory", root="R", k=1)
    for t in ("L1", "L2", "L3", "L5"):
        add(t, "ol_intrinsic", soma="L")
        add(t, "ol_intrinsic", soma="R")
    add("L4", "ol_intrinsic", soma="L")
    add("KCg-m", "cb_intrinsic", cls="Kenyon_Cell", soma="L", k=3)
    add("KCab-s", "cb_intrinsic", cls="Kenyon_Cell", soma="R", k=2)
    add("PAM11", "cb_intrinsic", cls="DAN", soma="L", k=2)
    add("PPL101", "cb_intrinsic", cls="DAN", soma="R", k=1)
    add("MBON07", "cb_intrinsic", cls="MBON", soma="L", k=2)
    add("MBON11", "cb_intrinsic", cls="MBON", soma="R", k=1)
    add("MBON01", "cb_intrinsic", cls="MBON", soma="R", k=1)
    add("DNp20", "descending_neuron", soma="L")
    add("DNp20", "descending_neuron", soma="R")
    add("DNpe017", "descending_neuron", soma="L")
    add("DNa02", "descending_neuron", soma="R", k=2)
    add("PFNa", "cb_intrinsic", cls="CX", soma="L", k=3)
    add("SMP001", "cb_intrinsic", soma="L", k=12)
    add("aMe12", "visual_projection", soma="L", k=2)
    n = len(cells)
    types = np.array([c[0] for c in cells], dtype=np.str_)
    superclass = np.array([c[1] for c in cells], dtype=np.str_)
    cell_class = np.array([c[2] for c in cells], dtype=np.str_)
    soma = np.array([c[3] for c in cells], dtype=np.str_)
    root = np.array([c[4] for c in cells], dtype=np.str_)

    def where(t):
        return np.flatnonzero(types == t)

    r16_all = where("R1-R6")
    r16 = r16_all[:4]  # two cells stay unmapped
    r8 = np.concatenate([where("R8p"), where("R8y")])
    lamina = np.concatenate([where(t) for t in ("L1", "L2", "L3", "L5")])
    kc = np.flatnonzero(np.char.startswith(types, "KC"))
    pam = where("PAM11")
    ppl = where("PPL101")
    mbon07 = where("MBON07")
    mbon11 = where("MBON11")
    dn = np.flatnonzero(superclass == "descending_neuron")
    ame12 = where("aMe12")

    edges = []
    for i, r in enumerate(r16_all):
        edges.append((r, lamina[i % len(lamina)], -0.275 * 20))
    for i, r in enumerate(r8):
        edges.append((r, lamina[(i + 2) % len(lamina)], -0.275 * 10))
        # histaminergic R8 onto aMe12: inhibitory under the sign rule, flipped by the assumption
        edges.append((r, ame12[i % len(ame12)], -0.275 * 60))
    for i, a in enumerate(ame12):
        edges.append((a, kc[(i + 3) % len(kc)], 0.275 * 100))
    # A jump of g by w mV peaks the membrane at about 0.158 w, so a synthetic
    # path needs a few hundred contacts to fire its target from rest.
    for i, lam in enumerate(lamina):
        edges.append((lam, kc[i % len(kc)], 0.275 * 400))  # strong lamina to KC drive
        edges.append((lam, dn[i % len(dn)], 0.275 * 200))
    for k_ in kc:
        for m in mbon07:
            edges.append((k_, m, 0.275 * 12))
        for m in mbon11:
            edges.append((k_, m, 0.275 * 8))
    for d in pam:
        for m in mbon07:
            edges.append((d, m, 0.275 * 5))
    for d in ppl:
        edges.append((d, mbon11[0], 0.275 * 7))
    edges.append((kc[0], kc[0], 0.275))  # a self edge
    edges.sort()
    pre = np.array([e[0] for e in edges], dtype=np.int64)
    post = np.array([e[1] for e in edges], dtype=np.int32)
    weight = np.array([e[2] for e in edges], dtype=np.float32)
    ptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(pre, minlength=n), out=ptr[1:])
    nt_sign = np.ones(n, np.int8)
    nt_sign[r16_all] = -1
    nt_sign[r8] = -1
    modulatory = np.zeros(n, np.uint8)
    modulatory[pam] = 1
    modulatory[ppl] = 1
    eye16 = np.array([0 if root[i] == "L" else 1 for i in r16], np.int8)
    eye8 = np.array([0 if root[i] == "L" else 1 for i in r8], np.int8)
    return {
        "ptr": ptr,
        "post": post,
        "weight": weight,
        "ids": np.arange(1000, 1000 + n, dtype=np.int64),
        "superclass": superclass,
        "type": types,
        "instance": types.copy(),
        "cell_class": cell_class,
        "soma_side": soma,
        "root_side": root,
        "nt_sign": nt_sign,
        "nt_uncertain": np.zeros(n, bool),
        "modulatory": modulatory,
        "r16": r16.astype(np.int32),
        "r16_uv": np.linspace(0, 1, 2 * len(r16), dtype=np.float32).reshape(len(r16), 2),
        "r16_eye": eye16,
        "r16_column": np.arange(len(r16), dtype=np.int32),
        "r8": r8.astype(np.int32),
        "r8_uv": np.linspace(0, 1, 2 * len(r8), dtype=np.float32).reshape(len(r8), 2),
        "r8_eye": eye8,
        "r8_column": np.arange(len(r8), dtype=np.int32),
        "r8_channel": np.array([2, 2, 1, 1], np.int8),
        "lamina": lamina.astype(np.int32),
    }


@pytest.fixture(scope="module")
def graph_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("graph") / "graph.npz"
    np.savez(path, **synthetic_graph())
    return path


@pytest.fixture
def brain(graph_file):
    return Brain(graph_path=graph_file, check_counts=False)


def white(brain, pulses=()):
    return Stimulus(r16=np.ones(len(brain.r16)), r8=np.ones(len(brain.r8)), pulses=pulses)


def dark(brain, pulses=()):
    return Stimulus(r16=np.zeros(len(brain.r16)), r8=np.zeros(len(brain.r8)), pulses=pulses)


def test_populations_are_complete_and_sized_as_built(brain):
    assert isinstance(brain, BrainProtocol)
    for name in REQUIRED_POPULATIONS:
        assert name in brain.populations
        assert brain.populations[name].dtype == np.int32
    sizes = brain.population_sizes()
    assert sizes["R1-R6"] == 4
    assert sizes["R8"] == 4 and sizes["R8p"] == 2 and sizes["R8y"] == 2
    assert sizes["lamina"] == 8
    assert sizes["KC"] == 5
    assert sizes["PAM11"] == 2 and sizes["PPL101"] == 1
    assert sizes["MBON07"] == 2 and sizes["MBON11"] == 1 and sizes["MBON"] == 4
    assert sizes["DNp20_L"] == 1 and sizes["DNp20_R"] == 1 and sizes["DNpe017"] == 1
    assert sizes["DN"] == 5
    assert sizes["central_complex"] == 3
    # random2000 draws from cb_* minus photoreceptors and lamina; the pool is small here
    assert 0 < sizes["random2000"] <= 2000
    assert np.all(np.diff(brain.populations["random2000"]) > 0)


def test_random_sample_is_reproducible(graph_file):
    a = build_populations(load_graph(graph_file))["random2000"]
    b = build_populations(load_graph(graph_file))["random2000"]
    assert np.array_equal(a, b)


def test_population_check_reports_mismatch(graph_file):
    pops = build_populations(load_graph(graph_file))
    with pytest.raises(ValueError, match="PAM11 has 2 cells, expected 15"):
        check_population_sizes(pops)
    with pytest.raises(ValueError):
        Brain(graph_path=graph_file)


def test_eye_map_shapes(brain):
    em = brain.eye_map()
    assert em.uv_r16.shape == (4, 2) and em.uv_r8.shape == (4, 2)
    assert em.eye_r16.tolist() == [0, 0, 0, 1]
    assert em.r8_channel.tolist() == [2, 2, 1, 1]
    assert np.array_equal(em.r16, brain.populations["R1-R6"])
    assert np.array_equal(em.r8, brain.populations["R8"])
    assert em.uv_r16.min() >= 0 and em.uv_r16.max() <= 1


def test_observe_returns_counts_and_advances_clock(brain):
    res = brain.observe(white(brain), 200.0)
    assert res.counts.dtype == np.int32 and res.counts.shape == (brain.n,)
    assert res.neural_ms == pytest.approx(200.0)
    assert res.sim_ms == pytest.approx(200.0)
    assert res.compute_seconds > 0
    res2 = brain.observe(white(brain), 55.0)
    assert res2.neural_ms == pytest.approx(55.0)
    assert res2.sim_ms == pytest.approx(255.0)
    assert brain.observations == 2
    lam = brain.populations["lamina"]
    assert res.counts[lam].sum() > 0  # 12 mV tonic drive
    assert res.counts[brain.populations["R1-R6"]].sum() > 0  # lit photoreceptors
    assert res.counts[brain.populations["KC"]].sum() > 0  # strong synthetic lamina to KC path


def test_dark_field_silences_photoreceptors(brain):
    res = brain.observe(dark(brain), 200.0)
    assert res.counts[brain.populations["R1-R6"]].sum() == 0
    assert res.counts[brain.populations["R8"]].sum() == 0
    assert res.counts[brain.populations["lamina"]].sum() > 0


def test_half_saturation_changes_photoreceptor_rate(graph_file):
    low = Brain(graph_path=graph_file, check_counts=False, half_saturation=0.2)
    high = Brain(graph_path=graph_file, check_counts=False, half_saturation=2.0)
    stim = Stimulus(r16=np.full(4, 0.5), r8=np.full(4, 0.5))
    a = low.observe(stim, 300.0).counts[low.populations["R1-R6"]].sum()
    b = high.observe(stim, 300.0).counts[high.populations["R1-R6"]].sum()
    assert a > b
    assert low.provenance()["parameters"]["half_saturation"] == 0.2


def test_pulse_drives_population_only_for_its_duration(brain):
    pam = brain.populations["PAM11"]
    quiet = brain.observe(dark(brain), 200.0)
    assert quiet.counts[pam].sum() == 0
    pulsed = brain.observe(dark(brain, pulses=(("PAM11", 20.0, 100.0),)), 200.0)
    assert pulsed.counts[pam].sum() > 0
    # A shorter pulse gives fewer spikes than a full-length one.
    brain.reset()
    short = brain.observe(dark(brain, pulses=(("PAM11", 20.0, 50.0),)), 200.0).counts[pam].sum()
    brain.reset()
    long = brain.observe(dark(brain, pulses=(("PAM11", 20.0, 200.0),)), 200.0).counts[pam].sum()
    assert 0 < short < long
    # PAM11 is modulatory: its spikes deliver nothing to MBON07 in the base model.
    assert (
        brain.observe(dark(brain, pulses=(("PAM11", 20.0, 200.0),)), 200.0)
        .counts[brain.populations["MBON07"]]
        .sum()
        == 0
    )


def test_invalid_stimuli_are_rejected(brain):
    with pytest.raises(ValueError):
        brain.observe(Stimulus(r16=np.ones(3), r8=np.ones(4)), 10.0)
    with pytest.raises(ValueError):
        brain.observe(Stimulus(r16=np.full(4, np.nan), r8=np.ones(4)), 10.0)
    with pytest.raises(KeyError):
        brain.observe(white(brain, pulses=(("nope", 1.0, 1.0),)), 10.0)
    with pytest.raises(ValueError):
        brain.observe(white(brain), 0.0)


def test_checkpoint_restore_round_trip(brain, tmp_path):
    stim = white(brain, pulses=(("PAM11", 20.0, 50.0),))
    brain.observe(stim, 130.0)
    path = tmp_path / "brain.npz"
    brain.checkpoint(str(path))
    later = [brain.observe(stim, 70.0).counts for _ in range(3)]
    v_after = brain.kernel.state.v.copy()
    brain.restore(str(path))
    assert brain.sim_ms == pytest.approx(130.0)
    again = [brain.observe(stim, 70.0).counts for _ in range(3)]
    for a, b in zip(later, again, strict=True):
        assert np.array_equal(a, b)
    assert np.array_equal(v_after, brain.kernel.state.v)


def test_restore_rejects_mismatched_brain(brain, graph_file, tmp_path):
    brain.observe(white(brain), 50.0)
    path = tmp_path / "ckpt.npz"
    brain.checkpoint(str(path))
    other = Brain(graph_path=graph_file, check_counts=False, half_saturation=0.9)
    with pytest.raises(ValueError, match="half_saturation"):
        other.restore(str(path))
    g = synthetic_graph()
    g["weight"] = g["weight"] * np.float32(1.5)
    changed = tmp_path / "graph2.npz"
    np.savez(changed, **g)
    other2 = Brain(graph_path=changed, check_counts=False)
    with pytest.raises(ValueError, match="graph arrays differ"):
        other2.restore(str(path))


def test_provenance_and_graph_hashes(brain):
    prov = brain.provenance()
    for key in ("kernel_version", "graph_hashes", "parameters", "population_sizes", "n", "edges"):
        assert key in prov
    assert set(prov["graph_hashes"]) == set(brain._graph)
    assert all(len(h) == 64 for h in prov["graph_hashes"].values())
    assert prov["parameters"]["plastic"] is False
    assert prov["population_definitions"]["DN"].startswith("superclass")


def test_plastic_arm_changes_kc_to_mbon_weights(graph_file):
    brain = Brain(graph_path=graph_file, check_counts=False, plastic=True)
    p = brain.plasticity
    assert isinstance(p, KCMBONPlasticity)
    assert len(p.edges) == 5 * 3  # 5 KCs onto 2 MBON07 + 1 MBON11
    assert p.gain.sum() == pytest.approx(2.0)  # each compartment's gains sum to one
    baseline = p.baseline.copy()
    stim = white(
        brain, pulses=(("PAM11", 20.0, 200.0), ("PPL101", 20.0, 200.0), ("MBON07", 15.0, 200.0))
    )
    for _ in range(5):
        brain.observe(stim, 200.0)
    s = p.summary()
    assert s["updates"] == 100
    assert s["fraction_changed"] > 0
    assert p.factor.min() >= 0.1 and p.factor.max() <= 2.0
    kernel_w = brain.kernel.weight[p.edges]
    np.testing.assert_allclose(
        kernel_w, (baseline.astype(np.float64) * p.factor).astype(np.float32)
    )
    assert brain.provenance()["plasticity_state"]["edges"] == 15
    # checkpoint carries the plastic state
    path = graph_file.parent / "plastic.npz"
    brain.checkpoint(str(path))
    other = Brain(graph_path=graph_file, check_counts=False, plastic=True)
    other.restore(str(path))
    assert np.array_equal(other.plasticity.factor, p.factor)
    assert np.array_equal(other.kernel.weight, brain.kernel.weight)


def test_r8_ame12_assumption_flips_edges_on_a_copy(graph_file, tmp_path):
    from openfly.neural.brain import r8_ame12_edges

    g = load_graph(graph_file)
    edges = r8_ame12_edges(g)
    assert len(edges) == 4
    assert np.all(g["weight"][edges] < 0)
    on = Brain(graph_path=graph_file, check_counts=False)  # default: assumption on
    off = Brain(graph_path=graph_file, check_counts=False, r8_ame12_excitatory=False)
    assert np.all(on.kernel.weight[edges] > 0)
    assert np.all(off.kernel.weight[edges] < 0)
    assert np.all(on._graph["weight"][edges] < 0)  # compiled graph untouched
    assert on.graph_hashes() == off.graph_hashes()
    others = np.setdiff1d(np.arange(len(g["weight"])), edges)
    assert np.array_equal(on.kernel.weight[others], off.kernel.weight[others])
    p_on, p_off = on.parameters(), off.parameters()
    assert p_on["r8_ame12_excitatory"] is True and p_off["r8_ame12_excitatory"] is False
    assert p_on["r8_ame12_edges"] == 4 and p_off["r8_ame12_edges"] == 4
    assert p_on["r8_ame12_edges_sha256"] == p_off["r8_ame12_edges_sha256"]
    assert p_on["r8_ame12_weights_sha256"] != p_off["r8_ame12_weights_sha256"]
    a = on.provenance()["assumptions"]["r8_ame12_excitatory"]
    assert (
        a["enabled"]
        and a["edges"] == 4
        and a["weight_sum_before_mv"] < 0 < a["weight_sum_after_mv"]
    )
    assert a["citation"].startswith("https://doi.org/")
    ame12 = np.flatnonzero(on.types == "aMe12")
    stim = white(on)
    assert on.observe(stim, 300.0).counts[ame12].sum() > 0
    assert off.observe(stim, 300.0).counts[ame12].sum() == 0
    path = tmp_path / "on.npz"
    on.checkpoint(str(path))
    with pytest.raises(ValueError, match="r8_ame12_excitatory"):
        off.restore(str(path))


def test_frozen_brain_never_changes_weights(brain):
    before = brain.kernel.weight.copy()
    stim = white(brain, pulses=(("PAM11", 20.0, 200.0),))
    for _ in range(3):
        brain.observe(stim, 200.0)
    assert np.array_equal(before, brain.kernel.weight)
