"""用合成图（已知 ground truth）量归属规则与折行合并的准确率。

测量方式刻意分成两个独立指标，避免混淆两种不同的错：
  1. 归属准确率：逐 OCR 文字块判定，看它落在哪个 ground truth 气泡里，side 对不对。
     这是 Gate 0 要求的 ≥95% 那个数。
  2. 消息切分：抽出来的消息条数是否等于 ground truth 条数（折行合并有没有过度/不足）。

用法:
  python synth.py --out out/synth          # 先生成合成图
  python selftest.py --synth out/synth     # 再量准确率
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import extract as ex  # noqa: E402


def load_cases(synth_dir: Path, limit: int | None = None) -> list[dict]:
    manifest = json.loads((synth_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest[:limit] if limit else manifest


def containing_bubble(cx: float, cy: float, bubbles: list[dict]) -> dict | None:
    for b in bubbles:
        if b["x0"] <= cx <= b["x1"] and b["y0"] <= cy <= b["y1"]:
            return b
    return None


def eval_case(case: dict, rules: list[str], langs: list[str]) -> dict:
    img = case["image"]
    bubbles = case["messages"]
    boxes = ex.ocr_boxes(img, langs)
    title_box = ex.guess_title_box(boxes, case["width"], case["height"])
    kept, dropped = ex.filter_noise(boxes, case["height"], title_box)
    lines = ex.group_lines(kept)

    per_rule: dict[str, dict] = {}
    for rule in rules:
        pairs = ex.assign_sides(lines, case["width"], rule, img if rule == "color" else None)
        ok = bad = orphan = 0
        mistakes: list[str] = []
        for ln, side in pairs:
            gt = containing_bubble(ln.cx, ln.cy, bubbles)
            if gt is None:
                orphan += 1
                continue
            if gt["side"] == side:
                ok += 1
            else:
                bad += 1
                mistakes.append(
                    f"      x{ln.cx:.0f},y{ln.cy:.0f} 判成{side} 实为{gt['side']}  {ln.text[:22]!r}"
                )
        total = ok + bad
        msgs = ex.merge_messages(pairs, rule)  # type: ignore[arg-type]
        per_rule[rule] = {
            "ok": ok,
            "bad": bad,
            "orphan": orphan,
            "acc": (ok / total) if total else 0.0,
            "n_msgs": len(msgs),
            "n_gt": len(bubbles),
            "split_ok": len(msgs) == len(bubbles),
            "mistakes": mistakes,
        }
    return {
        "name": case["name"],
        "n_boxes": len(boxes),
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "n_lines": len(lines),
        "rules": per_rule,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="合成图上量归属准确率")
    ap.add_argument("--synth", default="out/synth", help="synth.py 的输出目录")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rules", default="center,edge,color")
    ap.add_argument("--langs", default="ch_sim,en")
    ap.add_argument("--json", dest="json_out", help="把明细写成 JSON")
    args = ap.parse_args()

    synth_dir = Path(args.synth)
    cases = load_cases(synth_dir, args.limit or None)
    rules = [r.strip() for r in args.rules.split(",") if r.strip()]
    langs = [s.strip() for s in args.langs.split(",") if s.strip()]

    results = []
    for case in cases:
        r = eval_case(case, rules, langs)
        results.append(r)
        print(f"\n=== {r['name']}  OCR块 {r['n_boxes']} → 保留 {r['n_kept']} / 丢弃 {r['n_dropped']}")
        for rule in rules:
            d = r["rules"][rule]
            mark = "OK " if d["acc"] == 1.0 else "!! "
            print(
                f"  {mark}{rule:<6} 归属 {d['ok']}/{d['ok'] + d['bad']} = {d['acc'] * 100:5.1f}%"
                f"   孤立块 {d['orphan']:>2}   消息 {d['n_msgs']}/{d['n_gt']}"
                f" {'条数一致' if d['split_ok'] else '条数不符'}"
            )
            for m in d["mistakes"][:4]:
                print(m)

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    header = f"{'用例':<26}" + "".join(f"{r:>16}" for r in rules)
    print(header)
    print("-" * len(header))
    totals = {r: {"ok": 0, "bad": 0, "split": 0, "n": 0} for r in rules}
    for r in results:
        row = f"{r['name']:<26}"
        for rule in rules:
            d = r["rules"][rule]
            totals[rule]["ok"] += d["ok"]
            totals[rule]["bad"] += d["bad"]
            totals[rule]["split"] += 1 if d["split_ok"] else 0
            totals[rule]["n"] += 1
            row += f"{d['acc'] * 100:13.1f}%  "
        print(row)
    print("-" * len(header))
    row = f"{'总体归属准确率':<26}"
    for rule in rules:
        t = totals[rule]
        acc = t["ok"] / (t["ok"] + t["bad"]) if (t["ok"] + t["bad"]) else 0
        row += f"{acc * 100:13.1f}%  "
    print(row)
    row = f"{'条数一致用例':<26}"
    for rule in rules:
        t = totals[rule]
        row += f"{t['split']}/{t['n']}".rjust(14) + "  "
    print(row)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n明细已写 {args.json_out}")

    # Gate 0 判据
    print()
    for rule in rules:
        t = totals[rule]
        acc = t["ok"] / (t["ok"] + t["bad"]) if (t["ok"] + t["bad"]) else 0
        verdict = "通过" if acc >= 0.95 else "未达标"
        print(f"Gate 0（归属 ≥95%）规则 {rule:<6}: {acc * 100:.1f}% → {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
