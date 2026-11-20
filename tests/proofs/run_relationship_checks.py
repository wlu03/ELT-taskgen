"""Run offline integration proofs with ``python -m tests.proofs.run_relationship_checks``."""
import subprocess
import sys
from pathlib import Path


def main():
    repo = Path(__file__).resolve().parents[2]
    modules = (
        "prove_fan_out_no_dedup", "prove_branched_fact_rollup",
        "prove_relationship_pipeline", "prove_semantic_difficulty",
        "prove_graph_recipes", "prove_native_asof",
    )
    for index, module in enumerate(modules, 1):
        print(f"\nSTEP {index}: Run {module}", flush=True)
        subprocess.run([sys.executable, "-m", f"tests.proofs.{module}"], cwd=repo, check=True)
    print("\nSTEP 7: Check composite source proposals and final mart results", flush=True)
    subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_composite_proposals", "tests.test_composite_recipes",
         "tests.test_composition_search"],
        cwd=repo, check=True,
    )
    print("\nPASS: all relationship proofs completed without provider calls.")


if __name__ == "__main__":
    main()
