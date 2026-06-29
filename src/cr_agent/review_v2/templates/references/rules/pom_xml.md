# Maven POM Review Rule Overlay

- Check dependency scope/version changes for runtime compatibility and accidental test-only/runtime-only leakage.
- Check plugin/profile changes actually run in CI and do not silently skip tests, packaging, or enforcement.
- Check parent/BOM changes for transitive dependency shifts that can break consumers.
- Treat snapshot or local-only coordinates as release risks unless explicitly intended for a temporary validation run.
