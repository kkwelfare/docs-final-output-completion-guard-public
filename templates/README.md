# Artifact quality local revision template

`templates/artifact-quality-local-revision.json` is a fill-in template for the canonical `docs-artifact-quality-contract-1` input. It preserves the validator's required `authoritative_source.readback_mode`, measured source hash, 5–8 identified `visible_criteria`, matching `AQ-VISIBLE-01.criterion_ids`, and all ten `rule_contracts`.

Use it as a starting copy, not as a passing contract. Replace every `<fill: ...>` placeholder with task-specific values and evidence measured from the real production run; do not invent hashes, paths, review outcomes, or measurements. The template alone is intentionally incomplete and must fail validation. Structural contract validation is not an artifact-quality receipt or proof that visual/measured evidence passed.

The focused test `tests/test_artifact_quality_local_revision_template.py` fills an inert fixture, validates it with the existing `quality_rules.common.validate_contract` and canonical JSON Schema, and checks that omitted required inputs and the untouched template are rejected. No checker or schema is introduced by this template.
