# YAML Review Rule Overlay

- Check CI/deploy workflow changes for skipped gates, wrong branch filters, missing secrets, and unsafe default values.
- Check config changes for environment-specific drift, rollout safety, and missing backward-compatible defaults.
- Check indentation and list/map shape where it changes runtime semantics.
