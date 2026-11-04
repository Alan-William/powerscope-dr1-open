"""Training-only observation augmentation, never a new power-circuit simulation."""

import numpy as np

LEVELS = (-0.9, -0.6, -0.3, -0.1, -0.03, -0.01, 0.01, 0.03, 0.1, 0.3, 0.6, 0.9)


def replace_level(x, phase, level):
    """Change one held sensor mean, retain residual noise and other channels.

    Shared normalization follows the existing input contract. Controller
    response is NOT re-simulated; this is an algorithm robustness assumption.
    """
    out = np.array(x, dtype=np.float64, copy=True)
    if out.shape != (3, 500) or not np.isfinite(out).all():
        raise ValueError("Expected finite 3x500 window")
    if phase not in (0, 1, 2) or not np.isfinite(level) or abs(level) > 1:
        raise ValueError("Invalid phase or held level")
    if np.std(out[phase], dtype=np.float64) > 0.03:
        raise ValueError(
            "Only an existing approximately flat SF phase can be augmented"
        )
    out[phase] += level - np.mean(out[phase])
    out /= max(float(np.abs(out).max()), 1e-9)
    return out.astype(np.float32)


def augment(x, labels, rng, probability=0.75):
    out = np.array(x, dtype=np.float32, copy=True)
    if len(out) != len(labels) or not 0 <= probability <= 1:
        raise ValueError("Invalid augmentation controls")
    for index in np.flatnonzero(np.isin(labels, [16, 17, 18])):
        if rng.random() < probability:
            out[index] = replace_level(
                out[index], int(labels[index]) - 16, rng.uniform(-1, 1)
            )
    return out


def stress_set(x, labels):
    windows, targets, parents, levels = [], [], [], []
    for index in np.flatnonzero(np.isin(labels, [16, 17, 18])):
        for level in LEVELS:
            windows.append(replace_level(x[index], int(labels[index]) - 16, level))
            targets.append(labels[index])
            parents.append(index)
            levels.append(level)
    return (
        np.array(windows, dtype=np.float32),
        np.array(targets, dtype=np.int64),
        np.array(parents, dtype=np.int64),
        np.array(levels),
    )
