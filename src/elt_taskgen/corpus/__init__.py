"""Corpus: difficulty measurement + anchor-matched selection.

Scores difficulty (structural + empirical, load and transform kept separate)
and selects a corpus whose distribution matches the measured ELT-Bench anchor,
with quotas and family/cluster-isolated train/val splits.
"""
