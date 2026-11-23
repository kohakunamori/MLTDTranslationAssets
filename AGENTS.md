# Project Maintenance & CI Rules

## Offline Verification Commands
- **Workflow & contract check** (mandatory after editing `.github/workflows/*.yml`):
  - Validate YAML syntax with Python `yaml.safe_load`.
  - Run `python -m pytest scripts/test_validate_workflow.py -q -x`
- **Targeted scenario layout regression**:
  - `python -m pytest scripts/test_scenario_text_layout.py -q -x --maxfail=3`
- **Full offline regression suite** (mirrors `.github/workflows/validate-localization.yml`, does not require `locales/` or `lyrics/`):
  - `PYTHONPATH=. python -m pytest -q -n 4 --dist worksteal scripts pipelines/core pipelines/text asset-server server/tests --ignore=scripts/test_given_name_hint_authority.py --ignore=scripts/test_image_model.py --ignore=scripts/test_scenario_text_layout.py --ignore=scripts/test_assets_generated_index.py`
- **Repository & locale validation** (requires `locales/` and `lyrics/`):
  - `python scripts/validate_repo.py --layout-strict --layout-exceptions manifests/scenario-layout-exceptions.json`
  - `python -m unittest scripts.test_given_name_hint_authority`

## GitHub Actions Rules
- Never use unverified GitHub Actions version tags (verify tags exist upstream before pinning or bumping, e.g. `astral-sh/setup-uv@v10.3.3`, `actions/checkout@v7`).
- Keep `validate` and `regression` sparse-checkout rules intact (`generated/` and `images/` stay excluded; `locales/` and `lyrics/` stay out of `regression`).
- The offline `regression` job discovers tests by directory with explicit `--ignore=` exclusions; keep exclusions documented and minimal.
