# Mapper/DAO XML Review Rule Overlay

- Check SQL parameter names match mapper method arguments and DTO fields.
- Check dynamic SQL branches for missing `where`, accidental full-table update/delete, and invalid empty-list `IN` clauses.
- Check joins and nested selects for N+1 behavior, unbounded scans, and missing stable ordering with pagination.
- Check inserted/updated fields for unit, enum, status, and nullable-column compatibility.
