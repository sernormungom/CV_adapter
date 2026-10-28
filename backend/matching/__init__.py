"""Advisory matching — the deterministic scorer and the config it reads.

On demand, never stored (P-007). `service.score_job_for_consultant` is the only
entry point the Job domain uses; `config_reader` owns reading and writing a
consultant's `matching_config.yaml` and is also used by the learning module.

This package replaces `backend/opportunity_pipeline/pre_filter_matcher/`, whose
batch selection, exploration slots, and JSON-Job-Store scanning were retired
with the V1 review-batch workflow in Slice 1.
"""
