"""Public distribution: private historical evidence is not uploaded or fabricated."""
from pathlib import Path
import pytest

PRIVATE_EVIDENCE_TESTS = {'test_analysis_build_profile.py': ['test_actual_compose_config_separates_keyless_build_from_runtime_interpolation', 'test_child_sources_are_copied_and_build_config_needs_no_model_key', 'test_parent_provenance_uses_accepted_old_files_and_locks_while_child_may_change'], 'test_boundary_dev_profile.py': ['test_actual_compose_config_is_keyless', 'test_all_inputs_are_copied_without_downloads_or_public_ports', 'test_parent_uses_actual_p82_worker_and_p84_frozen_protocol'], 'test_holdout_collection.py': ['test_new_image_gate_uses_accepted_dev_worker_without_enabling_target_calls'], 'test_milvus_api_joint.py': ['test_joint_gate_rejects_incomplete_or_drifted_baseline'], 'test_native_build_profile.py': ['test_every_expected_child_hash_path_is_copied_to_app_layout'], 'test_paired_eval_profile.py': ['test_actual_compose_config_does_not_need_a_model_key', 'test_layout_inherits_accepted_image_and_copies_all_scored_inputs']}

def pytest_collection_modifyitems(items):
    root=Path(__file__).resolve().parents[1]
    if (root/"docs/evidence/p8/paired-docker-20261105T074259913219").exists(): return
    for item in items:
        if item.path.name in PRIVATE_EVIDENCE_TESTS and item.originalname in PRIVATE_EVIDENCE_TESTS[item.path.name]:
            item.add_marker(pytest.mark.skip(reason="Private historical gate/image receipt intentionally excluded from public distribution"))
