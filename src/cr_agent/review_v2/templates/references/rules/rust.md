# Rust Review Rule Overlay

- Check changed `unsafe` blocks, lifetime assumptions, panic paths, and error conversion.
- Check concurrency changes for lock ordering, blocking in async contexts, and channel/task leaks.
- Check serialization/deserialization changes for backward compatibility and unknown-field behavior.
