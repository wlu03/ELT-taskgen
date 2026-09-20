# Airbyte Terraform provider

Use the Airbyte server, username, password, and workspace ID injected into
`config.yaml` by the benchmark installer. Define the provider and connector
resources in `elt/main.tf`; do not hard-code credentials in submitted files.
The task config carries the connector definition IDs used by the original
ELT-Bench Terraform provider contract.
