# Local source-service certificate authority

`ca.key` and `server.key` are published deliberately. They sign and serve
nothing outside a generated source environment.

They exist because the Airbyte `source-file` connector discards the URL scheme,
always fetches `https://<host>`, and verifies against the certificate bundle
baked into its image, with no run-time option to add a certificate. A hermetic
flat-file server therefore needs a certificate from an authority the connector
image already trusts. The authority is fixed and shipped here, and the task
runner's connector image appends `ca.crt` to its bundle.

`server.crt` is issued for `elt-files`, `localhost` and `127.0.0.1`, the only
names the file service answers to. It is pre-generated rather than created per
run so the environment needs no certificate tooling.

Treat all of this as public test material. It must never sign a certificate for
a name outside a source environment, and nothing outside that environment
should trust it.
