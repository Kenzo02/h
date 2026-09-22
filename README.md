# Kenzo02 Kurigram fork

This fork of [Kurigram](https://github.com/kurigram-org/kurigram) is used by UserBot
and related Telegram services. The Python import remains `pyrogram`.

Python **3.10 or newer** is required. Package version `2.2.26.post1` identifies the
local integration of upstream `01ad943c9f7f5d6b540f19a1b3649732d3740f27` with this
fork's compatibility and runtime fixes. It is not a statement that the package
has been published or deployed.

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
