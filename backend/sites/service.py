"""Active Site resolution for account provisioning."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import Site


class SiteConfigurationError(RuntimeError):
    """The deployment no longer satisfies the single-Site pilot boundary."""


def active_sites(db: Session) -> list[Site]:
    return list(
        db.scalars(
            select(Site).where(Site.is_active.is_(True)).order_by(Site.code)
        )
    )


def require_active_site(db: Session, site_id=None, *, code: str | None = None) -> Site:
    """Resolve an explicit active Site; an omitted Site is valid only if unambiguous."""
    sites = active_sites(db)
    matches = [s for s in sites if (site_id is None or s.id == site_id) and (code is None or s.code == code)]
    if len(matches) != 1:
        raise SiteConfigurationError("Choose one configured active Site (GBG, MAL, STH or UPS)")
    return matches[0]
