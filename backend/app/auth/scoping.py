from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.models import Client, Contractor, Estimate, Memory


def get_user_contractor(db: Session, user: Contractor, contractor_id: int) -> Contractor:
    """Get a contractor by ID, scoped to the current user. Returns 404 on mismatch."""
    contractor = (
        db.query(Contractor)
        .filter(Contractor.id == contractor_id, Contractor.user_id == user.user_id)
        .first()
    )
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    return contractor


def get_user_client(db: Session, user: Contractor, client_id: int) -> Client:
    """Get a client by ID, scoped to the current user's contractor."""
    client = (
        db.query(Client).filter(Client.id == client_id, Client.contractor_id == user.id).first()
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def get_user_estimate(db: Session, user: Contractor, estimate_id: int) -> Estimate:
    """Get an estimate by ID, scoped to the current user's contractor."""
    estimate = (
        db.query(Estimate)
        .filter(Estimate.id == estimate_id, Estimate.contractor_id == user.id)
        .first()
    )
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return estimate


def get_user_memory(db: Session, user: Contractor, memory_id: int) -> Memory:
    """Get a memory by ID, scoped to the current user's contractor."""
    memory = (
        db.query(Memory).filter(Memory.id == memory_id, Memory.contractor_id == user.id).first()
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory
