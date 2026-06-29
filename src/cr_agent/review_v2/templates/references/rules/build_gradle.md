# Gradle Build Review Rule Overlay

- Check dependency configuration changes for runtime/test scope drift and transitive conflicts.
- Check task/profile changes do not skip tests, static checks, packaging, or generated-source steps in CI.
- Check repository, credential, and plugin changes for reproducibility and release safety.
