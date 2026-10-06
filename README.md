# docs-final-output-completion-guard quality-portable candidate v3

Release state: publicly published source repository at https://github.com/kkwelfare/docs-final-output-completion-guard-public on `main`. Repository publication was performed as a separate step; gateway changes, live profile changes, credential changes, and provider calls were not performed by this candidate build.

This archive is a quality-portable backup, not a full environment recovery image. It contains deterministic local quality logic, the canonical final-output checker source, portable contract tests, public producer seams, schemas, and versioned presets. It does not contain a Hermes profile, gateway state, task state, runtime ledgers, credentials, host configuration, a renderer installation, or generated user documents.

## What is portable

- `canonical_checker/` is the portable, vendored copy of the public `kkwelfare/final-output-local-filter` v1.0.0 package (`filter_core.py`, `check_document.py`, `plugin.yaml`, MIT license, and tests). It is resolved relative to this repository/package, never from a sibling profile or machine-local plugin root. The checker is deterministic, fail-open only where its rules require ambiguity preservation, and never calls a provider.
- The candidate plugin runtime, quality rules, schemas, and portable tests are preserved from the verified local baseline under the repository root.
- `portable/` exposes a small public producer core, typed body-free evidence boundary, format adapters, deterministic fake-renderer contract, and host adapter. The local checker result is authoritative; a renderer cannot turn a local block into a pass.
- `presets/` contains the versioned `docs-style-default-v1` and `docs-style-compact-v1` presets plus `docs-quality-ruleset-v3`. Producer receipts bind the preset/ruleset identifiers and SHA-256 values.
- `schemas/` contains both the existing quality schemas and the host adapter, style preset, ruleset, renderer contract, and producer receipt schemas.

No public executable module contains a machine-specific absolute path. Candidate paths are resolved relative to the candidate or an explicit host manifest.

## Host adapter contract

The portable route accepts a JSON manifest with schema `docs-host-adapter-manifest-1`, either through `DOCS_GUARD_HOST_MANIFEST` or `ProducerRequest.host_manifest`. Relative paths resolve against the manifest file. The manifest may name:

- the canonical checker root (`check_document.py` and `filter_core.py`);
- renderer kind/executable and an output root;
- explicit font-discovery roots;
- task-state and runtime roots as caller-supplied metadata locations; and
- provider mode, which is limited to `advisory-disabled` or `advisory-opt-in`.

The default manifest-free route uses the bundled checker and `FakeRenderer`. The adapter does not discover credentials, infer a task, start a renderer, or perform network I/O. Real renderer integrations remain host-owned and must satisfy the same renderer contract.

## Plugin generated-artifact review

For eligible PDFs, the plugin's `generated_artifact_jev.run` route supports one bounded evaluation attempt only when its caller explicitly sets `allow_live_provider: true` in `artifact_quality.layout_typography` and a supported provider key is available. The setting defaults to false; missing, false, and unrelated configuration keys do not authorize a provider call. When candidate count is zero, it uses a whole-artifact structural summary. Bound evidence is reused only while its artifact identity and supporting state remain unchanged. The owner scan stays byte-stable; execution details are recorded separately. Failure, uncertainty, or low confidence is not a pass: deterministic checks and the existing owner/completion path retain acceptance authority. The request is metrics-only; it does not include document body text or page pixels.

The existing public layout Jev repository is an optional, separate Hermes plugin: https://github.com/kkwelfare/jev-route-screening-public, pinned interoperability reference `9e2d276c2e2aa6ff5b4b547b31aa100c9695f11e` (`master` at publication). It exposes its documented Hermes tools/hooks (including `jev_bridge_review`); it is not imported as a Python package and does not define this guard's PDF-classification API. This guard's optional PDF advisory uses the explicit TypeSafe SystemOne decisions request contract in `quality_rules/jev_overlap.py`, with a bounded OpenRouter route only under that module's existing permitted fallback conditions. The Jev result is advisory; local deterministic quality rules remain authoritative. This describes an optional producer/host integration, not a universal completion enforcement claim. No provider call is made by the offline tests or plugin load validation.

## Lifecycle logging

The optional Jev overlap, design, and post-render routes emit bounded metadata-only `jev.lifecycle` INFO events around provider attempts, fallback, validation, and result/receipt delivery. Artifact body, user text, and credentials are not logged. Logging failures remain non-authoritative; deterministic local quality and receipt rules retain their existing behavior.

## Format and evidence boundary

The public adapters inspect UTF-8 text/Markdown, JSON, DOCX, PPTX, and PDF inputs. Text inputs are checked by the canonical local checker; JSON and container inputs receive structural checks and require a host renderer for a real document render. The evidence receipt records paths, formats, counts, hashes, rule IDs, and statuses only. It rejects raw text/body/content fields, binary payloads, provider payload bodies, and image transport. The candidate never sends images to a provider.

For the separate portable producer route, provider review remains disabled by default and is advisory-only when explicitly opted in; it cannot override a deterministic local block. Portable tests and the host integration readback use no provider call.

## External dependency setup

A receiving environment supplies its own Python interpreter and package environment. The verification host used Python 3.11 with the packages needed by the existing quality rules and tests, including `pytest`, `jsonschema`, `PyMuPDF`, Pillow, `python-docx`, `python-pptx`, `lxml`, `defusedxml`, and PyYAML. These observations are a setup checklist, not a support promise. Hermes supplies the plugin admission command and host API when the plugin is installed as a plugin; no `requires_hermes` range is asserted.

Optional host renderers (for example LibreOffice or a local browser) are external dependencies. They are not downloaded, configured, or claimed by this archive. Font discovery is also host-supplied through the manifest; no font files are bundled.

## Verification from the candidate root

    hermes plugins validate . --json
    PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider canonical_checker/tests tests test_production_retry_guard.py
    PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .

Validate every schema with `jsonschema.Draft202012Validator.check_schema`. For a bounded producer readback, use a clean local text fixture and run:

    PYTHONPATH=. python scripts/portable_host_integration.py --source <fixture> --output-root <new-output-root>

The producer writes only a body-free `producer-receipt.json` and fake-render metadata below the caller's output root. Use a new output root for each run; do not place runtime outputs inside the release archive.

## Recovery and publication boundary

To recover the quality-portable portion, unpack this archive into a new target, provide the external Python/Hermes/renderer/font setup, and supply a host adapter manifest. Verify checker/core hashes and rerun the portable suite before using it. This does not restore a full Hermes profile, plugin installation, gateway, task history, runtime ledgers, receipts from another environment, credentials, fonts, or external services. Full environment recovery requires a separately authorized host snapshot and should not be inferred from this archive.

`PUBLICATION_MANIFEST.json` records the candidate boundary and exclusions. External inventory and verification receipts record the final archive hash and readback; those receipts are deliberately outside the archive to avoid self-reference.

## Ordinary DOCX completion baseline

For routine non-PDF DOCX delivery, after the canonical final-output checker and the real source/order/text/package and rendered-page readbacks have passed, `ordinary-docx-baseline-1` provides a narrow completion handoff. It is not a universal document-quality pass and does not replace owner review of the actual content/readbacks. It binds the DOCX and its body-free readback handles, genuine final-output receipt, rendered PDF/page evidence, accepted finalization manifest, and a distinct receiver-copy readback. Existing non-text evidence must not be turned into a fabricated semantic or Jev result; optional Jev/PDF advisory status remains exactly as observed, and this route makes no provider call.

From an installed host plugin, use its `scripts/prepare_ordinary_docx_baseline.py` only after the destination mechanism has durably stored and read back the receiver copy:

```bash
python3 /path/to/installed-plugin/scripts/prepare_ordinary_docx_baseline.py \\
  --artifact /absolute/final.docx --receiver-copy /absolute/readback-copy.docx \\
  --source /absolute/authoritative-source.txt --docx-readback /absolute/docx-readback.json \\
  --render-readback /absolute/render-readback.json --rendered-pdf /absolute/rendered.pdf \\
  --representative-page /absolute/preview-first.png --representative-page /absolute/preview-last.png \\
  --final-output-receipt /absolute/final-output-receipt.json \\
  --manifest /absolute/finalization-manifest.json --receiver-receipt /absolute/receiver-receipt.json \\
  --contract /absolute/ordinary-docx-baseline-contract.json \\
  --receiver-task-id <actual-receiver-task-id> --receiver-run-id <actual-receiver-run-id>
```

If the producer identity is known, also pass `--producer-task-id <actual-producer-task-id> --producer-run-id <actual-producer-run-id>`; otherwise omit both flags.

Supply absolute paths to the DOCX, distinct received copy, authoritative source/readback, actual DOCX readback JSON, actual render readback JSON/PDF/page images, and genuine final-output receipt; choose new paths for manifest, receiver receipt, and contract. Provide producer task/run identity only when it is actually known; if unavailable, omit both flags rather than inventing provenance. Always provide the actual receiving task/run identity. The helper prepares records only; it does not submit Kanban completion, run quality measurements, or call a provider.

In the ordinary completion metadata, keep `verification.final_output_filter.receipts` and set `verification.artifact_quality.contract` to the generated contract; no synthetic artifact-quality receipt is used. The registered completion hook validates this explicit baseline contract and its hashes/readbacks. Other artifact kinds, PDFs, and full artifact-quality contracts remain on the existing strict receipt route. This local handoff format is not a claim of universal Hermes/Kanban enforcement: install the plugin hook in the intended host and exercise the real completion path there.
