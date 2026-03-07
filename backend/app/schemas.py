import datetime

from pydantic import BaseModel

from backend.app.enums import EstimateStatus


class HealthResponse(BaseModel):
    status: str


class ContractorBase(BaseModel):
    name: str = ""
    phone: str = ""
    trade: str = ""
    location: str = ""
    hourly_rate: float | None = None
    business_hours: str = ""


class ContractorCreate(ContractorBase):
    user_id: str


class ContractorResponse(ContractorBase):
    id: int
    user_id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class MemoryBase(BaseModel):
    key: str
    value: str
    category: str = "general"


class MemoryCreate(MemoryBase):
    confidence: float = 1.0


class MemoryResponse(MemoryBase):
    confidence: float
    contractor_id: int


class MessageBase(BaseModel):
    direction: str
    body: str = ""


class MessageResponse(MessageBase):
    seq: int
    timestamp: str


class EstimateLineItemBase(BaseModel):
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0


class EstimateBase(BaseModel):
    description: str = ""
    total_amount: float = 0.0
    status: str = EstimateStatus.DRAFT


class EstimateResponse(EstimateBase):
    id: str
    contractor_id: int
    client_id: str | None = None
    pdf_url: str = ""
    storage_path: str = ""
    created_at: str
