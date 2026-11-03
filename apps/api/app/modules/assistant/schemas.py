from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=3000)


class AssistantChatRequest(BaseModel):
    messages: list[AssistantChatMessage] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def latest_message_must_be_user(self) -> "AssistantChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须是用户提问")
        return self


class AssistantCitationRead(BaseModel):
    label: str
    url: str


class AssistantChangeRead(BaseModel):
    field: str
    before: str
    after: str


class AssistantProposalRead(BaseModel):
    id: str
    title: str
    reason: str
    changes: list[AssistantChangeRead]
    expires_at: datetime


class AssistantChatResponse(BaseModel):
    answer: str
    citations: list[AssistantCitationRead] = Field(default_factory=list)
    proposal: AssistantProposalRead | None = None
    model: str


class AssistantConfirmResponse(BaseModel):
    message: str
    target_type: Literal["work", "planned_release"]
    target_id: int


class AssistantProposalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["update_work_metadata", "update_planned_release"]
    target_id: int = Field(gt=0)
    patch: dict[str, object]
    reason: str = Field(min_length=1, max_length=300)


class AssistantModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=5000)
    citation_indexes: list[int] = Field(default_factory=list, max_length=12)
    proposal: AssistantProposalOutput | None = None


class AssistantPlannedReleasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["announced", "discovered", "linked", "released", "cancelled"] | None = None
    release_date: datetime | None = None
    event_code: str | None = Field(default=None, max_length=30)
    booth: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_nonnullable_fields(self) -> "AssistantPlannedReleasePatch":
        if "title" in self.model_fields_set:
            if self.title is None or not self.title.strip():
                raise ValueError("待发布作品标题不能为空")
            self.title = self.title.strip()
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("待发布作品状态不能为空")
        return self
