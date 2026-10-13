# Security policy

## Supported versions

While dynamoxy is 0.x, only the latest minor release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security
advisories) on the repository, or by email to hello@carloscar.com.

## Security-relevant behaviour

- **Credentials** come from the AWS credential chain unless `Credentials` are passed; their
  secrets are hidden in `repr()` and never logged. Prefer IAM roles over static keys.
- **No payload logging.** Events, logs, spans and metrics contain operation names, table and
  index names, counts, durations, error codes and request ids, never item contents or key values.
  `Telemetry(record_expressions=True)` adds expressions with placeholders only, never values.
  `FieldError` keeps the offending value but leaves it out of `str()`.
- **No injection.** All values in expressions are expression attribute values and all attribute
  names are placeholders; PartiQL helpers take `?` parameters. Never format user input into
  PartiQL statements or `dynamoxy.condition()` text.
- **Cursors are not signed.** `page.cursor` is base64url JSON of the last evaluated key. It can
  be decoded and forged by clients. Never derive authorization from a cursor; the partition key of
  a query must come from trusted code. Encrypt or sign cursors yourself if key values are
  sensitive.
- **Unique constraints** rely on guard items that only dynamoxy maintains; other writers must not
  bypass them.
- **Endpoints**: `endpoint_url` must be `http://` or `https://`. Use `http://` only for local
  emulators; credentials are sent signed (SigV4), but item data travels in clear text over HTTP.
- **Dependencies**: the Python package has no required dependencies; the Rust core uses the
  AWS SDK for Rust and Tokio. Wheels are built in GitHub Actions with build provenance
  attestations.
