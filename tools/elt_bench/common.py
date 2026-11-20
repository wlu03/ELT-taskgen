"""Local configuration and audit helpers for ELT-Bench evaluation."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from elt_taskgen.runtime.process import CommandResult, ProcessFailure
from elt_taskgen.runtime.snowflake import _attempt_principal_names, _existing_owned, quote_identifier


REPO = Path(__file__).resolve().parents[2]
PROFILE = os.environ.get("ELT_EVAL_SNOWFLAKE_PROFILE", "kang_lab_keypair")


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def environment_variables():
    env = {}
    env_path = REPO / ".env"
    if not env_path.is_file():
        return dict(os.environ)
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            name, value = line.split("=", 1)
            items = shlex.split(value, comments=True)
            if len(items) == 1:
                env[name.strip()] = items[0]
    return {**env, **os.environ}


class SanitizedCommandRunner:
    def __init__(self, secrets_to_redact):
        self.secrets = secrets_to_redact

    def run(self, argv, *, cwd=None, env=None, stdin_path=None):
        if stdin_path is not None:
            raise ValueError("Evaluation Docker commands require no input stream")
        result = subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})},
                                capture_output=True, text=True, timeout=250)
        if result.returncode:
            output = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr + "\n" + result.stdout)
            for value in self.secrets:
                if value:
                    output = output.replace(value, "[redacted]")
            raise ProcessFailure(output.strip()[-5000:])
        return CommandResult(result.returncode, result.stdout, result.stderr)


def cleanup_snowflake(admin, database):
    owner, user, warehouse = _attempt_principal_names(database)
    with admin.cursor() as cur:
        owned = _existing_owned(cur, database)
        for role, what, name in (("SYSADMIN", "DATABASE", database), ("SYSADMIN", "WAREHOUSE", warehouse),
                                 ("SECURITYADMIN", "USER", user), ("SECURITYADMIN", "ROLE", owner)):
            if owned[what.lower()]:
                cur.execute(f"USE ROLE {role}")
                cur.execute(f"DROP {what} IF EXISTS {quote_identifier(name)}")
