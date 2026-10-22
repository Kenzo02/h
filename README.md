# Kenzo02 Kurigram fork

This fork of [Kurigram](https://github.com/kurigram-org/kurigram) is used by UserBot
and related Telegram services. The Python import remains `pyrogram`.

Python **3.10 or newer** is required. Candidate package version `2.2.26.post8` integrates
maintained upstream `main` through Telegram Layer 232 with this fork's
compatibility and runtime fixes. The package version alone does not prove that
an artifact has been published or deployed.

## Changes from 2.2.26.post7

- Integrate the latest reviewed upstream Layer 231/232 schema, `Message.sender`,
  named storage sentinel and documentation updates. The sentinel still aliases
  `object`; storage `None` writes and the fork's legacy adapters are preserved.
- Preserve startup-error classification/cleanup chaining, pending-RPC cancellation
  cleanup, DC endpoint selection/fallback, dispatcher admission, and publication
  and no-replay controls.
- New gift deletion and wallet/theme wire APIs require explicit application calls;
  integration does not invoke them or enable financial actions.
- Raw `payments.GetStarGiftWithdrawalUrl` now takes `tmp_password` instead of the
  former SRP `password` object. Raw callers must migrate to the new protocol.
  Existing forwarding callers do not need a new feature flag.

The candidate is prepared on a separate integration branch. This does not indicate
publication, production installation or activation. The last delivered production
wheel remains a separate rollout.

## Changes from 2.2.26.post4

- Retain ACK obligations over reconnects, normalize proxy assignments, and preserve
  embedded business replies even when additional reply fetching is disabled.
- Preserve fork-owned shutdown/cancellation, publication receipts and no-replay
  protection, DC failover/cache, storage adapters, and preuploaded album handling.
- Use upstream dispatcher admission: packets received before dispatcher startup,
  after shutdown, or with `no_updates=True` are not queued. Processing begins when
  dispatcher startup opens admission. This intentionally retires the fork's old
  pre-initialize backlog preservation; no extra buffering or startup mechanism.
- Add inline game scores without changing the fork's existing positional/named
  arguments or edit-only return selection. The new `inline_message_id` is additive.
- Add Layer 230 TON address entities, wallet methods and service message types.
  No wallet, transfer, backup or dApp connection is enabled automatically.
- Layer 230 changes verification descriptions to formatted text. High-level
  `BotVerification.description` now exposes `FormattedText`; use `str(value)` for
  plain text and `.text`/`.entities` for structured content. Raw verification
  requests require `TextWithEntities`, not a string. The removed raw
  `InputPasskeyCredentialFirebasePNV` cannot be kept as a working wire API.
- Assigning `client.proxy` now validates immediately, just like construction;
  dictionary forms must include a valid `scheme`. Changes apply on reconnect,
  not retroactively to open connections.

New wallet and inline-game capabilities require explicit application calls; they
are not forwarding settings. Existing forwarding clients need no new feature flag.
Consumer rollout must still verify the exact immutable package and existing
application compatibility before activation. See the upgrade notes below when
coming from older package versions.

## Installation and upgrades

Production consumers must use an approved immutable commit archive or wheel with
its checksum and dependency set. The `dev.zip` URL changes whenever `dev` is
pushed and must not serve as a production release or rollback reference. PyPI's
`kurigram` distribution is the upstream project and does not identify this fork.

Before upgrading from `2.2.25.post1`, check these consumer contracts:

- Own the event loop explicitly when using asynchronous client methods;
  `Client.loop` and the constructor's `loop` parameter have been removed.
- Forum topic methods use `name` and `message_thread_id`; closing or hiding a
  topic uses the corresponding dedicated methods.
- `InputRichMessage.write` is asynchronous and needs `client`; custom rich-media
  wrappers must support the native serializer's calling contract.
- Message methods use `ephemeral_message_parameters` in place of the former
  receiver/callback parameters. This fork retains deprecated `send_voice`
  arguments for compatibility; other callers must migrate.
- Bound message methods have changed positional arguments and removed legacy
  reply keywords. Check callers against the exact candidate signatures.
- Upload errors now propagate to the caller. Dependency declarations use minimum
  versions instead of maximum versions, so record the resolved dependency set.

Deploy consumer code that works with both package versions before changing the
package pin. Verify the exact interpreter, imported package and running process,
then exercise a bounded canary before expanding the rollout. Retain the previous
consumer revision, package artifact and dependency set for rollback.

## Development

The upstream `uv` workflow is retained: `make sync`, `make api`, `make test-unit`,
`make lint`, and `make typecheck`. The dependency floor and ceiling checks are
`make test-floor` and `make test-ceil`. Live integration tests require separate
credentials and authorization; they are not part of the offline unit checks.

Upstream documentation is available at [docs.kurigram.icu](https://docs.kurigram.icu).
Fork-specific compatibility must also be checked against the retained tests and
the exact consumer release under review.
