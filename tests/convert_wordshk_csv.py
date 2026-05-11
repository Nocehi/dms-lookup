#!/usr/bin/env python3
"""Convert words.hk CSV to pyglossary tabfile TSV.

Filter: OK + 已公開 rows only.
Body: yue-first, eng-secondary, with pos/label prefix and jyutping line.

Usage:
    python3 convert_wordshk_csv.py /path/to/words.hk-export.csv /tmp/dank-lookup/wordshk-yue.tsv
"""

import csv
import re
import sys


def clean_body(body: str) -> str | None:
    body = body.strip()
    if not body or body.startswith("未有內容"):
        return None

    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"#(\S+)", r"\1", body)
    body = re.sub(r"-{3,}", "", body)

    pos_tags = re.findall(r"\(pos:([^)]+)\)", body)
    label_tags = re.findall(r"\(label:([^)]+)\)", body)

    body = re.sub(r"\(pos:[^)]+\)", "", body)
    body = re.sub(r"\(label:[^)]+\)", "", body)

    collected_yue = []
    collected_eng = []
    collected_eg: list[tuple[str, str]] = []

    in_eg = False
    in_expl = False

    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("<explanation>"):
            in_expl = True
            continue
        if line.startswith("<eg>"):
            in_eg = True
            continue

        if line.startswith("yue:"):
            text = line[4:].strip()
            if in_eg:
                collected_eg.append((text, ""))
            else:
                collected_yue.append(text)
        elif line.startswith("eng:"):
            text = line[4:].strip()
            if in_eg and collected_eg:
                prev_yue, _ = collected_eg[-1]
                collected_eg[-1] = (prev_yue, text)
            elif not in_eg:
                collected_eng.append(text)
        else:
            if not in_eg and collected_yue:
                collected_yue.append(line)
            elif not in_eg and not collected_yue:
                collected_yue.append(line)

    prefix = "".join(f"[{t}]" for t in pos_tags + label_tags)
    lines = []
    if prefix:
        lines.append(prefix)
    lines.append("")
    lines.extend(collected_yue)
    lines.append("")

    if collected_eg:
        lines.append("例：")
        for yue_ex, eng_ex in collected_eg:
            lines.append(yue_ex)
        lines.append("")

    if collected_eng:
        lines.append("英：" + "；".join(collected_eng))

    return "\n".join(lines).strip()


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input.csv> <output.tsv>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    count = 0
    with (
        open(input_path, encoding="utf-8") as f_in,
        open(output_path, "w", encoding="utf-8") as f_out,
    ):
        reader = csv.reader(f_in)
        for row in reader:
            if len(row) < 6:
                continue
            status = row[4].strip()
            public = row[5].strip()
            if status != "OK" or public != "已公開":
                continue

            full = row[1].strip()
            if ":" not in full:
                continue
            headword, jyutping = full.split(":", 1)

            body_raw = row[2]
            body_clean = clean_body(body_raw)
            if not body_clean:
                continue

            definition = f"[{jyutping}]\n{body_clean}"
            definition_escaped = definition.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
            f_out.write(f"{headword}\t{definition_escaped}\n")
            count += 1

    print(f"Written {count} entries to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
