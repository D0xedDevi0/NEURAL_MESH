# External agent identity bindings (v1)

`agent_id` labels in the ledger are not identity proof. `neural_mesh.identity_registry.IdentityRegistry` provides a separate, append-only, signed `agent_id → EVM address` binding log. It is **not** an API endpoint and does not automatically authenticate existing ledger rows.

Store the JSONL registry **outside the Mesh database and checkout**. Each first binding requires the new address's EIP-191 signature over `IdentityRegistry.challenge(agent_id, address, ledger_genesis, expected_head, 1)`. A rotation requires signatures from **both** the previous and new address over the same revision- and head-bound challenge. Keep an independently retained SHA-256 `record_hash` head outside the registry file; provide it as `expected_head` to every bind or resolve. Only the initial empty registry uses `expected_head=''`. After a successful bind, retain the returned `record_hash` through an independent trust channel before accepting further changes. The library cannot atomically update a remote/off-host pin, so a crashed pin update requires manual reconciliation; never replace a trusted pin with a hash read from the suspect registry.

For portable read-only verification without database write access:

```bash
python scripts/verify_identity_registry.py /external/identities.jsonl LEDGER_GENESIS INDEPENDENT_HEAD
```

The verifier checks the genesis, revision sequence, prior hash, address-control signatures, dual-signed rotations, and the independently supplied final head. It exits nonzero for a mismatch. `EMPTY` is allowed only for an intentionally empty initial registry.

This is a source-side primitive, not an identity service or an on-chain anchor. Production bindings exist (`d0xeddev`, `self`), retained with an independently pinned head under `ops/neural-mesh/` and re-checked daily from a different host. Existing unsigned ledger entries must not be retroactively attributed to these addresses.

## Enforcement — a signature is not yet an identity

Set `NEURAL_MESH_IDENTITY_REGISTRY` and `/mesh/ledger/verify` resolves the label of every signed entry through the registry **before** it reports `ok`. Without this, a valid signature only proves that *some* key wrote the entry — anyone holding any key could claim any label.

* Signed entry under a label bound to a **different** address → hard failure (`kind: "binding"`). The signature is real; the label claim is not.
* Registry missing, unreadable, or failing its own verification → **also** a failure. Deleting the registry must not switch the check off, so the check fails closed.
* `NEURAL_MESH_IDENTITY_HEAD` — the head retained off the registry host — is enforced when set. Internal consistency alone is something whoever writes the registry can arrange; a rewritten or silently extended registry fails with `independent head mismatch`.
* Signed entry under a label the registry does **not** bind → advisory: counted in `unbound_signed`, named in `unbound_labels`, with advice to bind it. Labels written before the registry existed still have to verify.
* Unsigned entries are never binding-checked, and stay counted separately as `unsigned` so they can never read as authenticated.
* Variable unset → `bindings_source` is empty and no binding is claimed, which is honest rather than safe.

The report gains `bindings` (label → address), `bindings_source`, `bindings_pinned`, `unbound_signed`, `unbound_labels`, and `summary()` says how many labels were bound and how many signed entries sit under an unbound one.
