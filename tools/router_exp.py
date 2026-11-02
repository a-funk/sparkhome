#!/usr/bin/env python3
"""The router classifier experiment (README roadmap 11): can a tiny stdlib classifier trained on the labelled eval
cases beat sparkhome/router.py's rules? Naive Bayes over word 1-2-grams and logistic regression over char 3-5-grams,
trained on the fixed train split (router.holdout), scored on the test split and on tests/eval/router_fresh.jsonl (50
more phrasings): held out from the classifiers, not from the rules, which were written with both in view (so their
numbers there are fitted; docs/research/EVAL.md). Gate to ship one: gate().
    python3 -I tools/router_exp.py [--selftest]
"""
import collections
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sparkhome import router  # noqa: E402
from sparkhome.agent import norm  # noqa: E402


def words(t):
    w = ["<s>"] + norm(t).split() + ["</s>"]
    return w[1:-1] + [f"{a} {b}" for a, b in zip(w, w[1:])]


def chars(t):
    t = f" {norm(t)} "
    return [t[i:i + n] for n in (3, 4, 5) for i in range(len(t) - n + 1)]


class NB:
    """Multinomial naive Bayes, add-one smoothing."""

    def fit(self, X, y):
        self.n = collections.Counter(y)
        self.f = {k: collections.Counter() for k in self.n}
        for x, k in zip(X, y):
            self.f[k].update(x)
        self.v = len({w for c in self.f.values() for w in c})
        self.tot = {k: sum(c.values()) for k, c in self.f.items()}
        return self

    def score(self, x):  # log-odds of "escalate"
        lp = {k: math.log(self.n[k] / sum(self.n.values())) + sum(
            math.log((self.f[k][w] + 1) / (self.tot[k] + self.v)) for w in x) for k in self.n}
        return lp[True] - lp[False]


class LR:
    """Binary logistic regression, plain SGD with L2, fixed seed."""

    def fit(self, X, y, epochs=40, lr=0.3, l2=1e-4):
        self.w, rnd, data = collections.defaultdict(float), random.Random(0), list(zip(X, y))
        for _ in range(epochs):
            rnd.shuffle(data)
            for x, k in data:
                g = (1 / (1 + math.exp(-max(-30, min(30, self.score(x))))) - k) * lr
                for f in set(x):
                    self.w[f] -= g + l2 * self.w[f]
                self.w["<b>"] -= g
        return self

    def score(self, x):
        return self.w["<b>"] + sum(self.w.get(f, 0) for f in set(x))


def evaluate(pred, cases):
    """-> (accuracy, escalate recall, local precision kept, TV commands sent up, ms per request)."""
    t0 = time.perf_counter()
    got = [pred(c["text"]) for c in cases]
    ms = (time.perf_counter() - t0) * 1000 / max(1, len(cases))
    want = [c["route"] == "escalate" for c in cases]
    acc = sum(g == w for g, w in zip(got, want)) / len(cases)
    esc = [g for g, w in zip(got, want) if w]
    tv = [c["id"] for c, g in zip(cases, got) if g and (c.get("cat") == "tv" or c["route"] == "fast")]
    return acc, sum(esc) / max(1, len(esc)), tv, ms


def gate(c, rules):
    """Whether a classifier's numbers c (test acc, fresh acc, TV commands sent up, ms) may replace the rules': at least
    their accuracy on both sets, under 50 ms, and no TV command (cat tv, or route fast) sent up. ponytail: the rules
    were fitted with both sets in view (100 % on test, so never "more than"), so this favours them; gate on the
    request log's real requests, a blind set, once there are a few hundred."""
    te, fr, tv, ms = c
    return te >= rules[0] and fr >= rules[1] and ms < 50 and not tv


def main():
    cases = [json.loads(x) for x in open(ROOT / "tests/eval/cases.jsonl") if x.strip()]
    fresh = [json.loads(x) for x in open(ROOT / "tests/eval/router_fresh.jsonl") if x.strip()]
    train, test = router.holdout(cases)
    y = [c["route"] == "escalate" for c in train]
    nb = NB().fit([words(c["text"]) for c in train], y)
    lr = LR().fit([chars(c["text"]) for c in train], y)
    preds = {"always local": lambda t: False,
             "rules (router.py)": lambda t: router.route(t)[0] == "escalate",
             "naive Bayes, words 1-2": lambda t: nb.score(words(t)) > 0,
             "logistic, chars 3-5": lambda t: lr.score(chars(t)) > 0}
    print(f"train {len(train)} ({sum(y)} escalate) · test {len(test)} ({sum(c['route'] == 'escalate' for c in test)}) "
          f"· fresh {len(fresh)} ({sum(c['route'] == 'escalate' for c in fresh)})")
    print(f"| {'':<24}| train acc | test acc | test esc. recall | fresh acc | fresh esc. recall | TV sent up "
          f"(test+fresh) | ms/request |")
    out = {}
    for name, p in preds.items():
        tr, te, fr = evaluate(p, train), evaluate(p, test), evaluate(p, fresh)
        out[name] = (te[0], fr[0], te[2] + fr[2], max(te[3], fr[3]))
        print(f"| {name:<24}| {tr[0]:9.1%} | {te[0]:8.1%} | {te[1]:16.1%} | {fr[0]:9.1%} | {fr[1]:17.1%} | "
              f"{len(te[2]) + len(fr[2]):>3} {','.join(te[2] + fr[2])[:40]:<40} | {max(te[3], fr[3]):10.3f} |")
    rules = out["rules (router.py)"]
    for name in ("naive Bayes, words 1-2", "logistic, chars 3-5"):
        te, fr, tv, ms = out[name]
        print(f"gate {name}: {'PASS' if gate(out[name], rules) else 'FAIL'} (held-out {te:.1%}/{fr:.1%} vs rules "
              f"{rules[0]:.1%}/{rules[1]:.1%}, fitted; {ms:.2f} ms, {len(tv)} TV commands sent up)")
    return out, len(test), len(fresh)


if __name__ == "__main__":
    out, n, m = main()
    if "--selftest" in sys.argv:  # the experiment runs, the rules hold their gate numbers, and a perfect router passes
        rules = out["rules (router.py)"]
        assert rules[2] == [] and rules[0] >= 0.9 and gate((1.0, 1.0, [], 0.0), rules), rules
        said = f"route {rules[0]:.0%} of the {n} test-split cases and {rules[1]:.0%} of {m} more".replace("%", " %")
        assert said in " ".join((ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8").split()), said  # (as README says)
        rows = (ROOT / "docs/research/EVAL.md").read_text(encoding="utf-8").splitlines()  # ...and EVAL.md's table, a
        pct = lambda x: f"| {x:.1%} |".replace("%", " %")  # row for each: its accuracies and TV commands sent up
        assert any(f"| test acc ({n}) |" in r for r in rows) and all(any(pct(te) in r and pct(fr) in r and
                                                                         f"| {len(tv)} |" in r for r in rows)
                                                                     for te, fr, tv, _ in out.values()), out
