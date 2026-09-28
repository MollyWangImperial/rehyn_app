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
New registrations queue an internal alert to `jw923@ic.ac.uk`; registrants are
never emailed by this notification feature.

## Browsing-time estimate

The `early-access-v2` notice discloses the internal email alert and timing.
`browsing_seconds` is an optional integer (0..86400), reported by the browser.
It measures visible time for the current landing-page visit starting at client
initialization, excludes hidden-tab/network-wait time, and is not proof of
attention. Both forms share one in-memory timer. No cookies, local storage,
user identifiers, click trails or cross-site tracking are used. Timing is sent
only with the form. Old v1 clients remain supported without timing; historical
records and duplicate submissions are not overwritten or retrospectively queued.

## Enable internal email notifications

In the Render **rehyn API service**, set these server-only environment variables:

- `RESEND_API_KEY`: a Resend sending key, preferably restricted to the sender domain.
- `EARLY_ACCESS_EMAIL_FROM`: a verified sender, e.g. `Rehyn <notifications@rehyn.com>`.

Do not put keys in the static site, source control, chat or browser bundle.
The recipient is fixed server-side to `jw923@ic.ac.uk`, not accepted from a request.
Use a verified Resend domain (https://resend.com/docs/dashboard/domains/introduction).
The existing HTTPX dependency sends via HTTPS, not SMTP. The provider's test domain
has recipient restrictions; use a verified sender for production.

Each Mongo signup atomically includes a `notification` outbox. An async worker
wakes on new signup and polls every 60 seconds while the API process runs. Missing
credentials leave records pending without failing signup. Cold starts/deploys
resume pending work. Free Render sleep can delay retries until the next wake-up;
this is not an always-on guaranteed delivery service.

Status is `pending`, `sending` (two-minute lease), `retry`, `accepted`, or
`needs_review`. A fixed payload and Resend idempotency key prevent duplicate mail
on concurrent retries. Transient failures get up to eight attempts with backoff.
Ambiguous sends older than 23 hours stop for review because Resend's idempotency
window is 24 hours (https://resend.com/docs/dashboard/emails/idempotency-keys).
Permanent provider errors also require review. No signup is lost when email fails.

Inspect `notification.status`, `error_code`, `attempts` and `provider_id` in Atlas.
`accepted` means accepted by Resend, not verified inbox delivery; check Resend's
delivery log and the destination inbox. No public status/list endpoint is exposed.
To retry a `needs_review` record, first check the provider for an existing message
and delivery outcome. Do not blindly reset ambiguous sends or historical records.
Removal requests must include notification payloads and internal email copies.

The static landing page calls the API without cookies or privileged keys. It
waits up to 90 seconds because the free API service can cold-start independently
of the landing page, which stays on static hosting.

Verification:

```sh
python -m pytest backend/tests/test_early_access.py backend/tests/test_early_access_notifications.py backend/tests/test_login_handoff_bson.py -q
```

For production verification, use a clearly synthetic `@example.com` email in
both landing-page forms, then confirm only one document with that address exists
in Atlas. Do not use a real patient's email or export the full list for testing.
After sender configuration, submit a unique synthetic address with v2 timing;
verify the alert in the target inbox and that a duplicate signup sends no second alert.
