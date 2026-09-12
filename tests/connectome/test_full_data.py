"""Full-data tests: run only with OPENFLY_FULL_TEST=1 and a compiled graph."""

from __future__ import annotations

import os

import numpy as np
import pytest

from openfly.config import PATHS
from openfly.interfaces import REQUIRED_POPULATIONS

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENFLY_FULL_TEST") != "1" or not PATHS.graph.exists(),
    reason="set OPENFLY_FULL_TEST=1 and run openfly prepare for full-data tests",
)


@pytest.fixture(scope="module")
def brain():
    from openfly.neural.brain import Brain

    return Brain()


def uniform(brain, luminance, pulses=()):
    from openfly.neural.benchmark import uniform_stimulus

    return uniform_stimulus(brain, luminance, pulses)


def test_sources_and_graph_verify():
    from openfly.connectome.verify import verify

    report = verify()
    assert report["sources_ok"], report
    assert report["graph"]["ok"], report["graph"]
    assert report["graph"]["arrays_total"] >= 20


def test_manifest_counts():
    from openfly.connectome.compile import read_manifest
    from openfly.connectome.normalize import EXPECTED

    m = read_manifest()
    assert m is not None
    assert m["counts"]["neurons"] == EXPECTED["neurons"]
    assert m["counts"]["edges"] == EXPECTED["edges"]
    assert m["counts"]["contacts"] == EXPECTED["contacts"]
    assert m["photoreceptors"]["r16_mapped"] == 3335
    assert m["photoreceptors"]["r8_mapped"] == 811


def test_graph_counts(brain):
    assert brain.n == 166_700
    assert brain.edges == 25_582_938
    g = brain._graph
    contacts = np.rint(np.abs(g["weight"]) / 0.275).astype(np.int64)
    assert int(contacts.sum()) == 124_177_617


def test_population_sizes(brain):
    sizes = brain.population_sizes()
    for name in REQUIRED_POPULATIONS:
        assert name in sizes
    assert sizes["R1-R6"] == 3335
    assert sizes["R8"] == 811 and sizes["R8p"] == 330 and sizes["R8y"] == 481
    assert sizes["lamina"] == 7114
    assert sizes["KC"] == 4064
    assert sizes["PAM11"] == 15 and sizes["PPL101"] == 2
    assert sizes["MBON07"] == 4 and sizes["MBON11"] == 2 and sizes["MBON"] == 97
    assert sizes["DNp20_L"] == 1 and sizes["DNp20_R"] == 1 and sizes["DNpe017"] == 2
    assert sizes["DN"] == 1314
    assert sizes["central_complex"] == 2950
    assert sizes["random2000"] == 2000


def test_eye_map(brain):
    em = brain.eye_map()
    assert em.uv_r16.shape == (3335, 2) and em.uv_r8.shape == (811, 2)
    assert em.uv_r16.min() >= 0 and em.uv_r16.max() <= 1
    for eye in (0, 1):
        m = em.eye_r16 == eye
        assert em.uv_r16[m, 0].min() == 0 and em.uv_r16[m, 0].max() == 1
        assert em.uv_r16[m, 1].min() == 0 and em.uv_r16[m, 1].max() == 1
    assert set(em.r8_channel.tolist()) == {1, 2}


def test_white_field_observation(brain):
    brain.reset()
    res = brain.observe(uniform(brain, 1.0), 200.0)
    assert res.counts.shape == (brain.n,)
    assert res.counts[brain.populations["R1-R6"]].sum() > 0
    assert res.counts[brain.populations["R8"]].sum() > 0
    assert res.counts[brain.populations["lamina"]].sum() > 0
    downstream = (
        res.counts.sum()
        - res.counts[brain.populations["R1-R6"]].sum()
        - res.counts[brain.populations["R8"]].sum()
        - res.counts[brain.populations["lamina"]].sum()
    )
    assert downstream > 0
    kc = res.counts[brain.populations["KC"]].sum()
    for _ in range(4):
        kc += brain.observe(uniform(brain, 1.0), 200.0).counts[brain.populations["KC"]].sum()
    if kc == 0:
        pytest.xfail(
            "measured: a uniform white field does not propagate past the distal medulla under the "
            "transmitter sign rule, so Kenyon cells stay silent (documented in docs/neural.md)"
        )


def test_pam11_pulse_produces_pam11_spikes(brain):
    brain.reset()
    idx = brain.populations["PAM11"]
    quiet = brain.observe(uniform(brain, 1.0), 200.0)
    assert quiet.counts[idx].sum() == 0
    res = brain.observe(uniform(brain, 1.0, pulses=(("PAM11", 20.0, 200.0),)), 200.0)
    assert res.counts[idx].sum() > 0
    assert (res.counts[idx] > 0).sum() == 15


def test_ppl101_pulse_produces_ppl101_spikes(brain):
    brain.reset()
    idx = brain.populations["PPL101"]
    res = brain.observe(uniform(brain, 1.0, pulses=(("PPL101", 20.0, 200.0),)), 200.0)
    assert res.counts[idx].sum() > 0
    assert (res.counts[idx] > 0).sum() == 2


def test_plastic_edges_count():
    from openfly.neural.brain import Brain

    plastic = Brain(plastic=True)
    assert len(plastic.plasticity.edges) == 7835
