"""Read-only signal audit after heldout failure; does not change the model/data.

Reports raw DF/SF distinguishability and normalized window statistics. These
are audit measurements, not implemented model/PL statistical features.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.io import loadmat


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--raw", type=Path, required=True)
    a = p.parse_args()
    ds = Path("/home/alan-william/xu2026/data/dataset_v4/dataset.npz")
    d = np.load(ds, allow_pickle=False)
    raw = a.raw
    sources = {
        r["file"]: r["sha256"]
        for r in json.loads(ds.with_name("splits.json").read_text())["sources"]
    }
    results = []
    for phase in range(3):
        for event in [1, 2]:
            for condition in range(1, 9):
                pair = []
                paths = []
                for label in [13 + phase, 16 + phase]:
                    name = (
                        f"L{label:02d}_C{condition}"
                        + ("" if event == 1 else f"_E{event}")
                        + ".mat"
                    )
                    path = raw / name
                    paths.append(path)
                    assert (
                        hashlib.sha256(path.read_bytes()).hexdigest() == sources[name]
                    ), name
                    pair.append(
                        loadmat(
                            path,
                            squeeze_me=True,
                            struct_as_record=False,
                            variable_names=["i_meas", "t", "meta"],
                        )
                    )
                df, sf = pair
                assert np.array_equal(df["t"], sf["t"])
                # Dataset uses a 150 ms post-fault guard.
                use = (
                    df["t"]
                    >= max(float(df["meta"].t_fault_s), float(sf["meta"].t_fault_s))
                    + 0.150
                )
                x = np.asarray(df["i_meas"])[use]
                y = np.asarray(sf["i_meas"])[use]
                ix = np.flatnonzero(
                    (d["event"] == event)
                    & (d["cond"] == condition)
                    & (d["y"] == 16 + phase)
                )
                results.append(
                    dict(
                        phase="ABC"[phase],
                        event=event,
                        condition=condition,
                        fault_time_s=float(sf["meta"].t_fault_s),
                        sf_stuck_raw=float(np.mean(y[:, phase])),
                        df_raw_mean=float(np.mean(x[:, phase])),
                        sf_raw_std=float(np.std(y[:, phase])),
                        df_vs_sf_raw_max_abs_difference=float(np.max(np.abs(x - y))),
                        df_vs_sf_other_phases_max_abs_difference=float(
                            np.max(np.abs(np.delete(x - y, phase, axis=1)))
                        ),
                        normalized_sf_phase_window_mean_median=float(
                            np.median(d["X"][ix, phase].mean(-1))
                        ),
                        normalized_sf_phase_window_std_median=float(
                            np.median(d["X"][ix, phase].std(-1))
                        ),
                        source_sha256={
                            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in paths
                        },
                    )
                )
    report = dict(
        scope="Post-hoc read-only diagnosis of holdout errors, not training or a new blind test",
        dataset_sha256=hashlib.sha256(ds.read_bytes()).hexdigest(),
        records=results,
    )
    a.output.parent.mkdir(parents=True, exist_ok=True)
    assert not a.output.exists()
    a.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            [r for r in results if r["phase"] == "B" and r["event"] == 2], indent=2
        )
    )


if __name__ == "__main__":
    main()
