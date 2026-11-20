# ELT-Bench evaluation

Run a prepared roster from the taskgen environment. SWE-agent uses the original
ELT-Bench configuration and reads `ANTHROPIC_API_KEY` from the environment or
`ELT-taskgen/.env`.

```sh
.venv/bin/python tools/elt_bench/run_batch.py /path/to/evaluation --agent swe --run-first --canary task-01
```

Set `ELT_BENCH_DIR` and `ELT_SWE_PYTHON` if the checkout and worker environment
live outside the local layout. Spider uses `--agent spider` and `ELT_SPIDER_PYTHON`.

The Airbyte host needs the patched File connector image before running tasks
with flat-file sources. Build and import the image into the Airbyte node:

```sh
docker build -t ghcr.io/wlu03/elt-taskgen-source-file:0.6.0-ca-temporal1 runtime-images/source-file
docker save ghcr.io/wlu03/elt-taskgen-source-file:0.6.0-ca-temporal1 |
  docker exec -i airbyte-abctl-control-plane ctr --namespace k8s.io images import -
```
