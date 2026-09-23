# docs-final-output-completion-guard quality-portable candidate v3

Release state: publicly published source repository at https://github.com/kkwelfare/docs-final-output-completion-guard-public on `main`. Repository publication was performed as a separate step; gateway changes, live profile changes, credential changes, and provider calls were not performed by this candidate build.

This archive is a quality-portable backup, not a full environment recovery image. It contains deterministic local quality logic, the canonical final-output checker source, portable contract tests, public producer seams, schemas, and versioned presets. It does not contain a Hermes profile, gateway state, task state, runtime ledgers, credentials, host configuration, a renderer installation, or generated user documents.

## What is portable

- `canonical_checker/` contains the MIT-covered `filter_core.py`, `check_document.py`, plugin metadata, and copied portable checker tests. The checker is local, deterministic, fail-open only where its rules require ambiguity preservation, and never calls a provider.
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

## Format and evidence boundary

The public adapters inspect UTF-8 text/Markdown, JSON, DOCX, PPTX, and PDF inputs. Text inputs are checked by the canonical local checker; JSON and container inputs receive structural checks and require a host renderer for a real document render. The evidence receipt records paths, formats, counts, hashes, rule IDs, and statuses only. It rejects raw text/body/content fields, binary payloads, provider payload bodies, and image transport. The candidate never sends images to a provider.

Jev/provider review is optional advisory context. It is disabled by default, is never authoritative, and cannot override a deterministic local block. Portable tests and the host integration readback use no provider call.

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
