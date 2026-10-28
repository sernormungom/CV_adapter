"""ORM models. Importing this package registers every model on Base.metadata.

Add new model modules to the imports below so Alembic autogenerate and
create_all see them.
"""

from backend.db.models.application import (
    APPLICATION_STATUS_ORDER,
    APPLICATION_STATUS_VALUES,
    LIVE_APPLICATION_STATUSES,
    TERMINAL_APPLICATION_STATUSES,
    Application,
    ApplicationStatus,
)
from backend.db.models.audit import AuditLog
from backend.db.models.cv_import import CV_IMPORT_STATUS_VALUES, CVImport
from backend.db.models.cv_publish import CVPublication
from backend.db.models.job import (
    JOB_STATUS_VALUES,
    REVIEW_DECISION_VALUES,
    Job,
    JobDecisionPair,
    JobReview,
    JobStatus,
    ReviewDecision,
)
from backend.db.models.notification import Notification
from backend.db.models.session import Invite, UserSession
from backend.db.models.site import Site
from backend.db.models.tailored_cv import (
    TAILORED_CV_STATUS_VALUES,
    TailoredCV,
    TailoredCVStatus,
)
from backend.db.models.user import (
    ROLE_VALUES,
    TAConsultant,
    User,
    UserRole,
    UserRoleAssignment,
)
from backend.db.models.workflow import (
    TERMINAL_STATUSES,
    WorkflowRun,
    WorkflowStatus,
)

__all__ = [
    "WorkflowRun",
    "WorkflowStatus",
    "TERMINAL_STATUSES",
    "User",
    "UserRole",
    "UserRoleAssignment",
    "ROLE_VALUES",
    "TAConsultant",
    "Job",
    "JobDecisionPair",
    "JobReview",
    "JobStatus",
    "ReviewDecision",
    "JOB_STATUS_VALUES",
    "REVIEW_DECISION_VALUES",
    "Application",
    "ApplicationStatus",
    "APPLICATION_STATUS_VALUES",
    "APPLICATION_STATUS_ORDER",
    "LIVE_APPLICATION_STATUSES",
    "TERMINAL_APPLICATION_STATUSES",
    "TailoredCV",
    "TailoredCVStatus",
    "TAILORED_CV_STATUS_VALUES",
    "Site",
    "UserSession",
    "Invite",
    "Notification",
    "CVPublication",
    "CVImport",
    "CV_IMPORT_STATUS_VALUES",
    "AuditLog",
]

from backend.db.models.matching import JobConsultantMatch
