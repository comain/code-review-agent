# TypeScript And JavaScript Review Rule Overlay

- Check changed async code for missing `await`, swallowed promise rejection, duplicate submission, and missing timeout/cancellation.
- Check API/client changes for request/response compatibility, nullable fields, and backward compatibility.
- Check React/UI changes for stale closures, incorrect effect dependencies, uncontrolled layout shifts, and expensive render work.
- Check changed validation and serialization code for type coercion, timezone, number precision, and empty-value edge cases.
