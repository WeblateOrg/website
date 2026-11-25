# Hosted service synchronization

Send `POST /api/hosted/` with a form field named `payload`. Produce the
field with Django signing's `dumps(snapshot, key=PAYMENT_SECRET,
salt="weblate.hosted")`. Sign each delivery no more than five minutes
before sending it.

All fields shown here are required.

```json
{
  "billing": 42,
  "payment_ids": [],
  "workspace_id": "a8171a76-f3fa-48aa-8082-89e010185d82",
  "workspace_name": "Example Workspace",
  "customer_name": "Example Customer",
  "plan": "10k",
  "administrators": ["123"],
  "state": "active",
  "payment_mode": "local",
  "paid": true,
  "version": "2026.10.dev0",
  "snapshot_at": "2026-10-01T12:00:00.000001Z",
  "usage": {
    "projects": 1,
    "components": 2,
    "languages": 5,
    "source_strings": 100,
    "hosted_strings": 500,
    "hosted_words": 1000
  }
}
```

- Use the Hosted billing primary key and its workspace UUID and name. Send
  the deduplicated shared payment UUIDs from `Billing.payment.all`,
  `Billing.payment.recurring`, and `Invoice.payment.pk` in `payment_ids`.
  Use an empty list when there are no known references. The
  workspace name must be nonblank and has a 100-character limit. The reported customer name may be
  empty and has a 200-character limit.
- Send the applied Hosted plan slug (1–100 ASCII letters, digits, hyphens, or underscores), billing state
  (`active`, `trial`, or `terminated`), and payment mode (`local` or `external`).
  Use `Billing.check_payment_status()` for `paid`, including Hosted's normal
  grace period. Send the running Weblate version (up to 32 characters).
- Send the full administrator set from `get_billing_owners()`, using the same
  decimal user IDs as Hosted OIDC and the user sync API. Use an empty list to
  remove all Hosted grants. Pending IDs resolve when a matching active website
  identity becomes available.
- Use the existing Hosted Billing usage counters and count components belonging
  to the workspace's projects. Use JSON integers from 0 to 2,147,483,647. The
  billing ID uses the same range starting at 1.
- Use UTC RFC 3339 (`Z` or `+00:00`) with up to six fractional digits. Assign
  the timestamp when capturing the snapshot. Use a strictly newer timestamp
  for each new snapshot of a billing and retain it when re-signing retries.
  Keep the Hosted clock synchronized. The endpoint rejects timestamps more
  than five minutes ahead of the website clock. Usage reports use this
  measurement timestamp, and CRM shows website acceptance time separately.

A successful request returns HTTP 200.

```json
{"service_id": 1234, "customer_id": 5678, "snapshot_at": "2026-10-01T12:00:00.000001+00:00"}
```

The timestamp is the latest stored snapshot. The customer ID always identifies
the customer currently linked to the website service. A valid request with an equal or
older timestamp receives the same acknowledgement and leaves stored data
unchanged. Invalid signatures or payloads return HTTP 400.

## Customer association and rollout

On first registration, the website combines signed payment references with
processed shared payments tagged with the billing ID in `Payment.extra["billing"]`
and their processed recurring-payment relationships. This includes initial
purchases whose payment was created before the billing ID existed when Hosted
supplies the payment UUID or a later tagged renewal still references it.

The website reuses an unambiguous payer customer and, when a current or past
payment identifies one matching shared-hosting subscription, its existing
service. It creates a dedicated customer only when no historical evidence is
available. The dedicated customer uses `HOSTED_ORIGIN`
(`https://hosted.weblate.org/create/billing/`) and `user_id=-1`. Existing
service/customer associations and customer origins are preserved.

First registration uses `payment_ids` to establish the association. For a
newer snapshot on an already linked service, the website checks only the
explicitly referenced processed payments against the linked customer. Missing
and unprocessed references do not block operational updates at this stage.
Routine snapshots may send an empty list after association. Replays and older
snapshots skip payment checks.

The API returns HTTP 409 for association conflicts and leaves the
snapshot unapplied. The response includes `billing`, `error`, and relevant
payment, service, or customer identifiers.

| Error | Operator action |
| --- | --- |
| `payment_customer_conflict` | A linked billing references processed payments belonging to another customer. Check the authoritative `customer_id` against `payment_ids` and `payer_ids` in the response. |
| `payment_unavailable` | Retry when all referenced payments exist and have been processed. Check missing UUIDs against Hosted history. |
| `payment_billing_conflict` | Check tagged payment metadata and the original and repeated payment links against Hosted history. |
| `multiple_payers` | Review the payer IDs and establish the correct association explicitly. |
| `service_conflict` | Review the linked website subscriptions and verify the intended service and customer. |

For an established billing, the producer must report `payment_customer_conflict`
and promptly resend the complete operational snapshot with `payment_ids: []`.
Keep the same `snapshot_at` and sign the retry again. This lets administrator
removals and other operational changes take effect while the payment conflict
is reviewed. First registration keeps its existing historical payment checks.

Deploy the schema and API before enabling the Hosted Weblate snapshot producer (step 03b). The endpoint
expects the new payload immediately after deployment. Check deployed jobs for
users of the previous contract. After enabling the producer, verify the
customer associations and monitor its conflict reporting. Where historical
references were lost, recover explicit payment references before the first
snapshot if preserving the existing payer is required.

The producer must deliver administrator removals using complete snapshots and
synchronize terminated billings. Future reconciliation must account for billing
records deleted from Hosted so their grants can be revoked. A record that
disappears produces no snapshot by itself.

Newly reported Hosted administrators become website customer owners. When the
last linked workspace removes an administrator, the website removes their
ownership, including an owner entry previously added on the website.
Unrelated owners and administrator IDs unchanged between snapshots are left
alone. Hosted user sync or login can apply a pending grant as soon as the
identity is known. Each newer accepted snapshot also retries pending grants.
Users manage access in Hosted workspace settings.
Customer owners can download invoices and DPAs on the website. Self-service
plan and renewal controls remain on Hosted during this transition. CRM staff
can issue renewal invoices under the conditions below.

## Hosted checkout integration (step 03b)

- Persist the returned `customer_id` on the corresponding Hosted billing.
  Use it in `BillingForm.create_payment()` to load the existing customer from
  the shared payments database. Establish the billing and its authoritative
  association before creating a new payment. When the ID is uncached, resolve
  it synchronously through the signed snapshot API. Stop checkout if
  resolution fails. Keep ordinary operational snapshots and reconciliation
  asynchronous.
- Authorize payment initiation and return handling against the specific
  Hosted billing and verify that the payment belongs to it and its linked
  customer. Update the current `Customer.user_id`-based checks for this flow
  because synchronized customers may have `user_id=-1`. Keep
  `Payment.extra["billing"]` so the website payment job skips these payments.
- Keep Hosted’s existing origin-based payment processing. Check historical
  customer origins before enabling the checkout path. If an authoritative
  customer’s origin differs from Hosted’s selector, flag the billing for
  explicit rollout handling and stop checkout for it until resolved.
- Review website renewals already initiated for Hosted-linked services before
  cutover. The website skips new recurring charges, expiry notifications, and
  CRM renewal follow-ups for those services while Hosted manages billing.
- Finish or explicitly grandfather already initiated legacy payments before
  switching checkout. Keep the original payer checks confined to those
  payments. Review any legacy payment charged to a different customer before
  including its UUID in later snapshots.

During the transition, CRM staff may issue renewal quotes or final invoices for
existing monthly or yearly shared-hosting subscriptions with an existing EUR
payment and a Hosted-origin customer. This manual exception does not enable
website automatic renewals, expiry notifications, or CRM renewal follow-ups.
Staff must verify the plan, amount, and dates against Hosted before issuance.
If they differ, use Hosted checkout.

Keep the initial payment fixed to the invoice total. Before using this flow,
deploy the Hosted scheduler change that creates future plan-priced recurring
payments with `amount_fixed=False`, so Hosted adds applicable VAT to the net
price. Hosted settles these payments. One-time methods follow Hosted's existing
recurrence behavior, including clearing a previous recurring payment when
applicable. Hosted upgrades stay on Hosted until CRM package mapping is available.

Historical subscription reconstruction and entitlement publishing remain
separate rollout steps.

## Hosted plan and package mapping

Run `./manage.py sync_packages` before historical subscription reconstruction.
It provisions hidden shared packages for Basic, Medium, Advanced, Advanced 20k
and Enterprise at their Hosted prices. Advanced 20k has 20,000 source strings
and 30 languages.

Use `hosted_plan_to_package(slug, period)` with the plan slug from the
historical purchase or invoice and its period (`m` or `y`). Supported string
tiers are `10k`, `40k`, `160k`, `640k`, `2-5m` and `10m`; legacy slugs `basic`, `medium`, `advanced`, `advanced-20000` and `enterprise` also map to the corresponding `hosted:*` packages. The `2-5m`
slug maps to `hosted:2.5m`. Monthly names end in `-m`. The snapshot example
uses `10k` as the Hosted slug.

Non-profit slugs resolve to their regular packages. Use
`hosted_plan_requires_nonprofit_discount` to identify those purchases.
Reconstruction must check for existing discounts and mixed discounted and
undiscounted services before applying a customer-wide discount. Some Hosted
prices round down from exactly half the regular price. Flag differences for
review and preserve recorded payment amounts.

For future entitlement synchronization, `package_to_hosted_plan(name)`
returns the canonical regular Hosted slug. Use the subscription period and
customer discount separately.

Libre (`libre`, `libre-2-5m`) and internal unlimited
(`weblate-unlimited`) have no paid package. The retired
`enterprise-collabora` and `enterprise-for-non-profit-200-languages` plans,
other custom or unknown slugs, and website packages without a confirmed
Hosted equivalent require explicit handling if encountered during migration.

Hidden packages are unavailable for new website purchases and as upgrade
targets. They remain usable for historical reconstruction and existing
subscriptions. Website upgrade suggestions for legacy Hosted packages use
the next tier by price and preserve the billing period. Basic moves to
10k, Medium to 40k, Advanced and Advanced 20k to 640k, and Enterprise to 10M.
Usage-aware suggestions are a follow-up.
