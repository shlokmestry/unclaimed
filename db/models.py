# SQLAlchemy ORM models, shared by:
#   - the ingestion scripts (via db/session.py, a sync engine)
#   - the FastAPI app (via db/async_session.py, an async engine)
#
# Table metadata is engine-agnostic in SQLAlchemy, so one set of model
# classes works for both the sync ingestion pipeline and the async API.
#
# Columns beyond what each step's brief literally listed are called out with
# a comment; see LOG.md for why each was added.

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MobilityAgency(Base):
    """Raw GTFS feed records pulled from the Mobility Database (Step 2),
    annotated with Transit-app coverage in Step 4."""

    __tablename__ = "mobility_agencies"
    __table_args__ = (
        # Added beyond the Step 2 spec so re-running the ingestion is
        # idempotent (upsert on the Mobility Database's own feed id) instead
        # of appending duplicate rows on every run. See LOG.md.
        UniqueConstraint("source_id", name="uq_mobility_agencies_source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The Mobility Database's own feed id (e.g. "mdb-1210"). Added for
    # idempotent upserts; not displayed anywhere. See LOG.md.
    source_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # ISO 3166-1 alpha-2 code, added so Step 4's "country code as a primary
    # filter" is possible without re-deriving it from the display name every
    # time. See LOG.md.
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)

    subdivision_name: Mapped[str | None] = mapped_column(String, nullable=True)
    municipality: Mapped[str | None] = mapped_column(String, nullable=True)
    feed_url: Mapped[str | None] = mapped_column(String, nullable=True)
    feed_status: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # --- Step 4 matching columns ---
    is_covered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_to: Mapped[str | None] = mapped_column(String, nullable=True)

    # Mobility Database's own `latest_dataset.downloaded_at` timestamp,
    # captured once here during Step 2 ingestion (which already fetches this
    # field, just didn't store it) rather than re-fetched per-agency during
    # enrichment — avoids ~3,000 redundant API calls. Propagated onto
    # UncoveredAgency.feed_last_updated for uncovered agencies in Step 5.
    # See LOG.md (opportunity-score rework).
    feed_last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TransitCovered(Base):
    """Cities/regions Transit app already covers (Step 3)."""

    __tablename__ = "transit_covered"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_name: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)

    # ISO 3166-1 alpha-2 code, derived from `country` via pycountry at
    # ingestion time. Added for the same reason as MobilityAgency.country_code
    # above — Step 4 needs a code to filter on, not a free-text name. See LOG.md.
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)

    region: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UncoveredAgency(Base):
    """Enriched, scored uncovered agencies (Step 5) — what the API/frontend serve."""

    __tablename__ = "uncovered_agencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mobility_agency_id: Mapped[int] = mapped_column(
        ForeignKey("mobility_agencies.id"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    municipality: Mapped[str | None] = mapped_column(String, nullable=True)
    feed_url: Mapped[str | None] = mapped_column(String, nullable=True)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    # Stored as JSON (a list of missing filenames) rather than a delimited
    # string — Postgres has a native JSON type and it keeps the value
    # machine-readable for the API/frontend. See LOG.md.
    missing_files: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # --- Opportunity-score rework fields (see LOG.md) ---
    has_realtime: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feed_last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    route_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trip_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # "Ready" | "Needs Work" | "Dead Feed" — see compute_readiness() in
    # ingestion/enrichment.py for the exact rule and LOG.md for rationale.
    readiness_status: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # True if this "feed" is very likely a regional/national aggregator or
    # open-data platform (e.g. "DELFI Germany-wide scheduled timetable data",
    # a transport ministry, a multi-operator bundle) rather than a single
    # onboardable transit agency. See is_probable_aggregator() in
    # ingestion/enrichment.py and LOG.md for why this exists.
    is_probable_aggregator: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
