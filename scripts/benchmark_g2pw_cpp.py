#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

MODEL_URL = "https://storage.googleapis.com/esun-ai/g2pW/G2PWModel-v2-onnx.zip"
SUPPORT_URLS = {
    "bopomofo_to_pinyin_wo_tune_dict.json": "https://raw.githubusercontent.com/GitYCC/g2pW/master/g2pw/bopomofo_to_pinyin_wo_tune_dict.json",
    "char_bopomofo_dict.json": "https://raw.githubusercontent.com/GitYCC/g2pW/master/g2pw/char_bopomofo_dict.json",
    "bert-base-chinese_s2t_dict.txt": "https://raw.githubusercontent.com/GitYCC/g2pW/master/g2pw/bert-base-chinese_s2t_dict.txt",
    "vocab.txt": "https://huggingface.co/bert-base-chinese/resolve/main/vocab.txt",
}
ANCHOR_CHAR = "▁"


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent.parent
    tmp_root = Path("/tmp")
    parser = argparse.ArgumentParser(
        description="Benchmark the official g2pW ONNX model on the CPP dataset."
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=tmp_root / "G2PWModel",
        help="Directory containing g2pw.onnx and the official model assets.",
    )
    parser.add_argument(
        "--support-dir",
        type=Path,
        default=tmp_root / "g2pw_support",
        help="Directory used to cache support files from the official g2pW repo.",
    )
    parser.add_argument(
        "--dataset-sent",
        type=Path,
        default=root / "benchmark" / "testdata" / "cpp" / "test.sent",
        help="CPP sentence file with anchor markers.",
    )
    parser.add_argument(
        "--dataset-label",
        type=Path,
        default=root / "benchmark" / "testdata" / "cpp" / "test.lb",
        help="CPP label file.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=0,
        help="Limit evaluated rows. Use 0 for the full dataset.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Batch size for ONNX inference.",
    )
    parser.add_argument(
        "--intra-op-threads",
        type=int,
        default=2,
        help="onnxruntime intra-op threads. Default 2 to mirror the official g2pW converter.",
    )
    parser.add_argument(
        "--tokenizer-source",
        default="bert-base-chinese",
        help="Tokenizer source passed to transformers.BertTokenizer.from_pretrained().",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=root / "benchmark_g2pw_cpp_report.txt",
        help="Text report output path.",
    )
    return parser


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, dest.open("wb") as fh:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def ensure_model(model_dir: Path) -> None:
    if (model_dir / "g2pw.onnx").exists():
        return

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    zip_path = model_dir.parent / "G2PWModel-v2-onnx.zip"
    if not zip_path.exists():
        print(f"[setup] downloading official g2pW ONNX model to {zip_path}", file=sys.stderr)
        download_file(MODEL_URL, zip_path)

    extract_parent = model_dir.parent
    extracted_dir = extract_parent / "G2PWModel"
    if extracted_dir.exists() and extracted_dir != model_dir:
        for child in extracted_dir.iterdir():
            child.unlink()
        extracted_dir.rmdir()

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_parent)

    if extracted_dir != model_dir:
        if model_dir.exists():
            for child in model_dir.iterdir():
                child.unlink()
            model_dir.rmdir()
        extracted_dir.rename(model_dir)


def ensure_support_files(support_dir: Path) -> None:
    support_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SUPPORT_URLS.items():
        path = support_dir / name
        if path.exists():
            continue
        print(f"[setup] downloading support file {name}", file=sys.stderr)
        download_file(url, path)


def require_python_deps():
    try:
        import numpy as np  # noqa: F401
        import onnxruntime as ort  # noqa: F401
        from transformers import BertTokenizer  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "missing Python dependency. Install with:\n"
            "  python -m pip install numpy onnxruntime transformers sentencepiece"
        ) from exc


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_tab_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            key, value = line.split("\t")
            out[key] = value
    return out


def load_polyphonic_entries(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            ch, phoneme = line.split("\t")
            out.append((ch, phoneme))
    return out


def bopomofo_to_pinyin(label: str, mapping: dict[str, str]) -> str:
    return f"{mapping[label[:-1]]}{label[-1]}"


def strip_tone(pinyin: str | None) -> str | None:
    if pinyin is None:
        return None
    return re.sub(r"[1-5]$", "", pinyin)


def wordize_and_map(text: str) -> tuple[list[str], list[int | None], list[tuple[int, int]]]:
    words: list[str] = []
    text_to_word: list[int | None] = []
    word_to_text: list[tuple[int, int]] = []
    rest = text
    while rest:
        match_space = re.match(r"^ +", rest)
        if match_space:
            space = match_space.group(0)
            text_to_word.extend([None] * len(space))
            rest = rest[len(space) :]
            continue

        match_alnum = re.match(r"^[a-zA-Z0-9]+", rest)
        if match_alnum:
            word = match_alnum.group(0)
            start = len(text_to_word)
            end = start + len(word)
            word_to_text.append((start, end))
            text_to_word.extend([len(words)] * len(word))
            words.append(word)
            rest = rest[len(word) :]
            continue

        start = len(text_to_word)
        end = start + 1
        word_to_text.append((start, end))
        text_to_word.append(len(words))
        words.append(rest[0])
        rest = rest[1:]

    return words, text_to_word, word_to_text


def tokenize_and_map(tokenizer, text: str) -> tuple[list[str], list[int | None], list[tuple[int, int]]]:
    words, text_to_word, word_to_text = wordize_and_map(text)
    tokens: list[str] = []
    token_to_text: list[tuple[int, int]] = []

    for word, (start, end) in zip(words, word_to_text):
        word_tokens = tokenizer.tokenize(word)
        if not word_tokens or word_tokens == ["[UNK]"]:
            token_to_text.append((start, end))
            tokens.append("[UNK]")
            continue

        cursor = start
        for word_token in word_tokens:
            token_len = len(re.sub(r"^##", "", word_token))
            token_to_text.append((cursor, cursor + token_len))
            cursor += token_len
            tokens.append(word_token)

    text_to_token = list(text_to_word)
    for token_index, (start, end) in enumerate(token_to_text):
        for pos in range(start, end):
            text_to_token[pos] = token_index

    return tokens, text_to_token, token_to_text


def truncate_text_window(text: str, query_id: int, window_size: int) -> tuple[str, int]:
    start = max(0, query_id - window_size // 2)
    end = min(len(text), query_id + window_size // 2)
    return text[start:end], query_id - start


def truncate_tokens(
    max_len: int,
    text: str,
    query_id: int,
    tokens: list[str],
    text_to_token: list[int | None],
    token_to_text: list[tuple[int, int]],
) -> tuple[str, int, list[str], list[int | None], list[tuple[int, int]]]:
    truncate_len = max_len - 2
    if len(tokens) <= truncate_len:
        return text, query_id, tokens, text_to_token, token_to_text

    token_position = text_to_token[query_id]
    if token_position is None:
        raise ValueError(f"query position {query_id} did not map to a token")

    token_start = token_position - truncate_len // 2
    token_end = token_start + truncate_len
    front_exceed = -token_start
    back_exceed = token_end - len(tokens)

    if front_exceed > 0:
        token_start += front_exceed
        token_end += front_exceed
    elif back_exceed > 0:
        token_start -= back_exceed
        token_end -= back_exceed

    start = token_to_text[token_start][0]
    end = token_to_text[token_end - 1][1]
    return (
        text[start:end],
        query_id - start,
        tokens[token_start:token_end],
        [pos - token_start if pos is not None else None for pos in text_to_token[start:end]],
        [(s - start, e - start) for s, e in token_to_text[token_start:token_end]],
    )


def load_dataset(sent_path: Path, label_path: Path, rows: int) -> tuple[list[str], list[str]]:
    with sent_path.open(encoding="utf-8") as sent_fh:
        sents = [line.rstrip("\n") for line in sent_fh if line.rstrip("\n")]
    with label_path.open(encoding="utf-8") as label_fh:
        labels = [line.rstrip("\n") for line in label_fh if line.rstrip("\n")]

    if len(sents) != len(labels):
        raise SystemExit(
            f"dataset mismatch: {sent_path} has {len(sents)} rows but {label_path} has {len(labels)} rows"
        )

    if rows > 0:
        return sents[:rows], labels[:rows]
    return sents, labels


def build_case(
    raw_text: str,
    tokenizer,
    s2t_map: dict[str, str],
    char_to_candidates: dict[str, list[int]],
    char_to_id: dict[str, int],
    label_count: int,
    monophonic_map: dict[str, str],
    char_default_map: dict[str, list[str]],
):
    import numpy as np

    query_id = raw_text.index(ANCHOR_CHAR)
    text = raw_text.replace(ANCHOR_CHAR, "")
    text = "".join(s2t_map.get(ch, ch) for ch in text).lower()
    text, query_id = truncate_text_window(text, query_id, 32)
    query_char = text[query_id]

    if query_char not in char_to_candidates:
        if query_char in monophonic_map:
            return {"mode": "mono", "pred": monophonic_map[query_char]}
        if query_char in char_default_map and char_default_map[query_char]:
            return {"mode": "char_default", "pred": char_default_map[query_char][0]}
        return {"mode": "unknown", "pred": None}

    tokens, text_to_token, token_to_text = tokenize_and_map(tokenizer, text)
    text, query_id, tokens, text_to_token, token_to_text = truncate_tokens(
        512, text, query_id, tokens, text_to_token, token_to_text
    )
    query_char = text[query_id]
    processed_tokens = ["[CLS]"] + tokens + ["[SEP]"]
    input_ids = np.asarray(tokenizer.convert_tokens_to_ids(processed_tokens), dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)
    attention_mask = np.ones_like(input_ids)
    phoneme_mask = np.zeros(label_count, dtype=np.float32)
    for idx in char_to_candidates[query_char]:
        phoneme_mask[idx] = 1.0

    return {
        "mode": "onnx",
        "pred": None,
        "input_ids": input_ids,
        "token_type_ids": token_type_ids,
        "attention_mask": attention_mask,
        "phoneme_mask": phoneme_mask,
        "char_id": char_to_id[query_char],
        "position_id": text_to_token[query_id] + 1,
    }


def make_batches(cases: list[dict], batch_size: int):
    import numpy as np

    indexed_cases = [(row_id, case) for row_id, case in enumerate(cases) if case["mode"] == "onnx"]
    batches = []
    for start in range(0, len(indexed_cases), batch_size):
        chunk = indexed_cases[start : start + batch_size]
        max_len = max(len(case["input_ids"]) for _, case in chunk)
        batch_len = len(chunk)
        input_ids = np.zeros((batch_len, max_len), dtype=np.int64)
        token_type_ids = np.zeros((batch_len, max_len), dtype=np.int64)
        attention_mask = np.zeros((batch_len, max_len), dtype=np.int64)
        phoneme_mask = np.stack([case["phoneme_mask"] for _, case in chunk])
        char_ids = np.asarray([case["char_id"] for _, case in chunk], dtype=np.int64)
        position_ids = np.asarray([case["position_id"] for _, case in chunk], dtype=np.int64)
        row_ids: list[int] = []

        for batch_row, (row_id, case) in enumerate(chunk):
            token_count = len(case["input_ids"])
            input_ids[batch_row, :token_count] = case["input_ids"]
            token_type_ids[batch_row, :token_count] = case["token_type_ids"]
            attention_mask[batch_row, :token_count] = case["attention_mask"]
            row_ids.append(row_id)

        batches.append(
            (
                row_ids,
                {
                    "input_ids": input_ids,
                    "token_type_ids": token_type_ids,
                    "attention_mask": attention_mask,
                    "phoneme_mask": phoneme_mask,
                    "char_ids": char_ids,
                    "position_ids": position_ids,
                },
            )
        )

    return batches


def run_inference(session, cases: list[dict], batches, labels: list[str], bp_map: dict[str, str]) -> list[str | None]:
    preds = [case["pred"] for case in cases]
    for row_ids, batch in batches:
        probs = session.run([], batch)[0]
        pred_indices = probs.argmax(axis=-1)
        for row_id, label_idx in zip(row_ids, pred_indices.tolist()):
            preds[row_id] = bopomofo_to_pinyin(labels[label_idx], bp_map)
    return preds


def build_report(args: argparse.Namespace, metrics: dict) -> str:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    mode_counts = metrics["mode_counts"]
    lines = [
        "g2pw_onnx cpp benchmark",
        f"timestamp_utc: {now}",
        f"model_dir: {args.model_dir}",
        f"support_dir: {args.support_dir}",
        f"dataset_sent: {args.dataset_sent}",
        f"dataset_label: {args.dataset_label}",
        f"rows: {metrics['rows']}",
        f"batch_size: {args.batch_size}",
        f"intra_op_threads: {args.intra_op_threads}",
        f"tokenizer_source: {args.tokenizer_source}",
        f"mode_counts: onnx={mode_counts.get('onnx', 0)}, mono={mode_counts.get('mono', 0)}, char_default={mode_counts.get('char_default', 0)}, unknown={mode_counts.get('unknown', 0)}",
        f"tone_accuracy_all: {metrics['tone_accuracy_all']:.6f}",
        f"toneless_accuracy_all: {metrics['toneless_accuracy_all']:.6f}",
        f"tone_accuracy_onnx_only: {metrics['tone_accuracy_onnx_only']:.6f}",
        f"toneless_accuracy_onnx_only: {metrics['toneless_accuracy_onnx_only']:.6f}",
        f"load_ms: {metrics['load_ms']:.3f}",
        f"prepare_ms: {metrics['prepare_ms']:.3f}",
        f"predict_cold_ms: {metrics['predict_cold_ms']:.3f}",
        f"predict_warm_ms: {metrics['predict_warm_ms']:.3f}",
        f"first_run_total_ms: {metrics['first_run_total_ms']:.3f}",
        f"warm_predict_ms_per_row: {metrics['warm_predict_ms_per_row']:.6f}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    require_python_deps()

    import numpy as np  # noqa: F401
    import onnxruntime as ort
    from transformers import BertTokenizer

    ensure_model(args.model_dir)
    ensure_support_files(args.support_dir)

    if not args.dataset_sent.exists():
        raise SystemExit(f"missing dataset sentence file: {args.dataset_sent}")
    if not args.dataset_label.exists():
        raise SystemExit(f"missing dataset label file: {args.dataset_label}")

    bp_map = load_json(args.support_dir / "bopomofo_to_pinyin_wo_tune_dict.json")
    char_default_bp = load_json(args.support_dir / "char_bopomofo_dict.json")
    char_default_py = {
        ch: [bopomofo_to_pinyin(bp, bp_map) for bp in bps]
        for ch, bps in char_default_bp.items()
    }
    s2t_map = load_tab_map(args.support_dir / "bert-base-chinese_s2t_dict.txt")

    poly_entries = load_polyphonic_entries(args.model_dir / "POLYPHONIC_CHARS.txt")
    mono_entries = load_tab_map(args.model_dir / "MONOPHONIC_CHARS.txt")
    mono_pinyin = {ch: bopomofo_to_pinyin(bp, bp_map) for ch, bp in mono_entries.items()}

    labels = sorted({phoneme for _, phoneme in poly_entries})
    label_to_idx = {label: idx for idx, label in enumerate(labels)}
    char_to_candidates: dict[str, list[int]] = {}
    for ch, phoneme in poly_entries:
        char_to_candidates.setdefault(ch, []).append(label_to_idx[phoneme])
    char_to_id = {ch: idx for idx, ch in enumerate(sorted(char_to_candidates))}

    dataset_sents, dataset_labels = load_dataset(args.dataset_sent, args.dataset_label, args.rows)

    load_start = time.perf_counter()
    tokenizer = BertTokenizer.from_pretrained(args.tokenizer_source)
    session_options = ort.SessionOptions()
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.intra_op_num_threads = args.intra_op_threads
    session = ort.InferenceSession(str(args.model_dir / "g2pw.onnx"), sess_options=session_options)
    load_ms = (time.perf_counter() - load_start) * 1000

    prepare_start = time.perf_counter()
    cases = [
        build_case(
            raw_text=raw_text,
            tokenizer=tokenizer,
            s2t_map=s2t_map,
            char_to_candidates=char_to_candidates,
            char_to_id=char_to_id,
            label_count=len(labels),
            monophonic_map=mono_pinyin,
            char_default_map=char_default_py,
        )
        for raw_text in dataset_sents
    ]
    batches = make_batches(cases, args.batch_size)
    prepare_ms = (time.perf_counter() - prepare_start) * 1000

    cold_start = time.perf_counter()
    preds = run_inference(session, cases, batches, labels, bp_map)
    predict_cold_ms = (time.perf_counter() - cold_start) * 1000

    warm_start = time.perf_counter()
    preds_warm = run_inference(session, cases, batches, labels, bp_map)
    predict_warm_ms = (time.perf_counter() - warm_start) * 1000
    if preds != preds_warm:
        raise SystemExit("warm run predictions differ from cold run predictions")

    mode_counts = Counter(case["mode"] for case in cases)
    tone_accuracy_all = sum(pred == gold for pred, gold in zip(preds, dataset_labels)) / len(dataset_labels)
    toneless_accuracy_all = (
        sum(strip_tone(pred) == strip_tone(gold) for pred, gold in zip(preds, dataset_labels))
        / len(dataset_labels)
    )

    onnx_pairs = [
        (pred, gold)
        for pred, gold, case in zip(preds, dataset_labels, cases)
        if case["mode"] == "onnx"
    ]
    tone_accuracy_onnx_only = sum(pred == gold for pred, gold in onnx_pairs) / max(len(onnx_pairs), 1)
    toneless_accuracy_onnx_only = (
        sum(strip_tone(pred) == strip_tone(gold) for pred, gold in onnx_pairs) / max(len(onnx_pairs), 1)
    )

    metrics = {
        "rows": len(dataset_labels),
        "mode_counts": dict(mode_counts),
        "tone_accuracy_all": tone_accuracy_all,
        "toneless_accuracy_all": toneless_accuracy_all,
        "tone_accuracy_onnx_only": tone_accuracy_onnx_only,
        "toneless_accuracy_onnx_only": toneless_accuracy_onnx_only,
        "load_ms": load_ms,
        "prepare_ms": prepare_ms,
        "predict_cold_ms": predict_cold_ms,
        "predict_warm_ms": predict_warm_ms,
        "first_run_total_ms": load_ms + prepare_ms + predict_cold_ms,
        "warm_predict_ms_per_row": predict_warm_ms / len(dataset_labels),
    }

    report = build_report(args, metrics)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
