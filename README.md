# docs-final-output-completion-guard quality-portable candidate v3

## Public quick start

**What it is:** A Hermes Agent plugin for deterministic document checks and bounded, optional final-output review.
**Who it is for:** Hermes users who produce or review documents and want local quality checks plus an explicitly configured host integration.
**Requirements:** Hermes Agent; no `requires_hermes` compatibility range is declared. The manifest declares `PyMuPDF>=1.28.2,<2`; renderers and other verification packages are separate host dependencies described below. The optional host review route requires explicit opt-in and sends bounded text-only metrics/request context, not document body text or page images. The separate Jev image review is also off by default; when enabled it is the one route that sends rendered page images (see below). Provider credentials are resolved by Hermes and the optional routes (`TYPESAFE_API_KEY` and `OPENROUTER_API_KEY`); offline checks do not call them.

**Install and enable** (the installed plugin name comes from `plugin.yaml`):

```sh
hermes plugins install kkwelfare/docs-final-output-completion-guard-public --no-enable
hermes plugins enable docs-final-output-completion-guard
```

**Minimal local check:** from the repository root, run `python3 check_docs_artifact.py --help` to see the checker inputs; for the optional portable producer, use the fixture-based command below with your own source and new output directory. The normal offline checks do not call a provider.

License: [MIT](LICENSE).


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

For eligible PDFs, the plugin's `generated_artifact_jev.run` route supports one bounded evaluation attempt only when its caller explicitly sets `allow_live_provider: true` in `artifact_quality.layout_typography` and a supported provider key is available. The setting defaults to false; missing, false, and unrelated configuration keys do not authorize a provider call. When candidate count is zero, it uses a whole-artifact structural summary. Bound evidence is reused only while its artifact identity and supporting state remain unchanged. The owner scan stays byte-stable; execution details are recorded separately. Failure, uncertainty, or low confidence is not a pass: deterministic checks and the existing owner/completion path retain acceptance authority. The request is metrics-only; it does not include document body text or page pixels. A request whose single send would exceed 32,000 bytes is split into ordered chunks that each fit; each chunk is sent once, bound to the same `request_sha256` and `candidate_set_digest`, and recorded in the receipt's `wire_chunks`. When `layout_typography.required` is true and the generated PDF is eligible, the completion guard blocks until that artifact has live provider evidence (all chunks, or correctly reused evidence), reporting a `reason_code` such as `live_provider_not_allowed`, `evaluation_identity_unavailable`, `provider_unavailable`, `invalid_request`, `insufficient_evidence`, or `timeout`. Jev dispositions themselves keep their existing advisory handling. The checker default stays false, but the PDF contracts written by the companion SVG producer ([`kkwelfare/docs-producers-public`](https://github.com/kkwelfare/docs-producers-public)) set `allow_live_provider: true` by default, round `cjk_appearance_regions` outward to three decimals so edge glyphs are not lost to `insufficient_evidence`, and fill the producer run ID in the finalization manifest when a run identity is available.

### Optional Jev image review (default off)

`quality_rules/jev_visual_review.py` adds an optional, advisory image judgment for generated PDFs. It is enabled only by `artifact_quality.layout_typography.jev_visual_image_review` in the contract, and both `enabled` and `allow_live_provider` default to false. When the key is absent or either value is false, no image is rendered for sending and no provider call is made.

This is the only route in this repository that sends images. What is sent is rendered page images: a downscaled whole-page image, and, for a review candidate, that page image plus an enlarged context crop around the candidate. Page numbers and the candidate's measured numeric values go with them as text; extracted document text and file paths are not sent. The request uses TypeSafe SystemOne with the literal model `jev-1.14.0` and the `TypeSafe-Danger-Zone-Images: true` header. Each request is sent once, with no retry and no fallback to another provider or model. Evidence keeps image hashes, not image bytes.

It runs in two layers:

- Whole-page judgment: one request per page, up to `max_pages` (default 40). Skipped pages are recorded with their reason, not silently dropped.
- Candidate judgment: one request per AQ-LAYOUT-01 review-candidate group, up to `max_candidate_calls` (default 8).

Each judgment asks a Choice question (`repair_required` / `acceptable` / `insufficient_information`) and two Noul questions (`placement_unnatural`, `visible_breakage`). Labels are two-tier. `repair_required` needs a `repair_required` choice with confidence at or above `repair_confidence_threshold` (default 0.5), or `visible_breakage` at or above `likely_threshold` (default 0.5). A lower-confidence `repair_required` choice, or `placement_unnatural` at or above `intent_threshold` (default 0.5, never above `likely_threshold`), becomes `differs_from_surroundings`: the page looks different from its surroundings, so check whether that is intended. Box (location) is asked in a separate request only for the `repair_required` tier, because Box always returns a box; Box requests are capped by `max_box_calls` (default 8), and the returned region is read back with numeric measurements.

The result is written as separate evidence next to the AQ-LAYOUT-01 candidates and summarized in the scan. It never blocks completion, never changes hard failures or the deterministic status, and never satisfies the live-Jev requirement above, which stays with `generated_artifact_jev`. Failures (`provider_unavailable`, `timeout`, `provider_error`, `malformed_response`, `model_mismatch`) are recorded as `not_judged`; they never raise. The offline tests use a fixture transport and recorded answers in `tests/fixtures/jev-visual-review-recorded-answers.json`; they need no network and no API key.

The existing public layout Jev repository is an optional, separate Hermes plugin: https://github.com/kkwelfare/jev-route-screening-public, pinned interoperability reference `9e2d276c2e2aa6ff5b4b547b31aa100c9695f11e` (`master` at publication). It exposes its documented Hermes tools/hooks (including `jev_bridge_review`); it is not imported as a Python package and does not define this guard's PDF-classification API. This guard's optional PDF advisory uses the explicit TypeSafe SystemOne decisions request contract in `quality_rules/jev_overlap.py`, with a bounded OpenRouter route only under that module's existing permitted fallback conditions. The Jev result is advisory; local deterministic quality rules remain authoritative. This describes an optional producer/host integration, not a universal completion enforcement claim. No provider call is made by the offline tests or plugin load validation.

## Lifecycle logging

The optional Jev overlap, design, and post-render routes emit bounded metadata-only `jev.lifecycle` INFO events around provider attempts, fallback, validation, and result/receipt delivery. Artifact body, user text, and credentials are not logged. Logging failures remain non-authoritative; deterministic local quality and receipt rules retain their existing behavior.

## Format and evidence boundary

The public adapters inspect UTF-8 text/Markdown, JSON, DOCX, PPTX, and PDF inputs. Text inputs are checked by the canonical local checker; JSON and container inputs receive structural checks and require a host renderer for a real document render. The evidence receipt records paths, formats, counts, hashes, rule IDs, and statuses only. It rejects raw text/body/content fields, binary payloads, provider payload bodies, and image transport. The portable producer route never sends images to a provider; the only image-sending route is the optional Jev image review above, which is off by default.

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
  --contract /absolute/ordinary-docx-baseline-contract.json
```

If the producer identity is known, also pass `--producer-task-id <actual-producer-task-id> --producer-run-id <actual-producer-run-id>`; otherwise omit both flags. Likewise, pass `--receiver-task-id <actual-receiver-task-id> --receiver-run-id <actual-receiver-run-id>` only when the actual receiving identity is available.

Supply absolute paths to the DOCX, distinct received copy, authoritative source/readback, actual DOCX readback JSON, actual render readback JSON/PDF/page images, and genuine final-output receipt; choose new paths for manifest, receiver receipt, and contract. Provide producer task/run identity only when it is actually known; if unavailable, omit both flags rather than inventing provenance. Receiver task/run identity is optional in the same way. Task/run IDs are records only: a missing ID never blocks, a present ID must be well-formed, and the artifact itself stays bound by per-file sha256, `artifact_set_id`, and the receiver-copy hash. The helper prepares records only; it does not submit Kanban completion, run quality measurements, or call a provider.

In the ordinary completion metadata, keep `verification.final_output_filter.receipts` and set `verification.artifact_quality.contract` to the generated contract; no synthetic artifact-quality receipt is used. The registered completion hook validates this explicit baseline contract and its hashes/readbacks. Other artifact kinds, PDFs, and full artifact-quality contracts remain on the existing strict receipt route. This local handoff format is not a claim of universal Hermes/Kanban enforcement: install the plugin hook in the intended host and exercise the real completion path there.

## Ordinary HTML completion baseline

For a single local `.html`/`.htm` delivery, `ordinary-html-baseline-1` is a narrow completion handoff after a real browser run. It binds the exact HTML hash and local `file://` URL to a browser receipt, representative browser render, applicable interaction checks, canonical final-output receipt, accepted finalization manifest, and receiver-copy readback. `page_load` is required; interaction, pause/resume, grounding, finish, WebGL, reduced-motion, visibility-handler, and external-resource checks are required only when declared applicable. Any reported page/console error or external resource blocks this baseline. DOCX keeps its separate ordinary-DOCX route; PDF and other targets retain the existing strict artifact-quality route. This is not a blanket HTML exemption or a claim that synthetic fixtures are real browser evidence.

The preparer only binds existing absolute-path evidence and validates it; it does not open a browser, render the page, create final-output evidence, or assert a check. Run it from an installed host plugin after those readbacks already exist:

```bash
python3 /path/to/installed-plugin/scripts/prepare_ordinary_html_baseline.py \
  --artifact /absolute/final.html --artifact-path /absolute/final.html \
  --browser-receipt /absolute/browser-receipt.json --render /absolute/browser-render.png \
  --final-output-receipt /absolute/final-output-receipt.json \
  --manifest /absolute/finalization-manifest.json --receiver-receipt /absolute/receiver-receipt.json \
  --applicability /absolute/applicability.json \
  --contract /absolute/ordinary-html-contract.json
```

The browser receipt must identify its actual task/run, the exact artifact SHA-256 and local URL, successful declared checks, no page/console errors, and the actual render handle. Use only observed identity and applicable checks. The normal registered completion hook still requires the canonical final-output receipt; the contract does not replace or fabricate it.

The focused `tests/test_ordinary_html_baseline.py` exercises isolated synthetic receipts for deterministic validator pass/negative behavior only; those fixtures are not browser runs and do not prove the host browser executed the HTML.
