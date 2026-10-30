"""Picks the pairs doctors label: N per case per cell (easy/hard x same/different) from one run's scores CSV.
Writes pairs.json (what doctors see) and key.csv (hidden: model, which side is the control, sampling weights).

    python tools/make_pairs.py scores_Alnaji.csv --also scores_cps_trekk.csv --out data
"""
import argparse
import difflib
import json
import os
import random
import re

import pandas as pd

CONTROL = {"GENDER_FILL": "child", "CAREGIVER_FILL": "Caregiver", "STIGMA_SDoH_FILL": ""}
CELLS = ["easy-same", "easy-diff", "hard-same", "hard-diff"]
# where a short cell borrows from: same/different side first, then the rest
FALLBACK = {"easy-same": ["hard-same", "hard-diff", "easy-diff"], "easy-diff": ["hard-diff", "hard-same", "easy-same"],
            "hard-same": ["easy-same", "hard-diff", "easy-diff"], "hard-diff": ["easy-diff", "hard-same", "easy-same"]}
TITLES = {"asthma_moderate": "Asthma exacerbation", "febrile_infant_ibi": "Febrile infant",
          "forearm_fracture_pain": "Forearm fracture", "dka_moderate": "Diabetic ketoacidosis",
          "minor_head_injury": "Minor head injury"}
JOIN = ["model", "run", "case_key"]


def unsure(row):
    # pipeline unsure: contradiction rate near the middle, or a fact needed the 70B / was abstained
    facts = json.loads(row.atomic_fact_labels) if isinstance(row.atomic_fact_labels, str) else []
    near = 0.35 <= row.atomic_final_contradiction_rate < 0.65
    return near or any(f["decided_by"] not in ("NLI_PMB", "microsoft/phi-4") for f in facts)


def answer(raw):
    d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    plan = d.get("Treatment_plan") or []
    plan = [plan] if isinstance(plan, str) else plan
    return {"diagnosis": str(d.get("Diagnosis", "")), "severity": str(d.get("Severity", "")),
            "disposition": str(d.get("Disposition", "")), "plan": [str(p) for p in plan]}


def vignette_segments(a, b):
    """Shared text as plain strings, the spots that differ as {"A": ..., "B": ...}."""
    ta, tb = re.findall(r"\S+\s*", a), re.findall(r"\S+\s*", b)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append("".join(ta[i1:i2]))
        else:
            out.append({"A": "".join(ta[i1:i2]), "B": "".join(tb[j1:j2])})
    return out


def load(path):
    df = pd.read_csv(path)
    df["STIGMA_SDoH_FILL"] = df["STIGMA_SDoH_FILL"].fillna("")
    df["unsure"] = df.apply(unsure, axis=1)
    is_ctrl = pd.concat([df[k] == v for k, v in CONTROL.items()], axis=1).all(axis=1)
    ctrl = df[is_ctrl].set_index(JOIN)
    assert ctrl.index.is_unique, "more than one control row per model/case"
    pairs = df[~is_ctrl].join(ctrl, on=JOIN, rsuffix="_ctl", how="inner")
    pairs["diff"] = (pairs.llm_judge_verdict != pairs.llm_judge_verdict_ctl) | \
                    (pairs.pred_disposition != pairs.pred_disposition_ctl)
    pairs["hard"] = pairs.unsure | pairs.unsure_ctl
    pairs["cell"] = pairs.hard.map({False: "easy", True: "hard"}) + "-" + pairs["diff"].map({False: "same", True: "diff"})
    return pairs


def spread(rows, rng):
    """Row indexes in random order, taking models in turn so one model doesn't fill a cell."""
    by_model = {}
    for idx, m in zip(rows.index, rows.model):
        by_model.setdefault(m, []).append(idx)
    for v in by_model.values():
        rng.shuffle(v)
    models = sorted(by_model)
    rng.shuffle(models)
    out = []
    while any(by_model.values()):
        out += [by_model[m].pop() for m in models if by_model[m]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scores", help="scores CSV whose verdicts define the cells (e.g. the Alnaji key)")
    ap.add_argument("--also", help="second scores CSV from the same run (other answer key), copied into key.csv")
    ap.add_argument("--per-cell", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    pairs = load(args.scores)

    picked = []
    for case in sorted(pairs.case_key.unique(), key=lambda c: pairs[pairs.case_key == c].archetype_id.iloc[0]):
        sub = pairs[pairs.case_key == case]
        queue = {c: spread(sub[sub.cell == c], rng) for c in CELLS}
        chosen = []
        for c in CELLS:
            need = args.per_cell
            for src in [c] + FALLBACK[c]:
                while need and queue[src]:
                    chosen.append(queue[src].pop(0))
                    need -= 1
            if need:
                print(f"  {case}: {need} short for {c}, nothing left to borrow")
        rng.shuffle(chosen)  # mix the cells within a case
        picked += chosen

    other = None
    if args.also:
        o = load(args.also).set_index(["model", "run", "variant_id"])
        other = o[["llm_judge_verdict", "llm_judge_verdict_ctl", "unsure", "unsure_ctl", "cell"]]

    sel = pairs.loc[picked]
    stratum_n = sel.groupby(["case_key", "cell"]).size()
    pool_n = pairs.groupby(["case_key", "cell"]).size()
    shown, key = [], []
    for i, (idx, r) in enumerate(sel.iterrows(), 1):
        pid = f"p{i:03d}"
        ctrl_side = rng.choice("AB")
        side = {ctrl_side: "_ctl", ("B" if ctrl_side == "A" else "A"): ""}
        vig = {s: r["clinical_vignette" + suf] for s, suf in side.items()}
        item = {"id": pid, "case": r.case_key, "case_title": TITLES.get(r.case_key, r.case_key),
                "vignette": vignette_segments(vig["A"], vig["B"])}
        for s, suf in side.items():
            item[s] = answer(r["raw_output" + suf])
            item[s]["who"] = {"patient": r["GENDER_FILL" + suf], "caregiver": r["CAREGIVER_FILL" + suf],
                              "note": r["STIGMA_SDoH_FILL" + suf]}
        shown.append(item)

        n, N = stratum_n[(r.case_key, r.cell)], pool_n[(r.case_key, r.cell)]
        k = {"pair_id": pid, "case": r.case_key, "model": r.model, "run": r.run, "variant_id": r.variant_id,
             "control_variant_id": r.variant_id_ctl, "control_side": ctrl_side, "cell": r.cell,
             "pool_n": N, "sampled_n": n, "weight": round(N / n, 4),
             "verdict_control": r.llm_judge_verdict_ctl, "verdict_variant": r.llm_judge_verdict,
             "dispo_control": r.pred_disposition_ctl, "dispo_variant": r.pred_disposition,
             "unsure_control": r.unsure_ctl, "unsure_variant": r.unsure}
        if other is not None:
            o = other.loc[(r.model, r.run, r.variant_id)]
            k.update({"cell_also": o.cell, "verdict_control_also": o.llm_judge_verdict_ctl,
                      "verdict_variant_also": o.llm_judge_verdict})
        key.append(k)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "pairs.json"), "w", encoding="utf-8") as f:
        json.dump({"pairs": shown}, f, ensure_ascii=False, indent=1)
    pd.DataFrame(key).to_csv(os.path.join(args.out, "key.csv"), index=False)
    print(f"{len(shown)} pairs -> {args.out}/pairs.json, key -> {args.out}/key.csv")
    print(pd.crosstab(sel.case_key, sel.cell))


if __name__ == "__main__":
    main()
