"""Compatibility entry point for the review regressions.

The original one-off failing probes have been promoted into the permanent suite.
Run from the repository root:
  PYTHONPATH="$PWD/tests:$PWD" .venv/bin/python -m pytest -p conftest docs/review-probes.py
"""
from test_whatsapp import internal
from test_review_fixes import (
    test_slow_ask_keeps_public_and_internal_listeners_responsive,
    test_final_account_cleanup_survives_deletion_and_lost_ack,
    test_gmail_burst_resumes_history_pages_without_losing_messages,
    test_long_email_uses_matching_segments_for_answers_and_extraction,
    test_legacy_chunks_are_rebuilt_before_retrieval,
    test_restore_bootstrap_sql_runs_outside_a_transaction,
    test_review_migration_upgrades_existing_data_and_matches_models,
    test_connector_upgrade_and_rollback_preserve_session,
    test_unavailable_connector_upgrade_keeps_working_container,
)
