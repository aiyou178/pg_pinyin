#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np


TENSOR_NAMES = {
    "embedding.weight": "embedding_weight",
    "lstm.weight_ih_l0": "weight_ih",
    "lstm.weight_hh_l0": "weight_hh",
    "lstm.bias_ih_l0": "bias_ih",
    "lstm.bias_hh_l0": "bias_hh",
    "lstm.weight_ih_l0_reverse": "weight_ih_reverse",
    "lstm.weight_hh_l0_reverse": "weight_hh_reverse",
    "lstm.bias_ih_l0_reverse": "bias_ih_reverse",
    "lstm.bias_hh_l0_reverse": "bias_hh_reverse",
    "logit_layer.0.weight": "hidden_weight_l0",
    "logit_layer.0.bias": "hidden_bias_l0",
    "logit_layer.2.weight": "hidden_weight_l1",
    "logit_layer.2.bias": "hidden_bias_l1",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the official g2pM wheel into pg_pinyin's compact manifest format."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory that will receive manifest.json and the packed weights blob.",
    )
    parser.add_argument(
        "--wheel",
        help="Optional local g2pM wheel path. If omitted, the script downloads the latest wheel from PyPI.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output directory if it already exists.",
    )
    return parser.parse_args()


def ensure_wheel_path(temp_dir: Path, explicit_wheel: str | None) -> Path:
    if explicit_wheel:
        wheel = Path(explicit_wheel).expanduser().resolve()
        if not wheel.is_file():
            raise FileNotFoundError(f"wheel does not exist: {wheel}")
        return wheel

    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--no-deps", "g2pM", "-d", str(temp_dir)],
        check=True,
    )
    matches = sorted(glob.glob(str(temp_dir / "g2pM-*.whl")))
    if not matches:
        raise FileNotFoundError("failed to download g2pM wheel from PyPI")
    return Path(matches[-1])


def load_pickle(path: Path):
    with path.open("rb") as fh:
        return pickle.load(fh)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if output_dir.exists():
        if not args.force:
            raise SystemExit(
                f"output directory already exists: {output_dir}. Pass --force to replace it."
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pg_pinyin_g2pm_export_") as temp:
        temp_dir = Path(temp)
        wheel_path = ensure_wheel_path(temp_dir, args.wheel)
        unpack_dir = temp_dir / "unpack"
        with zipfile.ZipFile(wheel_path) as zf:
            zf.extractall(unpack_dir)

        package_dir = unpack_dir / "g2pM"
        if not package_dir.is_dir():
            raise FileNotFoundError(f"g2pM package directory not found in wheel: {wheel_path}")

        char2idx = load_pickle(package_dir / "char2idx.pkl")
        class2idx = load_pickle(package_dir / "class2idx.pkl")
        state_dict = load_pickle(package_dir / "np_ckpt.pkl")

        idx2class = [None] * len(class2idx)
        for label, idx in class2idx.items():
            idx2class[idx] = label

        weights_path = output_dir / "weights.f32.bin"
        tensors: dict[str, dict[str, object]] = {}
        offset_bytes = 0
        with weights_path.open("wb") as weights_fh:
            for source_name, alias in TENSOR_NAMES.items():
                if source_name not in state_dict:
                    raise KeyError(f"missing tensor in g2pM checkpoint: {source_name}")
                array = np.asarray(state_dict[source_name], dtype="<f4")
                blob = array.reshape(-1).tobytes(order="C")
                weights_fh.write(blob)
                tensors[alias] = {
                    "shape": list(array.shape),
                    "offset_bytes": offset_bytes,
                    "byte_length": len(blob),
                }
                offset_bytes += len(blob)

        manifest = {
            "format": "g2pm_export_v2",
            "source": {
                "package": "g2pM",
                "wheel": wheel_path.name,
            },
            "weights_path": weights_path.name,
            "char2idx": char2idx,
            "idx2class": idx2class,
            "tensors": tensors,
        }

        manifest_path = output_dir / "manifest.json"
        with manifest_path.open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)

        print(manifest_path)


if __name__ == "__main__":
    main()
