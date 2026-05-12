#!/usr/bin/env python3
"""Compare tokenizer ground truth vs Rust binary output using dictionary hit rate.

Requires:
  - sdcv            (pacman -S sdcv)
  - dank-lookup-tokenize binary (cargo build --release in tokenizer/)

Usage:
    python3 tests/tokenizer-bench/compare.py
    python3 tests/tokenizer-bench/compare.py --tokenizer /path/to/dank-lookup-tokenize
"""
import json, subprocess, os, sys, argparse
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
GT_ZH = BENCH_DIR / "ground-truth" / "zh-jieba.json"
GT_JA = BENCH_DIR / "ground-truth" / "ja-manual.json"
TOKENIZER_DEFAULT = BENCH_DIR.parent.parent / "tokenizer" / "target" / "release" / "dank-lookup-tokenize"

# Dictionaries to check hits against, per language
DICTS = {
    "zh": ["萌典重編國語辭典", "CC-CEDICT 漢英辭典"],
    "ja": ["JMdict", "reader.dict JA"],
}

STARDICT_DATA_DIR = os.path.expanduser("~/.local/share/stardict/dic")


def sdcv_has_hit(token, dicts):
    """Check if any dictionary has an entry for this token."""
    env = os.environ.copy()
    env["STARDICT_DATA_DIR"] = STARDICT_DATA_DIR
    for d in dicts:
        try:
            result = subprocess.run(
                ["sdcv", "-n", "--json", "--use-dict", d, token],
                capture_output=True, text=True, timeout=5, env=env,
            )
            out = result.stdout.strip()
            if out and out != "[]":
                entries = json.loads(out)
                # Only count exact matches
                if any(e.get("word", "").strip() == token for e in entries):
                    return True
        except Exception:
            pass
    return False


def run_tokenizer(tokenizer_path, lang, text):
    """Run the Rust tokenizer binary and return token list."""
    try:
        result = subprocess.run(
            [str(tokenizer_path), "--lang", lang, "--json", text],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"  tokenizer error: {result.stderr.strip()}", file=sys.stderr)
            return None
        data = json.loads(result.stdout.strip())
        return data.get("tokens", [])
    except Exception as e:
        print(f"  tokenizer exception: {e}", file=sys.stderr)
        return None


def compute_hit_rate(tokens, dicts):
    """Return (hits, total, hits_excl_single, total_excl_single, details)."""
    details = []
    hits = 0
    total = len(tokens)
    hits_excl = 0
    total_excl = 0

    for token in tokens:
        is_single = len(token) == 1
        hit = sdcv_has_hit(token, dicts)
        if hit:
            hits += 1
        if not is_single:
            total_excl += 1
            if hit:
                hits_excl += 1
        details.append({
            "token": token,
            "hit": hit,
            "single_char": is_single,
        })

    return hits, total, hits_excl, total_excl, details


def format_pct(num, denom):
    if denom == 0:
        return "n/a"
    return f"{num}/{denom} ({100 * num / denom:.0f}%)"


def run_suite(lang, gt_path, tokenizer_path):
    """Run comparison for one language."""
    if not gt_path.exists():
        print(f"  ground truth not found: {gt_path}")
        return None

    entries = json.loads(gt_path.read_text())
    dicts = DICTS[lang]

    print(f"\n{'=' * 60}")
    print(f"  {lang.upper()} — {len(entries)} phrases, dicts: {', '.join(dicts)}")
    print(f"{'=' * 60}")

    gt_hits_all = 0
    gt_total_all = 0
    gt_hits_excl = 0
    gt_total_excl = 0
    rs_hits_all = 0
    rs_total_all = 0
    rs_hits_excl = 0
    rs_total_excl = 0
    phrases_with_diff = 0

    for entry in entries:
        inp = entry["input"]
        gt_tokens = entry["tokens"]

        # Get Rust binary tokens
        rs_tokens = run_tokenizer(tokenizer_path, lang, inp)
        if rs_tokens is None:
            rs_tokens = []

        # Compute hit rates
        gt_h, gt_t, gt_he, gt_te, gt_details = compute_hit_rate(gt_tokens, dicts)
        rs_h, rs_t, rs_he, rs_te, rs_details = compute_hit_rate(rs_tokens, dicts)

        gt_hits_all += gt_h
        gt_total_all += gt_t
        gt_hits_excl += gt_he
        gt_total_excl += gt_te
        rs_hits_all += rs_h
        rs_total_all += rs_t
        rs_hits_excl += rs_he
        rs_total_excl += rs_te

        tokens_match = gt_tokens == rs_tokens
        if not tokens_match:
            phrases_with_diff += 1

        # Per-phrase report
        print(f"\n  Input: {inp}")
        print(f"  GT tokens:   {gt_tokens}")
        print(f"  Rust tokens: {rs_tokens}")
        if tokens_match:
            print(f"  Tokens: MATCH")
        else:
            print(f"  Tokens: DIFF")

        def format_details(details):
            parts = []
            for d in details:
                mark = "✓" if d["hit"] else "✗"
                skip = " [1]" if d["single_char"] else ""
                parts.append(f"{mark}{d['token']}{skip}")
            return "  ".join(parts)

        print(f"  GT hits:   {format_details(gt_details)}  → {format_pct(gt_h, gt_t)}")
        print(f"  Rust hits: {format_details(rs_details)}  → {format_pct(rs_h, rs_t)}")

    # Summary
    print(f"\n{'─' * 60}")
    print(f"  SUMMARY ({lang.upper()})")
    print(f"  Phrases: {len(entries)}, with token diff: {phrases_with_diff}")
    print(f"")
    print(f"  Hit rate (all tokens):")
    print(f"    Ground truth: {format_pct(gt_hits_all, gt_total_all)}")
    print(f"    Rust binary:  {format_pct(rs_hits_all, rs_total_all)}")
    gt_rate = (gt_hits_all / gt_total_all * 100) if gt_total_all else 0
    rs_rate = (rs_hits_all / rs_total_all * 100) if rs_total_all else 0
    print(f"    Difference:   {abs(gt_rate - rs_rate):.1f}%")
    print(f"")
    print(f"  Hit rate (excluding single-char [1]):")
    print(f"    Ground truth: {format_pct(gt_hits_excl, gt_total_excl)}")
    print(f"    Rust binary:  {format_pct(rs_hits_excl, rs_total_excl)}")
    gt_rate_e = (gt_hits_excl / gt_total_excl * 100) if gt_total_excl else 0
    rs_rate_e = (rs_hits_excl / rs_total_excl * 100) if rs_total_excl else 0
    print(f"    Difference:   {abs(gt_rate_e - rs_rate_e):.1f}%")
    print(f"{'─' * 60}")

    return abs(gt_rate - rs_rate), abs(gt_rate_e - rs_rate_e)


def main():
    parser = argparse.ArgumentParser(description="Tokenizer benchmark comparison")
    parser.add_argument("--tokenizer", type=Path, default=TOKENIZER_DEFAULT,
                        help="Path to dank-lookup-tokenize binary")
    args = parser.parse_args()

    if not args.tokenizer.exists():
        print(f"Tokenizer binary not found: {args.tokenizer}")
        print(f"Build it first: cd tokenizer && cargo build --release")
        sys.exit(1)

    results = {}

    if GT_ZH.exists():
        results["zh"] = run_suite("zh", GT_ZH, args.tokenizer)
    else:
        print(f"Skipping ZH: ground truth not found at {GT_ZH}")
        print(f"Generate it: python3 tests/tokenizer-bench/gen-ground-truth.py")

    if GT_JA.exists():
        results["ja"] = run_suite("ja", GT_JA, args.tokenizer)
    else:
        print(f"Skipping JA: ground truth not found at {GT_JA}")

    # Decision gate
    print(f"\n{'=' * 60}")
    print(f"  DECISION GATE")
    print(f"{'=' * 60}")
    for lang, diffs in results.items():
        if diffs is None:
            continue
        diff_all, diff_excl = diffs
        if diff_all <= 5:
            verdict = "PASS — direct adoption"
        elif diff_all <= 15:
            verdict = "REVIEW — check specific diffs"
        else:
            verdict = "FAIL — consider alternatives"
        print(f"  {lang.upper()}: diff={diff_all:.1f}% (excl single={diff_excl:.1f}%) → {verdict}")
    print()


if __name__ == "__main__":
    main()
