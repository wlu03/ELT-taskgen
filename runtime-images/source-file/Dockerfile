FROM ghcr.io/wlu03/elt-taskgen-source-file:0.6.0-ca
COPY temporal.py /airbyte/integration_code/source_file/temporal.py
RUN python - <<'PY'
from pathlib import Path

path = Path("/airbyte/integration_code/source_file/source.py")
source = path.read_text()
original = "record = AirbyteRecordMessage(stream=name, data=row,"
assert source.count(original) == 1, "Unexpected source-file record emitter"
source = source.replace(
    original, "record = AirbyteRecordMessage(stream=name, data=normalize_record(row),"
)
path.write_text("from source_file.temporal import normalize_record\n" + source)
PY
