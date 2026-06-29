# Kotlin Review Rule Overlay

- Check null-safety assumptions around platform types, Java interop, `!!`, and nullable collection elements.
- Check coroutine or async changes for missing cancellation, blocking calls on hot paths, and lost exceptions.
- Check data class and default-argument changes for binary/API compatibility when used across modules.
- Check changed collection transformations for empty, duplicate, and large-input behavior.
