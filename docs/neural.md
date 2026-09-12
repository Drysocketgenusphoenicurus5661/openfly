# OpenFly neural core

Status: built and measured on 2026-09-12 on the development machine (AMD
Ryzen 7 7700, 32 GB, Windows 11, Python 3.12, numpy 2.4.6, numba 0.67).
This document describes the compiled graph, the kernel, the Brain API and
the numbers measured on the real MaleCNS v1.0 data. Sections 6 and 7 of
`PLAN.md` are the specification; where the measured behaviour differs from
what the plan expected, this document says so, and section 4 records the
one declared assumption added on top of the plan.

## 1. Data pipeline (openfly/connectome)

Commands:

    uv run openfly prepare       download if missing, verify, normalize, compile, lock
    uv run openfly verify        source hashes and compiled array hashes
    uv run openfly graph-info    counts, arrays and caveats of the compiled graph

Sources (`openfly/connectome/sources.py`): the three MaleCNS v1.0 flat
connectome feather files, pinned by size and SHA-256, downloaded with
resumable streaming into `data/malecns/` (`download.py`). Node policy:
every body whose superclass is non-null and non-empty and whose status is
not Glia. Edge policy: every edge between retained bodies, including the
101 self-connections and the 10,299,701 weight-1 edges. Measured on the
real files (`normalize.py`):

| Quantity | Value |
| --- | --- |
| Bodies in the annotation file | 211,577 |
| Retained neurons | 166,700 |
| Rows in the weights file | 151,856,684 |
| Retained directed edges | 25,582,938 |
| Synaptic contacts | 124,177,617 |
| Compile wall time | 22 s (weights streamed in 20 s) |
| Peak working set during compile | 2.16 GB |

Sign rule per presynaptic neuron from `consensus_nt`: +1 for acetylcholine
without gaba, glutamate or histamine; -1 for gaba, glutamate or histamine
without acetylcholine; otherwise +1 flagged uncertain. Modulatory cells
(dopamine, serotonin, octopamine consensus only) are flagged. Result:
107,438 excitatory, 59,262 inhibitory, 3,718 uncertain (3,177 of them
"unclear", 541 modulatory). Weight = contacts x sign x 0.275 mV as float32.

Photoreceptor geometry (`compile.py`): an R1-R6 cell has no column
annotation, so its column is the modal `assignedOlHex1/2` of its lamina
targets (L1 to L5) weighted by contact count: 3,335 of 3,377 R1-R6 cells
map (1,107 left eye, 2,228 right eye, by `rootSide`). R8p and R8y cells use
the modal column of any column-annotated target: all 811 map (330 R8p, 481
R8y). Hex to Cartesian is x = h1 - 0.5 h2, y = sqrt(3)/2 h2, then each eye
is min-max normalized to [0, 1] in both axes independently, so both eyes
project the full stimulus field (deliberate; there is no binocular overlap
model). R8 reuses the R1-R6 bounds of its eye and is clipped.

Caveat: the reconstruction has twice as many mapped R1-R6 cells on the
right (2,228) as on the left (1,107). A uniform stimulus therefore drives
the two eyes asymmetrically; lateral differences in downstream activity are
partly layout, not stimulus.

`data/graph.npz` arrays (all in graph order, index = rank of bodyId):
`ptr` int64 (n+1), `post` int32, `weight` float32, `ids` int64,
`superclass`, `type`, `instance`, `cell_class`, `soma_side`, `root_side`
(unicode), `nt_sign` int8, `nt_uncertain` bool, `modulatory` uint8, `r16`
int32, `r16_uv` float32 (3335, 2), `r16_eye` int8, `r16_column` int32,
`r8`, `r8_uv` (811, 2), `r8_eye`, `r8_column`, `r8_channel` int8 (1 R8y
green, 2 R8p blue), `lamina` int32 (7,114 L1, L2, L3, L5 cells).
`data/graph-manifest.json` holds counts, timings, memory and caveats;
`data/graph.lock.json` holds the SHA-256 of every array.

## 2. Kernel (openfly/neural/kernel.py)

Event-driven leaky integrate-and-fire in numba, single-threaded, no random
numbers. Constants: dt 0.1 ms, membrane tau 20 ms, synaptic tau 5 ms,
threshold -45 mV, rest -52 mV (-60 mV for Kenyon cells), axonal delay 1.8
ms (18 steps, 19-slot ring buffer), refractory 2.2 ms (22 steps, input
dropped and no spike; the constant drive keeps integrating), Kenyon cell
adaptation +8 mV per spike decaying with tau 200 ms.

Between events the state is advanced exactly in closed form over d steps
(t = 0.1 d ms, a = exp(-t/20), b = exp(-t/5), c = exp(-t/200)):

    v <- rest + (v - rest) a + I (1 - a) + g (a - b) / 3
    v <- v - A (200 / 180) (c - a)
    g <- g b,  A <- A c

Lazy active list: a neuron is skipped while v <= threshold, I <= threshold
- rest and I + g <= threshold - rest; in that region it cannot reach
threshold without new input (adaptation only hyperpolarizes), so sleeping
is exact. Sleepers are woken by an incoming spike or a drive change. Every
neuron is materialized at the end of each call. The kernel was checked
against a brute-force step-every-neuron reference on random graphs: spike
trains are identical and membrane potentials agree to 1e-12 mV
(`tests/neural/test_kernel.py`).

The per-neuron state is one 64-byte record (v, g, A, last, refractory
until, rest, drive, active position) so a random access touches one cache
line; this made the kernel 2.5x faster than separate arrays.

A useful number: a presynaptic spike adds its weight w to g, and the
resulting membrane deflection peaks 9.2 ms later at 0.158 w. From rest a
single spike therefore needs about 160 contacts (44 mV) to fire a target;
sustained input at rate r Hz through w mV raises the mean membrane by
0.005 w r mV, so 7 mV needs w r of about 1,400 (for example 28 mV from a
100 contact synapse at 50 Hz).

## 3. Brain (openfly/neural/brain.py)

    from openfly.neural.brain import Brain
    brain = Brain(half_saturation=0.5, plastic=False)   # loads data/graph.npz, 0.2 s
    result = brain.observe(stimulus, neural_ms=200.0)   # ObservationResult
    brain.checkpoint(path); brain.restore(path); brain.provenance()
    brain.eye_map(); brain.graph_hashes(); brain.population_rates(counts, neural_ms)
    brain.reset()                                        # fresh state, clock 0

Populations (`brain.populations`, name to int32 index array):

| Name | Definition | Size |
| --- | --- | --- |
| R1-R6 | mapped R1-R6 cells, stimulus order | 3,335 |
| R8 (extra) | mapped R8p and R8y cells, stimulus order for `Stimulus.r8` | 811 |
| R8p, R8y | subsets of R8 by channel | 330, 481 |
| lamina | L1, L2, L3, L5 | 7,114 |
| KC | type starts with KC | 4,064 |
| PAM11, PPL101 | by type | 15, 2 |
| MBON07, MBON11, MBON | by type, MBON = type starts with MBON | 4, 2, 97 |
| DNp20_L, DNp20_R, DNpe017 | type DNp20 by somaSide; DNpe017 both sides | 1, 1, 2 |
| DN | superclass == descending_neuron (the tbc, sensory_descending and efferent_descending variants are excluded) | 1,314 |
| central_complex | class == CX (the MaleCNS `class` column) | 2,950 |
| random2000 | numpy default_rng(20260912) sample without replacement from superclass cb_* minus photoreceptors and lamina | 2,000 |

The sizes PAM11 15, PPL101 2, MBON07 4, MBON11 2, R1-R6 3,335 and R8 811
are asserted at load (`check_counts=False` for synthetic graphs).

Stimulus conventions for encoders: `Stimulus.r16[i]` is the luminance in
[0, 1] of cell `populations["R1-R6"][i]`, whose field position is
`eye_map().uv_r16[i]` (u, v in [0, 1]) on eye `eye_map().eye_r16[i]` (0
left, 1 right). `Stimulus.r8[i]` likewise for `populations["R8"][i]` with
`uv_r8`, `eye_r8` and `r8_channel` (1 R8y green, 2 R8p blue). Values are
low-passed with tau 10 ms (updated per 10 ms bin). Drive: lamina 12 mV
constant; photoreceptors 30 L / (h + L) mV with half-saturation h
(constructor argument, settings key `neural.half_saturation`, default
0.5), so white gives 20 mV (about 115 Hz), mid-grey 15 mV (about 80 Hz)
and dark 0. `Stimulus.pulses` entries (population, mV, duration_ms) add to
that population's drive for the first duration_ms of the observation. The
observation runs in 10 ms bins; the clock continues across observations.

Checkpoints hold only mutable state (membrane records, ring buffer, active
list, luminance low-pass state, clock, plastic factors and traces) plus
metadata (kernel version, graph array hashes, parameters, population
sizes); `restore` refuses a checkpoint whose metadata differs. A
full-brain checkpoint is 1.3 MB, written in 0.25 s and restored in 0.02
s, and the run continues bit-identically after a restore.

## 4. Declared assumption: R8 drives aMe12 (excitatory)

`Brain(r8_ame12_excitatory=True)`, settings key `neural.r8_ame12_excitatory`,
default on; CLI switch `--r8-ame12 on|off` on `benchmark`, `circuits` and
`observe-test`.

Under the transmitter sign rule every R8 output is inhibitory (histamine).
The six aMe12 accessory medulla neurons receive 390 edges (2,100 contacts,
weight sum -577.5 mV) from R8 cells of every subtype (R8p 260, R8y 27, R8d
6, R8_unclear 97) and make 191 synapses (weight sum +491 mV) onto Kenyon
cells, mostly KCg-d, plus outputs to Tm37, Tm30, Cm6, aMe4 and further
projection neurons. With the sign rule as measured, aMe12 is silenced and
no visual signal reaches the mushroom body or the central brain (section
6). The published circuit finding is that R8 photoreceptors drive aMe12
(Nature 2023, https://doi.org/10.1038/s41586-023-06681-6), so OpenFly
treats those 390 edges as net excitatory: at Brain construction the
weights are copied and the sign of exactly those edges is flipped to
positive. The compiled graph and its hashes are untouched; `parameters()`
records the flag, the edge count and the SHA-256 of the edge index array
and of the resulting weights, and `provenance()["assumptions"]` carries
the full record including the weight sums before and after and the
citation. A checkpoint made with the flag on is refused by a brain with
it off. `Brain(r8_ame12_excitatory=False)` reproduces the pure sign-rule
model for controls.

## 5. Plastic arm (openfly/neural/plasticity.py)

Off by default; `Brain(plastic=True)` enables it on the 7,835 edges from
Kenyon cells onto MBON07 and MBON11. Rule per 10 ms bin from actual spike
counts: KC, MBON and compartment dopamine rate traces (tau 1 s), a slow
MBON baseline (tau 1800 s), update u = -eta d_m x_k (y_m - ybar_m) with
eta 0.001, low-passed with tau 50 ms, integrated per bin, relaxing back to
baseline with tau 1800 s, bounded to 0.1 to 2.0 times the baseline weight.
Dopamine gain per MBON is the fraction of the compartment DAN population's
contacts onto that MBON (PAM11 for MBON07, PPL101 for MBON11). A plastic
brain works on its own copy of the weights so `graph_hashes()` always
describes the compiled graph. Modulatory neurons still deliver nothing
through the kernel; only this rule uses their spikes.

## 6. Measured behaviour on the real graph

All numbers: single core, fresh state per stimulus, assumption of section
4 on unless stated. The runs are deterministic: repeated runs give the
same spike counts to the last spike.

### 6.1 Without the assumption (pure sign rule, `--r8-ame12 off`)

Under the specified constants a uniform field, a half field, a moving bar
pattern and white-dark flicker all stay in the lamina and distal medulla.
After 1 s of white field 6,528 optic lobe intrinsic neurons spike (mostly
L1, L3, L5, L2 and the wide-field Dm12, Dm18, Dm20 cells), 14 visual
projection neurons, 3 central brain neurons and no Kenyon cell. The
reasons are quantitative, not a kernel defect (the kernel matches the
brute-force reference exactly):

- L1 is glutamatergic and therefore inhibitory under the sign rule, and
  with its 12 mV tonic drive it is the largest input to the columnar
  medulla cells (Mi1 receives about 1,230 mV.spikes per second of
  inhibition against 430 of excitation; Tm3, Tm2, C3 are similar).
- The wide-field Dm cells (GABA) fire at 25 to 90 Hz and inhibit the rest
  of the medulla.
- Even the excitatory input alone is small against the 7 mV gap: 430
  mV.spikes per second onto Mi1 is a mean depolarization of about 2 mV.
- The one strong visual route into the central brain, R8 to aMe12 to the
  KCg-d Kenyon cells, is cut because the histaminergic R8 outputs count
  as inhibitory (section 4).

Cost in this regime: 0.25 s (dark) to 0.37 s (white) of wall time per
100 ms of neural time, 286,000 to 683,000 spikes per second, 8,600 to
12,600 neurons on the active list.

### 6.2 With the assumption (default)

`uv run openfly observe-test --neural-ms 500` (white field): KC spikes per
observation 1,961, 4,615, 4,439 (1,295 to 1,461 of the 4,064 cells), KC
about 2.2 Hz sustained, MBON 250 to 762 spikes, DN 1,864 to 5,251
(about 8 Hz sustained), central complex 5,907 to 13,684, random2000 2,444
to 11,658 per 500 ms; total spikes rise from 406,000 in the first 500 ms
to 609,000 once sustained. Activity keeps building for about 500 ms after
onset, so at 200 ms per observation the first two observations carry only
6 and 4 KC spikes and the third 2,934; the state carries over, so a
sequence of observations settles into the sustained regime after about
half a second of neural time. A PAM11 pulse of 20 mV for 200 ms gives 323
to 325 PAM11 spikes on all 15 cells and a PPL101 pulse 59 to 99 spikes on
both cells. Wall time in the sustained regime is about 0.9 s per 100 ms
of neural time on a white field (4.4 s per 500 ms observation), against
0.5 s per 100 ms during the first 500 ms.

Benchmark (`uv run openfly benchmark --neural-ms 500`, 500 ms untimed
warm-up so the timed window is the sustained regime):

| Stimulus | assumption | wall s per 100 ms neural | spikes per s | neurons spiking | active list | KC Hz | MBON Hz | DN Hz | CX Hz | random2000 Hz |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dark | on | 0.24 | 285,000 | 7,900 | 9,100 | 0 | 0 | 0 | 0 | 0 |
| mid-grey | on | 0.89 | 1,068,000 | 21,700 | 19,000 | 2.1 | 13.7 | 7.8 | 9.1 | 11.5 |
| white | on | 0.88 | 1,205,000 | 21,600 | 19,700 | 2.3 | 15.7 | 7.6 | 9.3 | 11.6 |
| dark | off | 0.25 | 285,000 | 7,900 | 9,100 | 0 | 0 | 0 | 0 | 0 |
| mid-grey | off | 0.31 | 549,000 | 10,300 | 12,400 | 0 | 0 | 0.1 | 0 | 0 |
| white | off | 0.31 | 683,000 | 10,100 | 12,200 | 0 | 0 | 0.1 | 0 | 0 |

The dark field is identical in both models because no R8 cell fires. With
the assumption on, the benchmark's own recommendation line reads: worst
case 0.889 s per 100 ms, ceiling 116 ms per bar for 21,000 observations
in 6 hours.

### 6.3 Neural time per bar

The kernel is single-threaded, so cost is linear in neural time: about
0.009 s of wall time per ms of neural time in the sustained white-field
regime, 0.0025 in the dark.

| neural_ms | s per observation (worst) | 21,000 observation pass (5 minute bars) | one day of 1 minute bars (375 observations) | 400 days of 1 minute bars |
| --- | --- | --- | --- | --- |
| 50 | 0.45 | 2.6 h | 2.8 min | 19 h |
| 100 | 0.9 | 5.3 h | 5.6 min | 37 h |
| 200 | 1.8 | 10.5 h | 11 min | 75 h |

Recommendation: 100 ms per bar. It keeps a 21,000 observation 5 minute
pass under the 6 hour budget (the ceiling is about 110 ms) and costs
about 6 minutes per trading day on 1 minute bars, which is far inside
real time for the live worker (one observation per minute, 0.9 s each).
The settings default of 200 ms would need 10.5 hours per 5 minute pass
and 75 hours for 400 days of 1 minute bars, so it should be lowered to
100 unless the pass can be split: independent arms (encoders, readouts,
frozen versus plastic) are separate single-threaded processes and this
machine has 8 cores, so 6 to 8 passes can run side by side at about 0.6
GB each. Note that with 100 ms per observation the network is always in
its onset regime relative to each new stimulus, but the state carries
across observations, so the sustained regime is reached after 5 bars.

## 7. Tests

    uv run pytest tests/neural tests/connectome -q
    OPENFLY_FULL_TEST=1 uv run pytest tests/connectome -q

`tests/neural/test_kernel.py`: analytic first spike time, delay and
weight of delivery, inhibition, refractory blocking of input and of
spiking, Kenyon cell adaptation, modulatory silence, sleeping neurons,
exact lazy evolution, bit-identical repeat runs, equality with a
brute-force reference, state round trip. `tests/neural/test_brain_synthetic.py`:
a synthetic graph.npz with a few cells of every required type; populations,
observe, pulses, invalid stimuli, checkpoint and restore, provenance, the
plastic arm. `tests/connectome/test_normalize.py`: sign rule, node policy,
CSR, streaming edge filter with duplicate merge, hex to uv transform,
modal column, per-eye normalization. `tests/connectome/test_full_data.py`
(env gated): hashes, counts, population sizes, eye map, white field
observation, PAM11 and PPL101 pulses, plastic edge count.
