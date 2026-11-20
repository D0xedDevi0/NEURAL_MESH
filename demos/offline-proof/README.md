# Check a memory event record offline

From the extracted kit or repository root, using Python 3.10+:

```sh
python3 -IS scripts/verify_bundle.py demos/offline-proof/record.json
python3 -IS scripts/verify_bundle.py demos/offline-proof/altered.json
```

The first exits 0. The second exits 1 at event 2: `reviewed` became `published`. Both records are synthetic; no actual memories or keys are included. No installation, network, wallet or database is required. Keep the directory layout when extracting the kit.

This checks **internal consistency** of a complete, nonempty, genesis-to-head v1 bundle: event hashes, links, identity, sequence, and Merkle root. It rejects filtered or truncated exports. It does not establish who wrote the events, verify memory contents, or authenticate an `anchored` flag. A fully rewritten history can be internally consistent. To check against a head hash you already trust, add `--expect-head HASH` obtained independently of the file. A hash from this same zip isn't independent evidence.
