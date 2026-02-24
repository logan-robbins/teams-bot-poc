"""Product specification models for LegionMeet modality parameterization."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class AgentTool(str, Enum):
    """Supported agent tools for this functional phase."""

    NONE = "none"
    CHECKLIST_AGENT = "checklist_agent"


class OutputRouteType(str, Enum):
    """Supported output route types."""

    UI_STREAM = "ui_stream"
    WEBHOOK = "webhook"
    TEAMS_CHAT = "teams_chat"
    TEAMS_DM = "teams_dm"


class ChecklistItemSpec(BaseModel):
    """Checklist item contract."""

    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    keywords: tuple[str, ...] = Field(default_factory=tuple)

    model_config = {"extra": "forbid"}


class ChecklistSpec(BaseModel):
    """Checklist configuration (required)."""

    items: tuple[ChecklistItemSpec, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ChecklistSpec":
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("checklist.items must have unique ids")
        return self

    model_config = {"extra": "forbid"}


class AgentSpec(BaseModel):
    """Agent parameterization controls."""

    prompt_template: str | None = None
    tools: tuple[AgentTool, ...] = Field(default_factory=lambda: (AgentTool.NONE,))
    model: str | None = None
    reasoning_effort: str | None = None

    @model_validator(mode="after")
    def validate_tools(self) -> "AgentSpec":
        if not self.tools:
            raise ValueError("agent.tools must include at least one tool")

        if AgentTool.NONE in self.tools and len(self.tools) > 1:
            raise ValueError("agent.tools cannot mix 'none' with other tools")
        return self

    model_config = {"extra": "forbid"}


class UiSpec(BaseModel):
    """Default UI template content and simulation data."""

    template: str = Field(default="talestral", min_length=1)
    page_title: str = Field(..., min_length=1)
    page_icon: str = Field(..., min_length=1)
    header_title: str = Field(..., min_length=1)
    candidate_name: str = Field(..., min_length=1)
    meeting_url: str = Field(..., min_length=1)
    interview_script: tuple[tuple[str, str], ...] = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


class OutputRouteSpec(BaseModel):
    """Single route declaration in the product spec."""

    id: str = Field(..., min_length=1)
    type: OutputRouteType
    enabled: bool = True
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 5.0

    @model_validator(mode="after")
    def validate_route(self) -> "OutputRouteSpec":
        if self.type == OutputRouteType.WEBHOOK and self.enabled and not self.url:
            raise ValueError("outputs.routes[].url is required for enabled webhook routes")
        return self

    model_config = {"extra": "forbid"}


class OutputsSpec(BaseModel):
    """Output routing configuration."""

    routes: tuple[OutputRouteSpec, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_route_ids(self) -> "OutputsSpec":
        ids = [route.id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError("outputs.routes must have unique ids")
        return self

    model_config = {"extra": "forbid"}


class ProductSpec(BaseModel):
    """Canonical product configuration for one Teams bot stack."""

    product_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    agent: AgentSpec
    checklist: ChecklistSpec
    outputs: OutputsSpec
    ui: UiSpec

    model_config = {"extra": "forbid"}
