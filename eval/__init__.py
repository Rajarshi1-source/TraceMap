"""The eval harness: a golden set, a runner, a scorer, and a merge gate.

This directory is the MLOps wrapper. The detector is the model, the twelve
cases are the held-out set, `eval_runs` is the experiment tracker, and
`check_regression.py` is the deployment gate. Nothing here is machine learning
and that is deliberate -- the claim being made is that the detector's accuracy
is MEASURED and REGRESSION-GATED, which is the part almost nobody does, and it
is true of an EWMA exactly as much as it would be of a transformer.

The honest framing: if a model beat the EWMA on this set, it would be worth
switching, and there would be a number to prove it.
"""
