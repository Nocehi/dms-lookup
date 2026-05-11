#!/usr/bin/env python3
"""Dictionary coverage probe for dank-lookup.

Tests how well each dictionary covers various word categories.
Not a pass/fail test — produces a coverage report for human review.

Usage:
    python3 tests/coverage_probe.py              # full report
    python3 tests/coverage_probe.py --suite han   # single suite
    python3 tests/coverage_probe.py --json        # machine-readable output
"""
import json, subprocess, os, sys, pathlib, argparse
from collections import defaultdict

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "lib/backends/sdcv.sh"
TMP = pathlib.Path("/tmp/dank-lookup")

# ── Test suites ──────────────────────────────────────────────

SUITES = {
    "han-single": {
        "label": "萌典：單字",
        "primary": "han",
        "terms": list("觸控板組內終端套件介面除錯貼字典詞"),
    },
    "han-compound": {
        "label": "萌典：複合詞",
        "primary": "han",
        "terms": [
            "字典", "觸控", "終端機", "套裝", "介紹", "除去",
            "貼紙", "新增", "刪除", "修改", "手勢", "設定",
            "瀏覽器", "桌面", "視窗", "應用程式", "輸入法",
            "查詢", "結果", "模式", "複製", "儲存", "開啟",
            "關閉", "安裝", "更新", "系統", "權限", "檔案",
            "目錄", "搜尋", "測試", "版本", "文件", "功能",
        ],
    },
    "han-tech-tw": {
        "label": "萌典：台灣科技用語",
        "primary": "han",
        "terms": [
            "觸控板", "觸控螢幕", "瀏覽器", "伺服器", "資料庫",
            "作業系統", "應用程式", "使用者介面", "原始碼", "編譯器",
            "除錯器", "終端機", "程式碼", "網路",
        ],
    },
    "han-tech-cn": {
        "label": "萌典：大陸簡體科技用語",
        "primary": "han",
        "terms": [
            "触摸板", "触摸屏", "浏览器", "服务器", "数据库",
            "操作系统", "应用程序", "用户界面", "源代码", "编译器",
        ],
    },
    "han-compounds-miss": {
        "label": "萌典：已知漏洞（語法組合 / UI 操作詞）",
        "primary": "han",
        "regression": True,   # expected-zero suite, excluded from total
        "terms": [
            "組內", "貼上", "新增", "觸控板", "觸控螢幕",
            "除錯", "終端", "套件", "介面",
        ],
    },
    "yue": {
        "label": "粵語 (words.hk)",
        "primary": "yue",
        "terms": [
            "你好", "唔好", "食飯", "飲水", "瞓覺",
            "返工", "放工", "搭車", "行路", "買嘢",
            "靚", "幾多", "點解", "係咪", "咁樣",
            "得閒", "冇", "嘅", "啲", "攰",
            "手機", "電腦", "巴士", "的士", "港鐵",
            "屋企", "學校", "醫院", "超市", "餐廳",
        ],
    },
    "ja-kanji": {
        "label": "日語：漢字詞",
        "primary": "ja",
        "terms": [
            "辞書", "文字", "漢字", "学校", "先生", "学生",
            "猫", "犬", "花", "山", "川", "東京", "大阪",
        ],
    },
    "ja-katakana": {
        "label": "日語：片假名外來語",
        "primary": "ja",
        "terms": [
            "コンピュータ", "ブラウザ", "デスクトップ", "サーバー",
            "ネットワーク", "データベース", "プログラム", "アルゴリズム",
            "ジェスチャー", "タッチパッド", "キーボード", "マウス",
        ],
    },
    "ja-verb": {
        "label": "日語：動詞辭書形",
        "primary": "ja",
        "terms": [
            "読む", "書く", "食べる", "飲む", "走る", "見る",
            "言う", "聞く", "話す", "思う", "知る", "行く",
            "来る", "する", "出来る", "分かる",
        ],
    },
    "ja-hiragana": {
        "label": "日語：平假名詞彙",
        "primary": "ja",
        "terms": [
            "ありがとう", "おはよう", "さようなら", "すみません",
            "これ", "それ", "あれ", "どれ",
        ],
    },
    "en": {
        "label": "英英 (GCIDE + WordNet)",
        "primary": "en-en",
        "terms": [
            "hello", "dictionary", "computer", "network", "server",
            "database", "compile", "kernel", "daemon", "repository",
            "debug", "algorithm", "variable", "function", "interface",
            "touchpad", "gesture", "browser", "desktop", "window",
            "Linux", "Python", "JavaScript", "Bluetooth", "WiFi",
        ],
    },
    "mixed-script": {
        "label": "混合文字查詢",
        "primary": "han",
        "terms": [
            "USB接口", "WiFi密碼", "Python腳本", "Linux系統",
            "CPU使用率", "API介面", "SDK工具", "HTML標籤",
        ],
    },
}

# Suites marked regression=True are excluded from the total count.
REGRESSION_SUITES = {k for k, v in SUITES.items() if v.get("regression")}

# ── Runner ───────────────────────────────────────────────────

def query_backend(term):
    out = TMP / ("probe-%d.json" % abs(hash(term)))
    env = os.environ.copy()
    env["DANK_LOOKUP_OUTPUT"] = str(out)
    subprocess.run(
        ["bash", str(BACKEND), term],
        capture_output=True, env=env, timeout=10,
    )
    try:
        with open(out) as f:
            return json.load(f)
    except Exception:
        return {}


def run_suite(name, suite):
    label = suite["label"]
    primary = suite["primary"]
    terms = suite["terms"]
    results = []

    for t in terms:
        data = query_backend(t)
        modes = data.get("available_modes", [])
        hit = primary in modes
        other = [m for m in modes if m != primary]
        results.append({"term": t, "hit": hit, "modes": modes, "other": other})

    hits = sum(1 for r in results if r["hit"])
    return {
        "name": name,
        "label": label,
        "primary": primary,
        "total": len(terms),
        "hits": hits,
        "pct": round(100 * hits / len(terms)) if terms else 0,
        "results": results,
    }


def print_suite(s):
    print("\n── %s ── %d/%d (%d%%)" % (s["label"], s["hits"], s["total"], s["pct"]))
    for r in s["results"]:
        marker = "✓" if r["hit"] else "✗"
        extra = "  (also: %s)" % ", ".join(r["other"]) if r["other"] else ""
        print("  %s %s%s" % (marker, r["term"], extra))


def print_summary(all_results):
    coverage = [s for s in all_results if s["name"] not in REGRESSION_SUITES]
    regression = [s for s in all_results if s["name"] in REGRESSION_SUITES]

    print("\n" + "=" * 50)
    print("Coverage")
    print("=" * 50)
    print("%-32s %6s %6s" % ("Suite", "Rate", "Miss"))
    print("-" * 50)
    for s in coverage:
        misses = [r["term"] for r in s["results"] if not r["hit"]]
        miss_str = ", ".join(misses[:5])
        if len(misses) > 5:
            miss_str += " ..."
        print("%-32s %3d/%-3d %s" % (s["label"], s["hits"], s["total"], miss_str))
    print("-" * 50)
    total_h = sum(s["hits"] for s in coverage)
    total_t = sum(s["total"] for s in coverage)
    print("%-32s %3d/%-3d (%d%%)" % ("TOTAL", total_h, total_t,
          round(100 * total_h / total_t) if total_t else 0))

    if regression:
        print("\n── Regression (expected-miss, not in total) ──")
        for s in regression:
            misses = [r["term"] for r in s["results"] if not r["hit"]]
            print("%-32s %3d/%-3d" % (s["label"], s["hits"], s["total"]))


def main():
    parser = argparse.ArgumentParser(description="Dictionary coverage probe")
    parser.add_argument("--suite", nargs="*", help="Suite names to run (default: all)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--list", action="store_true", help="List available suites")
    args = parser.parse_args()

    if args.list:
        for name, s in SUITES.items():
            print("%-20s %s (%d terms)" % (name, s["label"], len(s["terms"])))
        return

    TMP.mkdir(parents=True, exist_ok=True)

    names = args.suite if args.suite else list(SUITES.keys())
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        print("Unknown suites: %s" % ", ".join(unknown), file=sys.stderr)
        print("Available: %s" % ", ".join(SUITES.keys()), file=sys.stderr)
        sys.exit(1)

    all_results = []
    for name in names:
        s = run_suite(name, SUITES[name])
        all_results.append(s)
        if not args.json:
            print_suite(s)

    if args.json:
        json.dump(all_results, sys.stdout, indent=2, ensure_ascii=False)
    else:
        print_summary(all_results)


if __name__ == "__main__":
    main()
