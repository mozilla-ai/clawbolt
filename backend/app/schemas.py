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

    model_config = {"from_attributes": True}


class MemoryBase(BaseModel):
    key: str
    value: str
    category: str = "general"


class MemoryCreate(MemoryBase):
    confidence: float = 1.0
    source_message_id: int | None = None


class MemoryResponse(MemoryBase):
    id: int
    contractor_id: int
    confidence: float
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class MessageBase(BaseModel):
    direction: str
    body: str = ""


class MessageResponse(MessageBase):
    id: int
    conversation_id: int
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


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
    id: int
    contractor_id: int
    client_id: int | None = None
    pdf_url: str = ""
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
