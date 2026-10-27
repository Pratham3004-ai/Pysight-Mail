# Expected demo behavior

- `normal.json`: all six emails are allowed and return one of positive, negative, neutral, or mixed.
- `pii.json`: both are allowed; email, phone, and account values are redacted before model input.
- `injection.json`: all three are blocked before model input.
- `abuse.json`: the oversized email is rejected; repeated requests eventually return HTTP 429.

The phrase screen covers these examples only. It is not a complete jailbreak detector.
