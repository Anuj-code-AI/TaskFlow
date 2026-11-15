from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator
from app.models import Role, TaskStatus, Priority

class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class UserOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    model_config = ConfigDict(from_attributes=True)

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None

class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None

class WorkspaceOut(BaseModel):
    id: str
    name: str
    description: str | None
    created_by: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class MemberCreate(BaseModel):
    email: EmailStr
    role: Role = Role.MEMBER

class MemberRoleUpdate(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: EmailStr
    role: Role = Role.MEMBER


class InvitationOut(BaseModel):
    id: str
    workspace_id: str
    email: EmailStr
    role: Role
    status: str
    expires_at: datetime
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class AuditLogOut(BaseModel):
    id: str
    workspace_id: str
    actor_id: str
    actor_name: str
    action: str
    entity_type: str
    entity_id: str
    project_name: str | None = None
    before_data: dict | None
    after_data: dict | None
    created_at: datetime

class MemberOut(BaseModel):
    user_id: str
    email: EmailStr
    name: str
    role: Role

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None

class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None

class ProjectOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    created_by: str
    owner_id: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class TaskCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    assignee_id: str
    priority: Priority = Priority.MEDIUM
    due_at: datetime | None = None
    labels: list[str] = Field(default_factory=list, max_length=20)
    depends_on_task_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, values):
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 40 for value in cleaned):
            raise ValueError("Labels must contain 1 to 40 non-whitespace characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Labels must be unique")
        return cleaned


class TaskUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    priority: Priority | None = None
    due_at: datetime | None = None
    labels: list[str] | None = Field(default=None, max_length=20)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, values):
        if values is None:
            return values
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 40 for value in cleaned):
            raise ValueError("Labels must contain 1 to 40 non-whitespace characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Labels must be unique")
        return cleaned


class TaskAssign(BaseModel):
    expected_version: int = Field(ge=1)
    assignee_id: str | None

class TaskTransition(BaseModel):
    expected_version: int = Field(ge=1)
    status: TaskStatus

class TaskOut(BaseModel):
    id: str
    workspace_id: str
    project_id: str
    title: str
    description: str | None
    labels: list[str]
    creator_id: str
    assignee_id: str | None
    priority: Priority
    status: TaskStatus
    due_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)

class CommentOut(BaseModel):
    id: str
    task_id: str
    author_id: str
    body: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NotificationOut(BaseModel):
    id: str
    workspace_id: str
    event_key: str
    message: str
    read_at: datetime | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DependencyCreate(BaseModel):
    depends_on_task_id: str

class DependencyOut(BaseModel):
    id: str
    task_id: str
    depends_on_task_id: str
    created_by: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class OwnershipTransfer(BaseModel):
    new_owner_user_id: str


class TaskArchive(BaseModel):
    expected_version: int = Field(ge=1)
