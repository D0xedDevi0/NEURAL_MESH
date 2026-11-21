# External agent identity bindings (v1)

`agent_id` labels in the ledger are not identity proof. `neural_mesh.identity_registry.IdentityRegistry` provides a separate, append-only, signed `agent_id → EVM address` binding log. It is **not** an API endpoint and does not automatically authenticate existing ledger rows.

Store the JSONL registry **outside the Mesh database and checkout**. Each first binding requires the new address's EIP-191 signature over `IdentityRegistry.challenge(agent_id, address, ledger_genesis, expected_head, 1)`. A rotation requires signatures from **both** the previous and new address over the same revision- and head-bound challenge. Keep an independently retained SHA-256 `record_hash` head outside the registry file; provide it as `expected_head` to every bind or resolve. Only the initial empty registry uses `expected_head=''`. After a successful bind, retain the returned `record_hash` through an independent trust channel before accepting further changes. The library cannot atomically update a remote/off-host pin, so a crashed pin update requires manual reconciliation; never replace a trusted pin with a hash read from the suspect registry.

For portable read-only verification without database write access:

```bash
python scripts/verify_identity_registry.py /external/identities.jsonl LEDGER_GENESIS INDEPENDENT_HEAD
```

The verifier checks the genesis, revision sequence, prior hash, address-control signatures, dual-signed rotations, and the independently supplied final head. It exits nonzero for a mismatch. `EMPTY` is allowed only for an intentionally empty initial registry.

This is a source-side primitive, not a production identity service or on-chain anchor. No production binding exists until an actual agent authorizes one and its head is independently pinned. Existing unsigned or previously signed ledger entries must not be retroactively attributed to these addresses.
