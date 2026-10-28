"""Shared, stdlib-only code imported by the cloud backend.

`shared.job_record` holds `clean_text`, the one text-normalization primitive
still live (reached from `POST /api/jobs`). The local desktop tools this
package once also served (`local_tools/`) are gone (ADR-010, STAB-6, STAB-7);
this module must not import `backend`.
"""
