import unittest
import numpy as np
from experiments.student.augment_sensor import (
    augment,
    replace_level,
    stress_set,
    LEVELS,
)


class AugmentationContract(unittest.TestCase):
    def sample(self, phase):
        t = np.arange(500)
        x = np.stack([0.8 * np.sin(t / 63.0 + p * 2.1) for p in range(3)]).astype("f4")
        x[phase] = 0.2 + 0.001 * np.sin(t)
        return x

    def test_symmetric_phases_and_residual_noise(self):
        for phase in range(3):
            x = self.sample(phase)
            z = replace_level(x, phase, -0.6)
            scale = max(abs(x[q]).max() for q in range(3) if q != phase)
            np.testing.assert_allclose(z[phase].mean(), -0.6 / scale, atol=1e-5)
            np.testing.assert_allclose(
                np.diff(z[phase]), np.diff(x[phase]) / scale, atol=1e-7
            )
            for q in range(3):
                if q != phase:
                    np.testing.assert_allclose(z[q], x[q] / scale, atol=1e-7)
            self.assertLessEqual(np.abs(z).max(), 1)

    def test_only_sf_changes_and_input_is_immutable(self):
        x = np.stack([self.sample(0), self.sample(1), self.sample(2)])
        old = x.copy()
        y = np.array([0, 17, 21])
        a = augment(x, y, np.random.default_rng(1), 1)
        b = augment(x, y, np.random.default_rng(1), 1)
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(x, old)
        np.testing.assert_array_equal(a[[0, 2]], x[[0, 2]])
        self.assertFalse(np.array_equal(a[1], x[1]))

    def test_stress_sources_are_explicit_not_independent_samples(self):
        x = np.stack([self.sample(p) for p in range(3)])
        z, y, parents, levels = stress_set(x, np.array([16, 17, 18]))
        self.assertEqual(z.shape, (3 * len(LEVELS), 3, 500))
        np.testing.assert_array_equal(parents, np.repeat(range(3), len(LEVELS)))
        np.testing.assert_array_equal(y, 16 + parents)
        np.testing.assert_array_equal(levels, np.tile(LEVELS, 3))
        with self.assertRaises(ValueError):
            replace_level(x[0], 1, 0.5)


if __name__ == "__main__":
    unittest.main()
