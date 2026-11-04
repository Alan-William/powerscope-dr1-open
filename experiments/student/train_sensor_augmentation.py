"""Fixed-budget local training, using existing data only; never evaluates E2."""

import os

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
import argparse
import hashlib
import json
from pathlib import Path
import time
import numpy as np
import onnxruntime as ort
import torch
from torch import nn
import torch.nn.functional as F
from network import Student, inputs
from export_qdq import export
from augment_sensor import augment, stress_set, LEVELS
from analyze_acceptance import metrics as classification_metrics

DATA_SHA = "a9849da35f01595e201325295ffadd395e49dfa59ba3c1149e77b217ad6a5b2b"
TEACHER_SHA = "6566a8ea25f6817a5f9eb0202971c328eafe916c14bcfa6ad969a57eb98652c0"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def metrics(labels, logits):
    return classification_metrics(labels, np.asarray(logits).argmax(1))


def dump(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def infer(model, x):
    with torch.inference_mode():
        return torch.cat([model(part) for part in x.split(128)]).cpu().numpy()


def score(clean, stress):
    return (
        min(clean["accuracy"], stress["accuracy"]),
        clean["macro_f1"],
        clean["accuracy"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--teacher", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    assert sha(args.dataset) == DATA_SHA and sha(args.teacher) == TEACHER_SHA
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    assert torch.cuda.is_available(), "This local run requires an available CUDA GPU"
    torch.cuda.set_per_process_memory_fraction(0.35)
    device = "cuda"
    d = np.load(args.dataset, allow_pickle=False)
    ti, vi = [np.flatnonzero(d["split"] == split) for split in (0, 1)]
    assert len(ti) == 880 and len(vi) == 1100
    xtr, ytr = d["X"][ti].astype("f4"), d["y"][ti].astype("i8")
    xv, yv = d["X"][vi].astype("f4"), d["y"][vi].astype("i8")
    d.close()
    xs, ys, parents, levels = stress_set(xv, yv)
    assert len(xs) == 1800
    xc, _, _, _ = stress_set(xtr, ytr)
    calibration = inputs(np.concatenate([xtr, xc]))
    val_np, stress_np = inputs(xv), inputs(xs)
    val = torch.from_numpy(val_np).to(device)
    stress = torch.from_numpy(stress_np).to(device)
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    teacher = ort.InferenceSession(
        str(args.teacher), so, providers=["CPUExecutionProvider"]
    )

    def teach(x):
        return np.concatenate([teacher.run(None, {"waveform": w[None]})[0] for w in x])

    tz = teach(xtr)
    tv = teach(xv)
    assert int((tv.argmax(1) == yv).sum()) == 1097
    arms = ["ce", "aug_ce", "aug_teacher_correct_kd"]
    seeds = [0, 1, 2]
    epochs = 120
    if args.smoke:
        arms, seeds, epochs = ["aug_teacher_correct_kd"], [0], 2
    protocol = dict(
        schema=1,
        smoke=args.smoke,
        dataset_sha256=DATA_SHA,
        teacher_onnx_sha256=TEACHER_SHA,
        train_indices_sha256=hashlib.sha256(ti.tobytes()).hexdigest(),
        validation_indices_sha256=hashlib.sha256(vi.tobytes()).hexdigest(),
        counts=dict(train=880, clean_validation=1100, synthetic_sf_validation=1800),
        arms=arms,
        seeds=seeds,
        epochs=epochs,
        updates_per_epoch=7,
        batch_size=128,
        augmentation=dict(
            target_labels=[16, 17, 18],
            probability=0.75,
            level_range=[-1, 1],
            validation_levels=LEVELS,
            residual_noise="retained then shared normalization",
            scope="Observation augmentation only. Other-phase controller response not re-simulated. Synthetic derivatives are not new independent events.",
        ),
        statistics=False,
        architecture="unchanged 26,486-parameter deployable student",
        optimizer="AdamW lr=.003 weight_decay=.0001; cosine eta_min=.00003",
        distillation="T=2; 0.5 CE +0.5 KL when frozen teacher top1 matches training label, else CE",
        selection="FP32 checkpoints and QDQ candidates: maximize min(clean-val accuracy, synthetic-SF accuracy), then clean macro-F1, clean accuracy, then negative clean CE; exact ties keep first fixed arm/seed",
        calibration="original plus fixed-level augmented training only, same pool for all arms",
        test_policy="No E2 fitting, calibration, checkpoint or candidate selection. E2 previously examined; any later result is retrospective, not new blind evidence.",
        execution="Local Windows CUDA, per user's cancellation of Ubuntu/simulation work",
        runtime=dict(
            torch=torch.__version__,
            numpy=np.__version__,
            ort=ort.__version__,
            device=torch.cuda.get_device_name(0),
        ),
        source_sha256={
            name: sha(Path(__file__).parent / name)
            for name in [
                "train_sensor_augmentation.py",
                "augment_sensor.py",
                "network.py",
                "export_qdq.py",
                "analyze_acceptance.py",
            ]
        },
    )
    dump(args.output / "protocol.json", protocol)
    np.savez(
        args.output / "development-reference.npz",
        train_indices=ti,
        validation_indices=vi,
        clean_labels=yv,
        stress_labels=ys,
        stress_parent=parents,
        stress_levels=levels,
    )
    dump(args.output / "teacher-validation.json", metrics(yv, tv))
    results = []
    start = time.monotonic()
    for arm in arms:
        for seed in seeds:
            folder = args.output / f"{arm}-s{seed}"
            folder.mkdir()
            torch.manual_seed(1000 + seed)
            model = Student(rep=False).to(device)
            optimizer = torch.optim.AdamW(
                model.parameters(), lr=0.003, weight_decay=0.0001
            )
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, epochs, eta_min=0.00003
            )
            order_rng = np.random.default_rng(1000 + seed)
            aug_rng = np.random.default_rng(20261101 + seed)
            best = (-1.0, -1.0, -1.0, -float("inf"))
            history = []
            run_start = time.monotonic()
            for epoch in range(epochs):
                model.train()
                loss_sum = 0.0
                for ids in np.array_split(order_rng.permutation(880), 7):
                    raw = (
                        xtr[ids]
                        if arm == "ce"
                        else augment(xtr[ids], ytr[ids], aug_rng)
                    )
                    batch = torch.from_numpy(inputs(raw)).to(device)
                    target = torch.from_numpy(ytr[ids]).to(device)
                    optimizer.zero_grad(set_to_none=True)
                    z = model(batch)
                    ce = F.cross_entropy(z, target, reduction="none")
                    loss = ce.mean()
                    if arm == "aug_teacher_correct_kd":
                        logits = tz[ids].copy()
                        changed = np.any(raw != xtr[ids], axis=(1, 2))
                        if changed.any():
                            logits[changed] = teach(raw[changed])
                        t = torch.from_numpy(logits).to(device)
                        trust = t.argmax(1) == target
                        kl = 4 * F.kl_div(
                            F.log_softmax(z / 2, 1),
                            F.softmax(t / 2, 1),
                            reduction="none",
                        ).sum(1)
                        loss = torch.where(trust, 0.5 * ce + 0.5 * kl, ce).mean()
                    loss.backward()
                    optimizer.step()
                    loss_sum += float(loss.detach()) * len(ids)
                scheduler.step()
                model.eval()
                vz, sz = infer(model, val), infer(model, stress)
                clean, challenge = metrics(yv, vz), metrics(ys, sz)
                vce = float(F.cross_entropy(torch.from_numpy(vz), torch.from_numpy(yv)))
                key = score(clean, challenge) + (-vce,)
                history.append(
                    dict(
                        epoch=epoch + 1,
                        loss=loss_sum / 880,
                        validation=clean["accuracy"],
                        synthetic_sf=challenge["accuracy"],
                        validation_ce=vce,
                    )
                )
                if key > best:
                    best = key
                    torch.save(
                        dict(
                            state={
                                k: v.detach().cpu()
                                for k, v in model.state_dict().items()
                            },
                            rep=False,
                            features="none",
                            seed=seed,
                            arm=arm,
                            epoch=epoch + 1,
                        ),
                        folder / "best.pt",
                    )
                if (epoch + 1) % 20 == 0 or epoch + 1 == epochs:
                    progress = dict(
                        arm=arm,
                        seed=seed,
                        epoch=epoch + 1,
                        validation=clean["accuracy"],
                        synthetic_sf=challenge["accuracy"],
                        seconds=time.monotonic() - run_start,
                    )
                    dump(args.output / "progress.json", progress)
                    print(json.dumps(progress), flush=True)
            checkpoint = torch.load(
                folder / "best.pt", map_location="cpu", weights_only=True
            )
            model.load_state_dict(checkpoint["state"])
            model.eval()
            vz, sz = infer(model, val), infer(model, stress)
            deploy = model.deploy()
            fused = infer(deploy, torch.from_numpy(val_np))
            fusion_error = float(np.abs(fused - vz).max())
            assert fusion_error < 0.001
            trunk = nn.Sequential(*list(deploy.children())[:-2])
            head = nn.Sequential(list(deploy.children())[-1])
            q = export(trunk, calibration, folder / "trunk.onnx")
            mid = infer(trunk, torch.from_numpy(calibration)).reshape(
                len(calibration), -1
            )
            export(
                head,
                mid,
                folder / "head.onnx",
                input_quant=(np.float32(q[-1]["scale"]), np.uint8(q[-1]["zero_point"])),
            )
            export(deploy, calibration, folder / "full.onnx")
            quant = ort.InferenceSession(
                str(folder / "full.onnx"), so, providers=["CPUExecutionProvider"]
            )
            qv = np.concatenate(
                [quant.run(None, {"input": x[None]})[0] for x in val_np]
            )
            qs = np.concatenate(
                [quant.run(None, {"input": x[None]})[0] for x in stress_np]
            )
            result = dict(
                arm=arm,
                seed=seed,
                best_epoch=checkpoint["epoch"],
                epochs=epochs,
                updates=epochs * 7,
                fp32=metrics(yv, vz),
                fp32_synthetic_sf=metrics(ys, sz),
                qdq=metrics(yv, qv),
                qdq_synthetic_sf=metrics(ys, qs),
                fusion_max_abs=fusion_error,
                validation_ce=float(
                    F.cross_entropy(torch.from_numpy(vz), torch.from_numpy(yv))
                ),
                seconds=time.monotonic() - run_start,
                deploy_parameters=sum(p.numel() for p in deploy.parameters()),
            )
            np.savez(
                folder / "validation-predictions.npz",
                fp32=vz,
                qdq=qv,
                stress_fp32=sz,
                stress_qdq=qs,
            )
            dump(folder / "history.json", history)
            result["artifacts_sha256"] = {
                p.name: sha(p)
                for p in folder.iterdir()
                if p.suffix in [".pt", ".onnx", ".npz"]
            }
            dump(folder / "result.json", result)
            results.append(result)
            dump(args.output / "results.json", results)
            print(
                json.dumps(
                    dict(
                        completed=f"{arm}-s{seed}",
                        clean_qdq=result["qdq"]["accuracy"],
                        synthetic_sf_qdq=result["qdq_synthetic_sf"]["accuracy"],
                    )
                ),
                flush=True,
            )
            del model, deploy, trunk, head, optimizer, quant
            torch.cuda.empty_cache()
    chosen = max(
        results,
        key=lambda r: score(r["qdq"], r["qdq_synthetic_sf"]) + (-r["validation_ce"],),
    )
    selected = dict(
        arm=chosen["arm"],
        seed=chosen["seed"],
        directory=f"{chosen['arm']}-s{chosen['seed']}",
        selection_uses_test=False,
        protocol_sha256=sha(args.output / "protocol.json"),
        artifacts_sha256=chosen["artifacts_sha256"],
        smoke=args.smoke,
    )
    dump(args.output / "selected.json", selected)
    dump(
        args.output / "complete.json",
        dict(
            runs=len(results),
            seconds=time.monotonic() - start,
            test_evaluated=False,
            smoke=args.smoke,
        ),
    )
    print(
        json.dumps(
            dict(selected=selected["directory"], seconds=time.monotonic() - start)
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
