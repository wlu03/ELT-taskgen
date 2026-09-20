# Amazon Redshift destination

Configure the Airbyte Redshift destination with the injected cluster,
database, user, and flat S3 staging fields. The task name in
`redshift.config.schema` is the isolated schema inside that database. Use it as
the destination namespace; Airbyte writes final stream tables directly there.

With the pinned `airbytehq/airbyte` Terraform provider `0.6.5`, translate the
flat task fields into the provider's discriminated upload-strategy shape:

```hcl
configuration = {
  # database, host, password, port, schema, and username omitted here
  uploading_method = {
    awss3_staging = {
      access_key_id      = local.config.redshift.config.access_key_id
      secret_access_key  = local.config.redshift.config.secret_access_key
      s3_bucket_name     = local.config.redshift.config.s3_bucket_name
      s3_bucket_path     = "elt-bench/${local.config.redshift.config.schema}"
      s3_bucket_region   = local.config.redshift.config.s3_bucket_region
      purge_staging_data = true
    }
  }
}
```

Do not put `method = "S3 Staging"` in the nested provider object. That string
is part of the connector API payload, not the provider `0.6.5` HCL schema.
