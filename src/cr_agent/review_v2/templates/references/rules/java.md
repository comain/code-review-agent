# Java Review Rule Overlay

- Check nullability at changed call boundaries, especially values returned from remote calls, JSON parsing, caches, maps, and optional configuration.
- Check changed exception handling does not hide downstream failures or convert them into misleading success/default values.
- Check switch/branch changes for missing fall-through guards, inverted conditions, and incomplete enum/status handling.
- Check thread-safety only when changed code touches shared mutable state, caches, async callbacks, scheduled jobs, or static fields.
- Check performance risks from unbounded loops, repeated remote calls, repeated JSON parsing, or database/query calls in loops.
- For Dubbo/RPC/provider API changes, verify request/response compatibility, nullable fields, versioning, and consumer impact.
