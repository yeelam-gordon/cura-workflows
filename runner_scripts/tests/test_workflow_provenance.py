import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from runner_scripts import workflow_provenance

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"


def read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def runner_matrix(*flags):
    result = subprocess.run(
        [sys.executable, str(ROOT / "runner_scripts" / "make_runners_list.py"), *flags],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["include"]


def provenance_artifact_names(matrix):
    return [
        f"package-provenance-create-12345-1-{entry['platform']}"
        for entry in matrix
    ]


def test_setup_action_supports_pinned_helpers_and_arm_identity():
    text = (ACTIONS / "setup-build-environment" / "action.yml").read_text(encoding="utf-8")
    for token in (
        "cleanup_workspace:",
        "if: ${{ inputs.cleanup_workspace == 'true' }}",
        "ref: ${{ inputs.cura_workflows_branch }}",
        "Setup Python (Windows ARM64)",
        "python-path:",
        "python-base-prefix:",
        "python-version:",
        "python-machine:",
        "^[0-9a-fA-F]{40}$",
    ):
        assert token in text
    assert "windows_arm_setup.ps1" not in text


def test_package_ref_is_declared_and_forwarded_at_every_boundary():
    package = read("conan-package.yml")
    for workflow in ("conan-recipe-version.yml", "conan-recipe-export.yml", "make-runners-list.yml"):
        assert f"uses: ./.github/workflows/{workflow}" in package
    assert package.count("cura_workflows_ref: ${{ inputs.cura_workflows_ref }}") >= 4
    assert "allow_non_default_branch_package_create:" in package
    assert "inputs.allow_non_default_branch_package_create" in package
    assert "platform_windows_arm64" in package
    assert "job.workflow_sha" in package
    assert "job.workflow_ref" in package
    assert "validate-callers" in package
    assert "create-package-chain" in package
    assert "validated-package-provenance-" in package
    assert "allow_non_default_branch_package_create requires cura_workflows_ref to be an exact 40-hex SHA" in package


def test_nested_workflows_use_only_local_pinned_actions():
    version = read("conan-recipe-version.yml")
    export = read("conan-recipe-export.yml")
    for text in (version, export):
        assert "cura_workflows_ref:" in text
        assert "cleanup_workspace: false" in text
        assert "cura_workflows_branch: ${{ inputs.cura_workflows_ref }}" in text
        assert "uses: ./_cura_workflows_action/.github/actions/setup-build-environment" in text
        assert "^[0-9a-fA-F]{40}$" in text
    assert "uses: ./_cura_workflows_action/.github/actions/upload-conan-package" in export
    combined = read("conan-package.yml") + version + export
    assert "ultimaker/cura-workflows/.github/actions/setup-build-environment@main" not in combined.lower()
    assert "ultimaker/cura-workflows/.github/actions/upload-conan-package@main" not in combined.lower()


def test_broadcast_data_runs_from_checked_out_recipe_root_with_pinned_helper():
    workflow = yaml.safe_load(read("conan-recipe-version.yml"))
    step = next(
        step
        for step in workflow["jobs"]["make-versions"]["steps"]
        if step.get("id") == "get-conan-broadcast-data"
    )
    assert step["working-directory"] == "_package_sources/${{ inputs.conan_recipe_root }}"
    assert 'conan inspect "."' in step["run"]
    assert (
        'python "$GITHUB_WORKSPACE/Cura-workflows/runner_scripts/'
        'get_conan_broadcast_data.py"'
    ) in step["run"]


def test_credential_free_validation_skips_only_write_recipe_uploads():
    package = yaml.safe_load(read("conan-package.yml"))
    export = yaml.safe_load(read("conan-recipe-export.yml"))
    package_inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    export_inputs = export.get("on", export[True])["workflow_call"]["inputs"]
    for inputs in (package_inputs, export_inputs):
        assert inputs["validation_skip_recipe_upload"]["default"] is False

    package_jobs = package["jobs"]
    for name in ("conan-recipe-export-specific", "conan-recipe-export-latest"):
        assert package_jobs[name]["with"]["validation_skip_recipe_upload"] == (
            "${{ inputs.validation_skip_recipe_upload }}"
        )

    export_steps = export["jobs"]["package-export"]["steps"]
    upload = next(step for step in export_steps if step["name"] == "Upload the Recipe")
    skipped = next(
        step
        for step in export_steps
        if step["name"] == "Record validation-only upload skip"
    )
    assert upload["if"] == "${{ inputs.validation_skip_recipe_upload == false }}"
    assert upload["uses"].endswith("/upload-conan-package")
    assert skipped["if"] == "${{ inputs.validation_skip_recipe_upload }}"

    create = package_jobs["conan-package-create"]
    assert "conan-recipe-export-specific" in create["needs"]
    assert "conan-recipe-export-latest" in create["needs"]
    assert create["runs-on"] == "${{ matrix.runner }}"
    create_command = next(
        step["run"]
        for step in create["steps"]
        if step["name"] == "Create the Package (binaries)"
    )
    assert 'conan create "_package_sources/${{ inputs.conan_recipe_root }}"' in create_command


def test_validation_mpdecimal_override_is_pinned_and_fail_closed():
    package = yaml.safe_load(read("conan-package.yml"))
    inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    assert inputs["validation_mpdecimal_recipe_ref"]["default"] == ""

    steps = package["jobs"]["conan-package-create"]["steps"]
    checkout = next(
        step for step in steps if step["name"] == "Checkout validation mpdecimal recipe"
    )
    export = next(
        step
        for step in steps
        if step["name"] == "Export validation mpdecimal recipe override"
    )
    condition = (
        "${{ matrix.runner == 'windows-11-arm' && "
        "inputs.validation_mpdecimal_recipe_ref != '' }}"
    )
    assert checkout["if"] == condition
    assert checkout["with"]["repository"] == "yeelam-gordon/conan-center-index"
    assert checkout["with"]["ref"] == "${{ inputs.validation_mpdecimal_recipe_ref }}"
    assert checkout["with"]["sparse-checkout"] == "recipes/mpdecimal"
    assert export["if"] == condition
    assert "restricted to credential-free validation mode" in export["run"]
    assert "must be an exact 40-hex SHA" in export["run"]
    assert "rev-parse HEAD" in export["run"]
    assert "conan export _mpdecimal_recipe/recipes/mpdecimal/all --version 2.5.1" in export["run"]
    assert steps.index(export) < next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Create the Package (binaries)"
    )


def test_validation_conan_config_override_is_pinned_and_preflights_libffi():
    package = yaml.safe_load(read("conan-package.yml"))
    inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    assert inputs["validation_conan_config_ref"]["default"] == ""

    steps = package["jobs"]["conan-package-create"]["steps"]
    checkout = next(
        step for step in steps if step["name"] == "Checkout validation Conan configuration"
    )
    install = next(
        step for step in steps if step["name"] == "Install validation Conan configuration"
    )
    preflight = next(
        step for step in steps if step["name"] == "Preflight Windows ARM64 libffi"
    )
    condition = (
        "${{ matrix.runner == 'windows-11-arm' && "
        "inputs.validation_conan_config_ref != '' }}"
    )
    assert checkout["if"] == install["if"] == preflight["if"] == condition
    assert checkout["with"]["repository"] == "yeelam-gordon/conan-config"
    assert checkout["with"]["ref"] == "${{ inputs.validation_conan_config_ref }}"
    assert "restricted to credential-free validation mode" in install["run"]
    assert "must be an exact 40-hex SHA" in install["run"]
    assert "rev-parse HEAD" in install["run"]
    assert "conan config install _validation_conan_config" in install["run"]
    assert preflight["env"]["CURA_WINDOWS_ARM64_X64_BUILD_CONTEXT"] == "1"
    assert "conan install --requires=libffi/3.4.4" in preflight["run"]
    assert "--build='libffi/*' --build=missing" in preflight["run"]
    create_index = next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Create the Package (binaries)"
    )
    assert steps.index(install) < steps.index(preflight) < create_index


def test_validation_conan_cache_is_bounded_package_only_and_failure_safe():
    package = yaml.safe_load(read("conan-package.yml"))
    inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    assert inputs["validation_conan_cache_key"]["default"] == ""

    steps = package["jobs"]["conan-package-create"]["steps"]
    validate = next(
        step for step in steps if step["name"] == "Validate Windows ARM64 Conan cache namespace"
    )
    restore = next(
        step for step in steps if step["name"] == "Restore Windows ARM64 Conan package cache"
    )
    create = next(
        step for step in steps if step["name"] == "Create the Package (binaries)"
    )
    clean = next(
        step for step in steps if step["name"] == "Remove regenerable Conan cache content"
    )
    save = next(
        step for step in steps if step["name"] == "Save Windows ARM64 Conan package cache"
    )
    fail = next(
        step for step in steps if step["name"] == "Fail when Conan package creation failed"
    )
    condition = (
        "${{ matrix.runner == 'windows-11-arm' && "
        "inputs.validation_conan_cache_key != '' }}"
    )
    assert validate["if"] == restore["if"] == condition
    assert "restricted to credential-free validation mode" in validate["run"]
    assert "^[A-Za-z0-9._-]{1,160}$" in validate["run"]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"]["path"] == "~/.conan2/p"
    assert "~/.conan2/p" in save["with"]["path"]
    assert "~/.conan2/" not in save["with"]["path"].replace("~/.conan2/p", "")
    assert "runner.os" in restore["with"]["key"]
    assert "runner.arch" in restore["with"]["key"]
    assert "github.run_id" in restore["with"]["key"]
    assert restore["with"]["restore-keys"].endswith("${{ runner.arch }}-\n")
    assert create["continue-on-error"] is True
    assert create["timeout-minutes"] == 300
    assert clean["if"].startswith("${{ always()")
    assert "--source --build --download --temp" in clean["run"]
    assert save["if"].startswith("${{ always()")
    assert save["uses"] == "actions/cache/save@v4"
    assert save["with"]["key"] == (
        "${{ steps.validation-conan-cache.outputs.cache-primary-key }}"
    )
    assert fail["if"] == "${{ steps.conan-create.outcome == 'failure' }}"
    assert steps.index(restore) < steps.index(create) < steps.index(clean)
    assert steps.index(clean) < steps.index(save) < steps.index(fail)


def test_validation_uranium_override_is_exact_and_fail_closed():
    package = yaml.safe_load(read("conan-package.yml"))
    inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    assert inputs["validation_uranium_ref"]["default"] == ""

    steps = package["jobs"]["conan-package-create"]["steps"]
    checkout = next(
        step for step in steps if step["name"] == "Checkout validation Uranium package"
    )
    export = next(
        step for step in steps if step["name"] == "Export validation Uranium package override"
    )
    condition = (
        "${{ matrix.runner == 'windows-11-arm' && "
        "inputs.validation_uranium_ref != '' }}"
    )
    assert checkout["if"] == export["if"] == condition
    assert checkout["with"]["repository"] == "yeelam-gordon/Uranium"
    assert checkout["with"]["ref"] == "${{ inputs.validation_uranium_ref }}"
    assert "restricted to credential-free validation mode" in export["run"]
    assert "must be an exact 40-hex SHA" in export["run"]
    assert "rev-parse HEAD" in export["run"]
    assert (
        "conan export _uranium_override --version 5.14.0-alpha.0 "
        "--user ultimaker --channel testing"
    ) in export["run"]
    assert steps.index(export) < next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Create the Package (binaries)"
    )


def test_validation_iocpsupport_override_builds_and_tests_exact_arm64_wheel():
    package = yaml.safe_load(read("conan-package.yml"))
    inputs = package.get("on", package[True])["workflow_call"]["inputs"]
    assert inputs["validation_iocpsupport_ref"]["default"] == ""

    steps = package["jobs"]["conan-package-create"]["steps"]
    checkout = next(
        step for step in steps
        if step["name"] == "Checkout validation IOCP support package"
    )
    build = next(
        step for step in steps
        if step["name"] == "Build validation IOCP support ARM64 wheel"
    )
    condition = (
        "${{ matrix.runner == 'windows-11-arm' && "
        "inputs.validation_iocpsupport_ref != '' }}"
    )
    assert checkout["if"] == build["if"] == condition
    assert checkout["with"]["repository"] == "yeelam-gordon/twisted-iocpsupport"
    assert checkout["with"]["ref"] == "${{ inputs.validation_iocpsupport_ref }}"
    assert "restricted to credential-free validation mode" in build["run"]
    assert "must be an exact 40-hex SHA" in build["run"]
    assert build["env"]["CIBW_BUILD"] == "cp312-win_arm64"
    assert build["env"]["CIBW_ARCHS_WINDOWS"] == "ARM64"
    assert "cibuildwheel==3.2.1" in build["run"]
    assert "twisted_iocpsupport-1.0.4-cp312-cp312-win_arm64.whl" in build["run"]
    assert 'assert "Version: 1.0.4\\n" in metadata' in build["run"]
    assert 'requirement["url"] = wheel.as_uri()' in build["run"]
    assert steps.index(build) < next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Create the Package (binaries)"
    )


def test_runner_list_checkout_is_pinned_and_asserted():
    text = read("make-runners-list.yml")
    assert "cura_workflows_ref:" in text
    assert "ref: ${{ inputs.cura_workflows_ref }}" in text
    assert "Requested Cura-workflows ref:" in text
    assert "Resolved Cura-workflows SHA:" in text
    assert "^[0-9a-fA-F]{40}$" in text


def test_runner_nonempty_output_deterministically_gates_package_creation():
    runners = yaml.safe_load(read("make-runners-list.yml"))
    package = yaml.safe_load(read("conan-package.yml"))

    reusable_outputs = runners.get("on", runners[True])["workflow_call"]["outputs"]
    runner_job = runners["jobs"]["make-runners-list"]
    assert reusable_outputs["has_runners"]["value"] == (
        "${{ jobs.make-runners-list.outputs.has_runners }}"
    )
    assert runner_job["outputs"]["has_runners"] == (
        "${{ steps.call-make-runner-script.outputs.has_runners }}"
    )
    runner_step = next(
        step
        for step in runner_job["steps"]
        if step.get("id") == "call-make-runner-script"
    )
    assert 'echo "has_runners=$HAS_RUNNERS" >> "$GITHUB_OUTPUT"' in runner_step["run"]

    create = package["jobs"]["conan-package-create"]
    assert create["if"] == (
        "${{ needs.make-runners-list.outputs.has_runners == 'true' && "
        "(github.ref_name == 'main' || github.ref_name == 'master' || "
        "inputs.allow_non_default_branch_package_create) }}"
    )
    assert ".include.length" not in read("conan-package.yml")
    assert create["strategy"]["matrix"] == (
        "${{ fromJson(needs.make-runners-list.outputs.matrix) }}"
    )


def test_proof_validation_fetches_only_exact_parent_history_before_validation():
    package = yaml.safe_load(read("conan-package.yml"))
    step = next(
        step
        for step in package["jobs"]["conan-package-create"]["steps"]
        if step["name"] == "Validate immutable source callers"
    )
    run = step["run"]
    fetch = 'git -C _package_sources fetch --no-tags --depth=2 origin "$validation_sha"'
    assert step["if"] == "${{ inputs.allow_non_default_branch_package_create }}"
    assert "validation_sha='${{ github.sha }}'" in run
    assert fetch in run
    assert run.index(fetch) < run.index("validate-callers")
    assert '--validation-sha "$validation_sha"' in run
    assert "fetch-depth: 0" not in read("conan-package.yml")
    assert "fetch --unshallow" not in run


def test_linux_and_wasm_provenance_artifact_names_are_unique():
    matrix = runner_matrix("--platform-linux", "--platform-wasm")
    assert [entry["runner"] for entry in matrix] == ["ubuntu-latest", "ubuntu-latest"]
    assert [entry["platform"] for entry in matrix] == ["linux", "wasm"]
    names = provenance_artifact_names(matrix)
    assert len(names) == len(set(names))
    package = read("conan-package.yml")
    assert "package-provenance-create-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.platform }}" in package
    assert "package-provenance-create-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.runner }}" not in package
    assert "pattern: package-provenance-create-${{ github.run_id }}-${{ github.run_attempt }}-*" in package


def test_all_platform_provenance_artifact_names_are_unique_and_stable():
    matrix = runner_matrix(
        "--platform-linux",
        "--platform-windows",
        "--platform-mac",
        "--platform-windows-arm64",
        "--platform-wasm",
    )
    assert [entry["platform"] for entry in matrix] == [
        "linux",
        "windows",
        "macos",
        "windows-arm64",
        "wasm",
    ]
    names = provenance_artifact_names(matrix)
    assert len(names) == len(set(names)) == 5


def test_windows_arm_uses_clean_emulated_x64_build_profile():
    matrix = runner_matrix("--platform-windows-arm64")
    assert matrix == [
        {
            "platform": "windows-arm64",
            "runner": "windows-11-arm",
            "conan_extra_args": (
                "-s:h arch=armv8 -pr:b default -s:b arch=x86_64 "
                "-s:b compiler.cppstd=17 -s:b \"curator/*:compiler.cppstd=20\" "
                "-c:b tools.build:skip_test=True"
            ),
        }
    ]
    arm_args = matrix[0]["conan_extra_args"]
    assert "-pr:b default" in arm_args
    assert "cura_build.jinja" not in arm_args
    assert "tools.microsoft.bash:path" not in arm_args
    package = yaml.safe_load(read("conan-package.yml"))
    create_step = next(
        step
        for step in package["jobs"]["conan-package-create"]["steps"]
        if step["name"] == "Create the Package (binaries)"
    )
    assert create_step["env"]["CURA_WINDOWS_ARM64_X64_BUILD_CONTEXT"] == (
        "${{ matrix.runner == 'windows-11-arm' && '1' || '0' }}"
    )
    assert create_step["run"].endswith(
        "--build=missing ${{ matrix.conan_extra_args }}"
    )


def test_all_required_package_provenance_instances_exist():
    text = "\n".join(
        read(name)
        for name in (
            "conan-package.yml",
            "conan-recipe-version.yml",
            "conan-recipe-export.yml",
            "make-runners-list.yml",
        )
    )
    required = (
        "conan-package/workflow",
        "conan-recipe-version/workflow",
        "conan-recipe-version/setup-action",
        "conan-recipe-version/setup-helper-checkout",
        "make-runners-list/workflow",
        "make-runners-list/script-checkout",
        "conan-package-create/setup-action",
        "conan-package-create/setup-helper-checkout",
    )
    for instance in required:
        assert instance in text
    package = read("conan-package.yml")
    export = read("conan-recipe-export.yml")
    for instance in ("conan-recipe-export-specific", "conan-recipe-export-latest"):
        assert f"provenance_instance: {instance}" in package
    for suffix in ("/workflow", "/setup-action", "/setup-helper-checkout", "/upload-action"):
        assert "${{ inputs.provenance_instance }}" + suffix in export
    assert "component_path" in text
    assert "requested_ref" in text
    assert "resolved_sha" in text
    assert "status" in text
    assert text.count("job.workflow_sha") >= 4


def test_installer_binds_validated_package_chain_and_actual_workflow_invocation():
    arm = read("cura-installer-windows-arm.yml")
    for token in (
        "package_workflow_run_id:",
        "package_workflow_run_attempt:",
        "validated-package-provenance-",
        "validate-callers",
        "validate-package-chain",
        "job.workflow_ref",
        "job.workflow_sha",
        "metadata\\package-provenance.json",
        "package_workflow_run_id requires cura_workflows_ref to be an exact 40-hex SHA",
    ):
        assert token in arm
    assert "C=$implementationSha" not in arm


def test_combined_windows_artifact_names_are_architecture_qualified_and_unique():
    x64 = read("cura-installer-windows.yml")
    arm = read("cura-installer-windows-arm.yml")
    for name in (
        "windows-x64-UltiMaker-Cura.exe",
        "windows-x64-CuraEngine.exe",
        "windows-arm64-UltiMaker-Cura.exe",
        "windows-arm64-CuraEngine.exe",
    ):
        assert name in x64 + arm
    assert "\n          name: UltiMaker-Cura.exe\n" not in x64 + arm
    assert "\n          name: CuraEngine.exe\n" not in x64 + arm


def test_smoke_installers_are_hash_and_size_bound_to_signed_release_evidence():
    arm = read("cura-installer-windows-arm.yml")
    assert "--include installers" in arm
    assert "signed\\installers" in arm
    assert "path=\"installers/$($exe.Name)\"" in arm
    assert "path=\"installers/$($msi.Name)\"" in arm
    assert "verify-release `" in arm
    assert "--evidence signed\\metadata\\release-evidence.json" in arm


def test_provenance_validator_rejects_missing_conflicting_duplicate_and_wrong_sha(tmp_path):
    requested = "a" * 40
    rows = [
        f"{instance}|{workflow_provenance.REQUIRED_COMPONENTS[instance]}|{requested}"
        for instance in sorted(workflow_provenance.REQUIRED_INSTANCES)
    ]
    workflow_provenance.create(tmp_path / "all.json", requested, rows)
    assert len(workflow_provenance.validate(tmp_path, requested)) == 16

    broken = tmp_path / "all.json"
    records = __import__("json").loads(broken.read_text(encoding="utf-8"))
    records.pop()
    broken.write_text(__import__("json").dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="missing provenance"):
        workflow_provenance.validate(tmp_path, requested)

    workflow_provenance.create(broken, requested, rows)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(broken.read_text(encoding="utf-8"), encoding="utf-8")
    assert len(workflow_provenance.validate(tmp_path, requested)) == 16

    duplicate_records = json.loads(duplicate.read_text(encoding="utf-8"))
    duplicate_records[0]["component_path"] = ".github/actions/unrelated/action.yml"
    duplicate.write_text(json.dumps(duplicate_records), encoding="utf-8")
    with pytest.raises(ValueError, match="component path mismatch|conflicting duplicate provenance"):
        workflow_provenance.validate(tmp_path, requested)

    duplicate.unlink()
    records = __import__("json").loads(broken.read_text(encoding="utf-8"))
    records[0]["resolved_sha"] = "b" * 40
    broken.write_text(__import__("json").dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="resolved SHA mismatch"):
        workflow_provenance.validate(tmp_path, requested)


def test_provenance_validator_rejects_unrelated_component_path(tmp_path):
    requested = "a" * 40
    rows = [
        f"{instance}|{workflow_provenance.REQUIRED_COMPONENTS[instance]}|{requested}"
        for instance in sorted(workflow_provenance.REQUIRED_INSTANCES)
    ]
    workflow_provenance.create(tmp_path / "all.json", requested, rows)
    records = json.loads((tmp_path / "all.json").read_text(encoding="utf-8"))
    records[0]["component_path"] = ".github/actions/unrelated/action.yml"
    (tmp_path / "all.json").write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="component path mismatch"):
        workflow_provenance.validate(tmp_path, requested)


def test_workflow_invocation_requires_literal_matching_target():
    requested = "a" * 40
    workflow_provenance.verify_workflow_invocation(
        f"yeelam-gordon/cura-workflows/.github/workflows/conan-package.yml@{requested}",
        requested,
        requested,
        ".github/workflows/conan-package.yml",
    )
    with pytest.raises(ValueError, match="exact 40-hex"):
        workflow_provenance.verify_workflow_invocation(
            "yeelam-gordon/cura-workflows/.github/workflows/conan-package.yml@refs/heads/main",
            requested,
            "main",
            ".github/workflows/conan-package.yml",
        )


def _git(repository, *arguments):
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True)


def _validation_callers(workflow_sha):
    mpdecimal_recipe_sha = "d" * 40
    conan_config_sha = "e" * 40
    conan_cache_key = "cura-arm64-deps-v1-c-w-mpdecimal-config"
    uranium_sha = "f" * 40
    iocpsupport_sha = "1" * 40
    package = f"""jobs:
  package:
    uses: yeelam-gordon/cura-workflows/.github/workflows/conan-package.yml@{workflow_sha}
    with:
      cura_workflows_ref: {workflow_sha}
      allow_non_default_branch_package_create: true
      validation_skip_recipe_upload: true
      validation_mpdecimal_recipe_ref: {mpdecimal_recipe_sha}
      validation_conan_config_ref: {conan_config_sha}
      validation_conan_cache_key: {conan_cache_key}
      validation_uranium_ref: {uranium_sha}
      validation_iocpsupport_ref: {iocpsupport_sha}
      platform_windows_arm64: true
      platform_linux: false
      platform_windows: false
      platform_mac: false
      platform_wasm: false
"""
    installer = f"""jobs:
  installer:
    uses: yeelam-gordon/cura-workflows/.github/workflows/cura-installer-windows-arm.yml@{workflow_sha}
    with:
      cura_workflows_ref: {workflow_sha}
      cura_conan_version: ${{{{ inputs.cura_conan_version }}}}
      package_workflow_run_id: ${{{{ inputs.package_workflow_run_id }}}}
      package_workflow_run_attempt: ${{{{ inputs.package_workflow_run_attempt }}}}
"""
    return package, installer


def test_validation_callers_enforce_single_c_v_w_chain(tmp_path):
    repository = tmp_path / "Cura"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "product.txt").write_text("implementation", encoding="utf-8")
    (workflows / "conan-package.yml").write_text("original package", encoding="utf-8")
    (workflows / "windows-arm.yml").write_text("original installer", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "C")

    workflow_sha = "a" * 40
    package, installer = _validation_callers(workflow_sha)
    (workflows / "conan-package.yml").write_text(package, encoding="utf-8")
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "V")
    validation_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    chain = workflow_provenance.validate_callers(repository, validation_sha, workflow_sha)
    assert chain["V"] == validation_sha
    assert chain["W"] == workflow_sha
    assert chain["mpdecimal_recipe"] == "d" * 40
    assert chain["conan_config"] == "e" * 40
    assert chain["conan_cache_key"] == "cura-arm64-deps-v1-c-w-mpdecimal-config"
    assert chain["uranium"] == "f" * 40
    assert chain["iocpsupport"] == "1" * 40

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace(f"conan-package.yml@{workflow_sha}", "conan-package.yml@main"),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "mutable caller")
    mutable_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="package workflow target"):
        workflow_provenance.validate_callers(repository, mutable_sha, workflow_sha)

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace("      validation_skip_recipe_upload: true\n", ""),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "upload skip not opted in")
    unopted_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="validation_skip_recipe_upload=true"):
        workflow_provenance.validate_callers(repository, unopted_sha, workflow_sha)

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace(f"      validation_conan_config_ref: {'e' * 40}\n", ""),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Conan config not pinned")
    unpinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="validation_conan_config_ref"):
        workflow_provenance.validate_callers(repository, unpinned_sha, workflow_sha)

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace(
            "      validation_conan_cache_key: cura-arm64-deps-v1-c-w-mpdecimal-config\n",
            "",
        ),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Conan cache not bounded")
    unbounded_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="validation_conan_cache_key"):
        workflow_provenance.validate_callers(repository, unbounded_sha, workflow_sha)

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace(f"      validation_uranium_ref: {'f' * 40}\n", ""),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Uranium override not pinned")
    uranium_unpinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="validation_uranium_ref"):
        workflow_provenance.validate_callers(
            repository, uranium_unpinned_sha, workflow_sha
        )

    _git(repository, "reset", "--hard", "HEAD^")
    (workflows / "conan-package.yml").write_text(
        package.replace(f"      validation_iocpsupport_ref: {'1' * 40}\n", ""),
        encoding="utf-8",
    )
    (workflows / "windows-arm.yml").write_text(installer, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "IOCP support override not pinned")
    iocpsupport_unpinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="validation_iocpsupport_ref"):
        workflow_provenance.validate_callers(
            repository, iocpsupport_unpinned_sha, workflow_sha
        )


def test_package_chain_binds_reference_run_and_c_v_w(tmp_path):
    source = tmp_path / "source.json"
    package = tmp_path / "package.json"
    source.write_text(
        json.dumps({"schema_version": 1, "C": "a" * 40, "V": "b" * 40, "W": "c" * 40}),
        encoding="utf-8",
    )
    workflow_provenance.create_package_chain(source, "cura/1@u/c", "123", "2", package)
    assert workflow_provenance.validate_package_chain(
        package, source, "cura/1@u/c", "123", "2"
    )["package_reference"] == "cura/1@u/c"
    with pytest.raises(ValueError, match="does not match"):
        workflow_provenance.validate_package_chain(
            package, source, "cura/other@u/c", "123", "2"
        )
