"""A small deterministic stand-in for the connectome brain.

FakeBrain implements BrainProtocol (n, populations, observe, checkpoint,
restore, provenance) plus `eye_map()`. Its spike counts are a deterministic
pseudo-random function of the stimulus (a fixed random projection through a
leaky state, Poisson counts seeded from the stimulus bytes), so encoders,
readouts and the experiment harness can run end to end without the graph.
It is used by `openfly experiment run --fake-brain` and by the tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from openfly.interfaces import REQUIRED_POPULATIONS, ObservationResult, Stimulus


@dataclass(frozen=True)
class FakeEyeMap:
    uv_r16: np.ndarray
    eye_r16: np.ndarray
    uv_r8: np.ndarray
    eye_r8: np.ndarray
    r8_channel: np.ndarray


def _layout(n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Half the cells on each eye, u and v on a jittered grid in [0, 1]."""
    uv = np.zeros((n, 2), dtype=np.float32)
    eye = np.zeros(n, dtype=np.int8)
    half = n // 2
    for e, (lo, hi) in enumerate(((0, half), (half, n))):
        m = hi - lo
        cols = max(1, int(np.ceil(np.sqrt(m * 16 / 9))))
        rows = max(1, int(np.ceil(m / cols)))
        i = np.arange(m)
        u = (i % cols + 0.5) / cols + rng.uniform(-0.2, 0.2, size=m) / cols
        v = (i // cols + 0.5) / rows + rng.uniform(-0.2, 0.2, size=m) / rows
        uv[lo:hi, 0] = np.clip(u, 0.0, 1.0)
        uv[lo:hi, 1] = np.clip(v, 0.0, 1.0)
        eye[lo:hi] = e
    return uv, eye


class FakeBrain:
    def __init__(self, n: int = 3000, seed: int = 0, memory: float = 0.5, base_rate_hz: float = 4.0, gain_hz: float = 12.0):
        self.n = int(n)
        self.seed = int(seed)
        self.memory = float(memory)
        self.base_rate_hz = float(base_rate_hz)
        self.gain_hz = float(gain_hz)
        rng = np.random.default_rng(seed)
        sizes = {
            "R1-R6": 600, "R8p": 80, "R8y": 80, "lamina": 200, "KC": 500, "PAM11": 15, "PPL101": 2,
            "MBON07": 4, "MBON11": 2, "MBON_rest": 34, "DNp20_L": 1, "DNp20_R": 1, "DNpe017": 1,
            "DN_rest": 197, "central_complex": 200,
        }
        pops: dict[str, np.ndarray] = {}
        pos = 0
        for name, size in sizes.items():
            pops[name] = np.arange(pos, pos + size, dtype=np.int32)
            pos += size
        assert pos <= self.n, "n too small for the fake populations"
        pops["MBON"] = np.concatenate([pops["MBON07"], pops["MBON11"], pops["MBON_rest"]]).astype(np.int32)
        pops["DN"] = np.concatenate([pops["DNp20_L"], pops["DNp20_R"], pops["DNpe017"], pops["DN_rest"]]).astype(np.int32)
        pops["random2000"] = np.sort(rng.choice(self.n, size=min(2000, self.n), replace=False)).astype(np.int32)
        del pops["MBON_rest"], pops["DN_rest"]
        self.populations = {name: pops[name] for name in REQUIRED_POPULATIONS}
        n16 = len(self.populations["R1-R6"])
        n8 = len(self.populations["R8p"]) + len(self.populations["R8y"])
        self.n_r16, self.n_r8 = n16, n8
        uv16, eye16 = _layout(n16, rng)
        uv8, eye8 = _layout(n8, rng)
        r8_ids = np.union1d(self.populations["R8p"], self.populations["R8y"])
        channel = np.where(np.isin(r8_ids, self.populations["R8p"]), 2, 1).astype(np.int8)
        self._eye_map = FakeEyeMap(uv_r16=uv16, eye_r16=eye16, uv_r8=uv8, eye_r8=eye8, r8_channel=channel)
        self.W16 = rng.normal(0.0, 1.0 / np.sqrt(n16), size=(self.n, n16)).astype(np.float32) * 4.0
        self.W8 = rng.normal(0.0, 1.0 / np.sqrt(n8), size=(self.n, n8)).astype(np.float32) * 4.0
        self.bias = rng.normal(0.0, 0.3, size=self.n).astype(np.float32)
        self.state = np.zeros(self.n, dtype=np.float32)
        self.sim_ms = 0.0
        self.observations = 0

    def eye_map(self) -> FakeEyeMap:
        return self._eye_map

    def observe(self, stimulus: Stimulus, neural_ms: float) -> ObservationResult:
        r16 = np.asarray(stimulus.r16, dtype=np.float32)
        r8 = np.asarray(stimulus.r8, dtype=np.float32)
        if r16.shape[0] != self.n_r16 or r8.shape[0] != self.n_r8:
            raise ValueError(f"stimulus lengths {r16.shape[0]}, {r8.shape[0]} do not match {self.n_r16}, {self.n_r8}")
        drive = self.W16 @ (r16 - 0.5) + self.W8 @ (r8 - 0.5) + self.bias
        self.state = self.memory * self.state + (1.0 - self.memory) * drive
        rate = self.base_rate_hz + self.gain_hz * np.log1p(np.exp(np.clip(self.state, -30, 30)))
        expected = rate * float(neural_ms) / 1000.0
        digest = hashlib.sha256(r16.tobytes() + r8.tobytes() + self.state.tobytes()).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "little"))
        counts = rng.poisson(expected).astype(np.int32)
        self.sim_ms += float(neural_ms)
        self.observations += 1
        return ObservationResult(counts=counts, neural_ms=float(neural_ms), sim_ms=self.sim_ms, compute_seconds=0.0)

    def checkpoint(self, path: str) -> None:
        np.savez(path, state=self.state, sim_ms=np.array([self.sim_ms]), observations=np.array([self.observations]))

    def restore(self, path: str) -> None:
        with np.load(path) as data:
            self.state = data["state"].astype(np.float32)
            self.sim_ms = float(data["sim_ms"][0])
            self.observations = int(data["observations"][0])

    def provenance(self) -> dict:
        return {
            "kind": "FakeBrain",
            "n": self.n,
            "seed": self.seed,
            "memory": self.memory,
            "base_rate_hz": self.base_rate_hz,
            "gain_hz": self.gain_hz,
            "note": "deterministic pseudo-random stand-in, not the connectome",
        }
