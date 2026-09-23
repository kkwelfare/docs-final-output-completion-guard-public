# Publication readiness — quality-portable candidate v3

Status: READY_FOR_BOUNDED_PUBLICATION_REVIEW

This candidate is a quality-portable backup candidate, not a full environment recovery package and not an external publication event. It is safe to review as a new tree because the build scope is confined to the candidate workspace and contains no credentials, task/VLR records, runtime ledgers, generated user documents, or machine-specific executable paths.

## Release decisions

1. License: MIT (`LICENSE`), applied to the candidate tree and the included canonical checker source.
2. Deterministic quality authority: bundled `canonical_checker/filter_core.py` and `check_document.py` plus the versioned local ruleset. Optional renderer/provider routes cannot override a local block.
3. Host separation: profile paths, checker relocation, renderer locations, font roots, task state, and runtime roots are caller-supplied through `docs-host-adapter-manifest-1`.
4. Evidence boundary: receipts carry typed metadata, hashes, rule IDs, counts, and statuses; raw body text, provider payload bodies, binary payloads, and images are excluded.
5. Provider boundary: Jev/provider use is advisory and optional; the portable verification path performs no provider call and sends no image.
6. Public tree boundary: candidate-only metadata, tests, presets, schemas, and adapter seams are included; host state and external dependencies remain outside.

## Acceptance readback checklist

- Candidate tree, root MIT license, canonical checker source, and checker tests are present.
- Portable producer core, format adapters, renderer contract, host adapter, versioned presets, and schemas are present.
- Offline fake-renderer contract tests run without credentials or network access.
- Current-host integration is a representative local checker/producer readback only; it is not a claim of gateway activation, provider availability, or full renderer recovery.
- Hermes plugin validation, fresh imports/compile, schema checks, archive member/hash readback, inventory, and verification receipt are supplied with the release handoff.

## Known external setup

The receiver must install its own Python dependencies, compatible Hermes plugin host, optional Office/browser renderer, and fonts. The receiver must create a host manifest and choose explicit output/runtime roots. No credentials or host profile state can be reconstructed from this archive.

The final archive hash, candidate member inventory, test results, plugin validation, path scan, and current-host integration summary are held in the external machine-readable verification receipt. The receipt and archive must be read back together; neither alone is a full recovery proof.
