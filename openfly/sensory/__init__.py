"""Sensory encoders: market observation to photoreceptor luminance.

Public API:

    from openfly.sensory import make_encoder, stimulus_hash
    encoder = make_encoder("B", settings, eye_map)      # names A/chart, B/bars, C/features
    stimulus = encoder.encode(observation, brain)
    png = encoder.render_png(stimulus, eye_map)
"""

from openfly.sensory.encoders import (
    ENCODER_NAMES,
    BarsEncoder,
    ChartEncoder,
    FeatureEncoder,
    make_encoder,
    stimulus_hash,
)
from openfly.sensory.eyemap import EyeMap, default_eye_map, resolve_eye_map

__all__ = [
    "ENCODER_NAMES",
    "BarsEncoder",
    "ChartEncoder",
    "EyeMap",
    "FeatureEncoder",
    "default_eye_map",
    "make_encoder",
    "resolve_eye_map",
    "stimulus_hash",
]
