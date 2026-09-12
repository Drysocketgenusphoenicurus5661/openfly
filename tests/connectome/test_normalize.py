"""Unit tests of the sign rule, node policy, edge loading and the hex to uv transform."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather
import pytest

from openfly.connectome import compile as cg
from openfly.connectome import normalize as nz


@pytest.mark.parametrize(
    "value, expected",
    [
        ("acetylcholine", (1, False, False)),
        ("gaba", (-1, False, False)),
        ("glutamate", (-1, False, False)),
        ("histamine", (-1, False, False)),
        ("GABA", (-1, False, False)),
        ("gaba,glutamate", (-1, False, False)),
        ("acetylcholine,dopamine", (1, False, False)),
        ("acetylcholine,gaba", (1, True, False)),
        ("unclear", (1, True, False)),
        ("", (1, True, False)),
        (None, (1, True, False)),
        (float("nan"), (1, True, False)),
        ("dopamine", (1, True, True)),
        ("serotonin, octopamine", (1, True, True)),
        ("dopamine,unclear", (1, True, False)),
    ],
)
def test_sign_rule(value, expected):
    assert nz.sign_from_nt(value) == expected


def test_signs_for_values_vectorised():
    s = pd.Series(["acetylcholine", "gaba", None, "dopamine", "glutamate,acetylcholine"])
    sign, uncertain, modulatory = nz.signs_for_values(s)
    assert sign.tolist() == [1, -1, 1, 1, 1]
    assert uncertain.tolist() == [False, False, True, True, True]
    assert modulatory.tolist() == [0, 0, 0, 1, 0]
    assert sign.dtype == np.int8 and modulatory.dtype == np.uint8


def test_node_policy():
    ann = pd.DataFrame(
        {
            "bodyId": [5, 3, 9, 1, 7, 8],
            "superclass": ["cb_intrinsic", None, "", "ol_sensory", "cb_intrinsic", "  "],
            "status": ["Traced", "Traced", "Traced", "Orphan", "Glia", None],
        }
    )
    assert nz.node_mask(ann).tolist() == [True, False, False, True, False, False]
    nodes = nz.select_nodes(ann)
    assert nodes["bodyId"].tolist() == [1, 5]


def test_build_csr():
    pre = np.array([0, 0, 2, 2, 2], np.int32)
    post = np.array([1, 2, 0, 1, 2], np.int32)
    ptr = nz.build_csr(pre, post, 4)
    assert ptr.tolist() == [0, 2, 2, 5, 5]


def test_load_edges_filters_sorts_and_merges(tmp_path):
    table = pa.table(
        {
            "body_pre": pa.array([30, 10, 10, 20, 99, 10, 30], pa.int64()),
            "body_post": pa.array([10, 30, 20, 20, 10, 20, 10], pa.int64()),
            "weight": pa.array([1, 4, 2, 7, 5, 3, 1], pa.int64()),
        }
    )
    path = tmp_path / "w.feather"
    feather.write_feather(table, str(path))
    ids = np.array([10, 20, 30], np.int64)
    pre, post, contacts, stats = nz.load_edges(ids, path)
    # body 99 is dropped; (10 -> 20) appears twice and is summed; (30 -> 10) twice too.
    assert pre.tolist() == [0, 0, 1, 2]
    assert post.tolist() == [1, 2, 1, 0]
    assert contacts.tolist() == [5, 4, 7, 2]
    assert stats["edges"] == 4 and stats["contacts"] == 18
    assert stats["duplicates_merged"] == 2
    assert stats["self_edges"] == 1
    assert stats["rows_read"] == 7


def test_assert_expected():
    nz.assert_expected(166_700, 25_582_938, 124_177_617)
    with pytest.raises(AssertionError, match="neurons"):
        nz.assert_expected(1, 25_582_938, 124_177_617)


def test_hex_to_xy():
    x, y = cg.hex_to_xy(np.array([1.0, 3.0]), np.array([2.0, 2.0]))
    assert x.tolist() == [0.0, 2.0]
    assert y.tolist() == pytest.approx([math.sqrt(3.0), math.sqrt(3.0)])


def test_column_ids_round_trip():
    col = cg.column_ids(np.array([3.0, np.nan, 12.0]), np.array([7.0, 1.0, np.nan]))
    assert col.tolist() == [3 * 64 + 7, -1, -1]
    h1, h2 = cg.decode_column(col[:1])
    assert h1.tolist() == [3] and h2.tolist() == [7]


def test_modal_column_weighted_with_tie_break():
    # sources 0 and 1; targets 2, 3, 4 with columns 5, 6, 7
    col = np.array([-1, -1, 5, 6, 7], np.int64)
    pre = np.array([0, 0, 0, 1, 1], np.int32)
    post = np.array([2, 3, 3, 3, 4], np.int32)
    contacts = np.array([3, 1, 1, 2, 2], np.int64)
    source = np.array([True, True, False, False, False])
    target = np.array([False, False, True, True, True])
    out = cg.modal_column(pre, post, contacts, source, col, target)
    assert out[0] == 5  # 3 contacts vs 2
    assert out[1] == 6  # tie 2 vs 2: smallest column id
    assert out[2] == -1


def test_normalize_uv_per_eye_and_clipping():
    x = np.array([0.0, 2.0, 4.0, 10.0, 20.0])
    y = np.array([0.0, 1.0, 2.0, 5.0, 7.0])
    eye = np.array([0, 0, 0, 1, 1], np.int8)
    uv, bounds, clipped = cg.normalize_uv(x, y, eye)
    assert clipped == 0
    assert uv[:, 0].tolist() == pytest.approx([0.0, 0.5, 1.0, 0.0, 1.0])
    assert uv[:, 1].tolist() == pytest.approx([0.0, 0.5, 1.0, 0.0, 1.0])
    uv2, _, clipped2 = cg.normalize_uv(
        np.array([-1.0, 30.0]), np.array([1.0, 6.0]), np.array([0, 1], np.int8), bounds
    )
    assert clipped2 == 2
    assert uv2[:, 0].tolist() == [0.0, 1.0]
    assert uv2[0, 1] == pytest.approx(0.5)
    assert uv2[1, 1] == pytest.approx(0.5)


def test_photoreceptor_geometry_small():
    # R cells 0-3 (left, left, right, unmapped), lamina 4-7 with columns, R8 8-9, other 10
    types = np.array(
        ["R1-R6", "R1-R6", "R1-R6", "R1-R6", "L1", "L2", "L1", "L2", "R8p", "R8y", "Mi1"]
    )
    root = np.array(["L", "L", "R", "R", "", "", "", "", "L", "R", ""])
    hex1 = np.array([np.nan, np.nan, np.nan, np.nan, 1, 3, 10, 12, np.nan, np.nan, 2])
    hex2 = np.array([np.nan, np.nan, np.nan, np.nan, 1, 1, 2, 2, np.nan, np.nan, 1])
    pre = np.array([0, 0, 1, 2, 2, 8, 9, 9], np.int32)
    post = np.array([4, 5, 5, 6, 7, 10, 7, 6], np.int32)
    contacts = np.array([5, 1, 3, 1, 4, 2, 3, 1], np.int64)
    arrays, stats = cg.photoreceptor_geometry(pre, post, contacts, types, root, hex1, hex2)
    assert arrays["r16"].tolist() == [0, 1, 2]
    assert stats["r16_mapped"] == 3 and stats["r16_total"] == 4
    assert arrays["r16_eye"].tolist() == [0, 0, 1]
    # left eye: columns (1,1) and (3,1): u spans 0..1, v constant -> 0
    assert arrays["r16_uv"][0].tolist() == pytest.approx([0.0, 0.0])
    assert arrays["r16_uv"][1].tolist() == pytest.approx([1.0, 0.0])
    # R8p maps through Mi1 (column 2,1) on the left eye: between the two lamina columns
    assert arrays["r8"].tolist() == [8, 9]
    assert arrays["r8_channel"].tolist() == [2, 1]
    assert arrays["r8_uv"][0, 0] == pytest.approx(0.5)
    assert stats["r8_mapped"] == 2
