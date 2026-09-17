# Trigger and monitor syncs

After `terraform apply`, retrieve each Airbyte connection ID, trigger a sync,
and wait until every job succeeds. `check_job_status.py` can monitor the
connections recorded in `elt/terraform.tfstate`.
