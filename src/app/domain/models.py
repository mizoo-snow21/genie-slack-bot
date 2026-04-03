"""Pydantic models for API request/response and internal state."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING_SUBQUESTION = "running_subquestion"
    EVALUATING = "evaluating"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

class ResearchConfig(BaseModel):
    max_steps: int = 6
    max_duration_seconds: int = 300
    max_result_rows_per_query: int = 100
    enable_charts: bool = True

class ResearchRequest(BaseModel):
    question: str
    space_id: Optional[str] = None
    config: ResearchConfig = Field(default_factory=ResearchConfig)

class ResearchJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime

class StepResponse(BaseModel):
    step_id: str
    question: str
    status: StepStatus
    summary: Optional[str] = None
    row_count: Optional[int] = None
    has_chart: bool = False
    chart_url: Optional[str] = None

class ProgressInfo(BaseModel):
    total_steps_planned: int = 0
    steps_completed: int = 0
    current_step: Optional[str] = None
    elapsed_seconds: int = 0

class ResearchStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: Optional[ProgressInfo] = None
    steps: list[StepResponse] = Field(default_factory=list)

class ReportMetadata(BaseModel):
    total_steps: int
    total_duration_seconds: int
    model_used: str

class ReportResponse(BaseModel):
    job_id: str
    report: str
    metadata: ReportMetadata

class CancelResponse(BaseModel):
    job_id: str
    status: JobStatus

class ErrorResponse(BaseModel):
    detail: str
