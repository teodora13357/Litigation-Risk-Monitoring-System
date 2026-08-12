from typing import Optional

from pydantic import BaseModel, ConfigDict


class CaseBase(BaseModel):
    delivery_time: Optional[str] = None
    case_number: Optional[str] = None
    case_type: Optional[str] = None
    involved_parties: Optional[str] = None
    court_time: Optional[str] = None
    court_location: Optional[str] = None
    contact_phone: Optional[str] = None
    plaintiff: Optional[str] = None
    id_number: Optional[str] = None
    defendant: Optional[str] = None
    service_client: Optional[str] = None
    stage: Optional[str] = None
    client_contact: Optional[str] = None
    processing_status: Optional[str] = None
    handler: Optional[str] = None
    defense_method: Optional[str] = None
    is_closed: Optional[bool] = False
    judgment_result: Optional[str] = None
    compensation_amount: Optional[float] = 0
    case_code: Optional[str] = None
    document_type: Optional[str] = None
    business_type: Optional[str] = None
    standard_cause: Optional[str] = None
    accept_court: Optional[str] = None
    case_status: Optional[str] = None
    cause_description: Optional[str] = None
    key_dates: Optional[str] = None
    remark: Optional[str] = None


class CaseCreate(CaseBase):
    case_number: str


class CaseUpdate(CaseBase):
    pass


class CaseResponse(CaseBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
