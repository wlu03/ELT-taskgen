# Airbyte connections

Create one Airbyte source for each source section in `config.yaml`, one
destination from the single destination section, and connections selecting
exactly the tables declared for that source. Use `full_refresh_append` and the
destination namespace.
