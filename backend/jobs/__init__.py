"""The Job domain: relational Jobs, their Consultant links, and Reject markers.

`service.py` owns every state change, `routes.py` is a thin HTTP shell over it,
and the access rules live in `backend/authz/policy.py` as named predicates —
`backend/main.py` composes the router and nothing else
(`docs/plans/v2-pilot.md` §5).
"""
