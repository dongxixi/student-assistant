from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator


class MailConfig(BaseModel):
    provider: Literal['outlook', 'generic'] = 'outlook'
    login_url: str = 'https://outlook.office.com/mail/'
    inbox_url: str = 'https://outlook.office.com/mail/'
    allowed_hosts: list[str] = Field(default_factory=lambda: ['outlook.office.com', 'outlook.office365.com', 'login.microsoftonline.com', 'login.live.com'])
    username_selector: str = 'input[type=email]'
    password_selector: str = 'input[type=password]'
    submit_selector: str = 'input[type=submit]'
    authenticated_selector: str = '[role=listbox][aria-label], [role=tree]'
    message_selector: str = '[role=option][data-convid]'
    body_selector: str = '[role=document]'
    subject_selector: str = '[role=heading]'
    sender_selector: str = ''
    empty_selector: str = ''
    mfa_selector: str = '#idDiv_SAOTCAS_Title, #idDiv_SAOTCS_Proofs, #idTxtBx_SAOTCC_OTC'
    error_selector: str = '#passwordError, #usernameError'
    timezone: str = 'Europe/London'
    interval_minutes: int = Field(default=60, ge=5, le=10080)
    max_messages: int = Field(default=30, ge=1, le=200)
    failure_keep_seconds: int = Field(default=120, ge=0, le=600)
    verification_timeout: int = Field(default=300, ge=30, le=1800)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError):
            raise ValueError('无效的 IANA 时区')
        return value

    @field_validator('login_url', 'inbox_url')
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('邮箱地址必须是 HTTPS，且不能包含账号密码')
        return value


class StudentInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=500)
    config: MailConfig = Field(default_factory=MailConfig)


class DraftInput(BaseModel):
    draft: str = Field(min_length=1, max_length=12000)


class ExtractedItem(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    course_code: str | None = None
    category: Literal['assignment', 'exam', 'attendance', 'grade', 'registration', 'other']
    deadline_local: str | None = None
    timezone: str | None = None
    weight_percent: float | None = Field(default=None, ge=0, le=100)
    required_action: str
    evidence: str = Field(min_length=1)


class Extraction(BaseModel):
    items: list[ExtractedItem] = Field(max_length=30)
