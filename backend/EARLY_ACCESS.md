# Early-access signups

`POST /api/early-access` stores landing-page registrations in the existing
MongoDB database's `early_access_signups` collection. This is separate from
patient accounts and does not create an account or subscribe to general marketing.

Each document stores the normalized email, UTC `created_at`, `source`,
`form_location` (`hero` or `footer`), `notice_version`, `notice_text`, and
`status: subscribed`. The email's SHA-256 is the unique `_id`, so case/whitespace
variants and retried submissions cannot create another document. The address
itself is retained for contacting the registrant; the hash is not anonymization.

Writes and read-back use majority concern. There is no file/device fallback,
and failed or unconfirmed writes return HTTP 503. Duplicate submissions return
the same generic confirmation and do not disclose registration status.

Access records through the existing authenticated MongoDB Atlas Data Explorer:
open the application database, then `early_access_signups`, sorted by
`created_at` descending. There is no public list, export, or email lookup endpoint.
Restrict access to staff responsible for early-access invitations. Handle removal
requests sent to `info@rehyn.com` through that authenticated account.

The endpoint limits bodies to 2 KiB, validates the email and form fields, checks
the landing-page Origin, uses a honeypot, and applies a bounded 20-attempt/hour
per-client process-local limiter. Origin checks are not authentication and the
limiter resets on process restart. A distributed limiter/challenge should replace
this basic protection if traffic or abuse grows. No email ownership verification
or invitation sending is implemented here; registration only stores interest.

The static landing page calls the API without cookies or privileged keys. It
waits up to 90 seconds because the free API service can cold-start independently
of the landing page, which stays on static hosting.

Verification:

```sh
python -m pytest backend/tests/test_early_access.py backend/tests/test_login_handoff_bson.py -q
```

For production verification, use a clearly synthetic `@example.com` email in
both landing-page forms, then confirm only one document with that address exists
in Atlas. Do not use a real patient's email or export the full list for testing.
