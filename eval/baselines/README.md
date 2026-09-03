# Committed baselines

One file per tier. `check_regression.py` picks `main-<tier>.json` from the
run's own tier, because smoke and full scores are computed over different case
mixes and comparing them is a category error rather than a lenient check.

| File | Tier | Recorded by |
|---|---|---|
| `main-smoke.json` | 4 cases | PR gate; recorded from a green smoke run on `main` |
| `main-full.json` | 12 cases | nightly gate; recorded from a green nightly run on `main` |

## Why these are committed and not computed

A gate whose threshold lives outside version control is not a gate. Committing
the baseline means a change to what "acceptable" means arrives as a diff with a
commit message, gets reviewed like anything else, and can be pointed at
afterwards when someone asks why the number moved.

## Why nothing updates them automatically

The nightly run reports drift; it does **not** rewrite these files. A baseline
that updates itself can only ever ratchet in whichever direction the detector
happens to be drifting, so a slow degradation becomes the new normal and the
gate stops being a gate. That is the same boiling-frog failure M6 guards
against in the detector, one level up — and it is easier to introduce here,
because "keep the baseline current" sounds like hygiene.

## Recording one

Only ever from a green run on `main`, and only with the run attached:

```bash
make eval          # or eval-smoke
make eval-baseline # writes main-<tier>.json from eval-run.json
```

Commit it with a message that says **why** it moved. "Detector alpha 0.3 → 0.2,
F1 0.87 → 0.91" is a reason. "Update baseline" is not — it is indistinguishable
from having tuned the threshold to clear the gate, which is M8.

## A missing file is a hard failure, deliberately

`check_regression.py` exits 2 when the tier's baseline is absent, rather than
passing. The cheapest way to turn a red gate green is to stop measuring, and
"no baseline yet" is exactly what that looks like from the outside.

So the first run of a new tier goes red with a message telling you to record
one. That is the correct behaviour and not a bug to work around.
