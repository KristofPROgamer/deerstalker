# Publication validation

Checked on 2026-10-13. These results apply to the publication copy, not every historical snapshot.

Existing offline integration suite: **6 passed**. It uses a deterministic model stub and tests scanning, extraction, reporting and local API readiness. The new environment-key adapter was also checked with dummy values. No real case data or live cloud/model request was used.

## Publication checks

Source files and retained history are scanned for credentials with Gitleaks and additional manual review of configuration, notebooks and private data. A specific upstream SHA-256 in Phone Studio is allowlisted as an integrity checksum, not a credential. This is a bounded review, not a guarantee that the projects are defect-free. Large runtime files, secrets and local account/session databases are excluded.
