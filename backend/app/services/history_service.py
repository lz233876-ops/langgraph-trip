"""历史行程记录服务: 保存/查询/删除用户的历史旅行计划"""

import json
import logging
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..db.models import TripRecord
from ..models.schemas import TripPlan, TripRequest, TripUsage

logger = logging.getLogger(__name__)


def create_trip_record(
    db: Session,
    request: TripRequest,
    trip_plan: TripPlan,
    usage: Optional[TripUsage] = None,
) -> TripRecord:
    """保存一条旅行计划历史记录 (行程生成成功后调用)"""
    usage = usage or TripUsage()
    record = TripRecord(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        travel_days=request.travel_days,
        transportation=request.transportation,
        accommodation=request.accommodation,
        preferences=json.dumps(request.preferences, ensure_ascii=False),
        free_text_input=request.free_text_input or "",
        plan_json=trip_plan.model_dump_json(),
        input_tokens=usage.token_usage.input_tokens,
        output_tokens=usage.token_usage.output_tokens,
        total_tokens=usage.token_usage.total_tokens,
        llm_calls=usage.llm_calls,
        llm_duration_ms=usage.llm_duration_ms,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(f"💾 历史记录已保存: id={record.id}, 城市={record.city}")
    return record


def list_trip_records(
    db: Session,
    page: int = 1,
    page_size: int = 10,
    city: Optional[str] = None,
):
    """分页查询历史记录 (按创建时间倒序)

    Returns:
        (records, total): 记录列表与总条数
    """
    query = select(TripRecord)
    if city:
        query = query.where(TripRecord.city.contains(city))

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    records = db.scalars(
        query.order_by(desc(TripRecord.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(records), total


def get_trip_record(db: Session, record_id: int) -> Optional[TripRecord]:
    """按 id 查询历史记录"""
    return db.get(TripRecord, record_id)


def update_trip_record(db: Session, record_id: int, trip_plan: TripPlan) -> Optional[TripRecord]:
    """更新历史记录的行程计划 (前端编辑保存后持久化)"""
    record = db.get(TripRecord, record_id)
    if record is None:
        return None
    record.plan_json = trip_plan.model_dump_json()
    db.commit()
    db.refresh(record)
    logger.info(f"✏️  历史记录已更新: id={record_id}")
    return record


def delete_trip_record(db: Session, record_id: int) -> bool:
    """删除历史记录, 返回是否删除成功"""
    record = db.get(TripRecord, record_id)
    if record is None:
        return False
    db.delete(record)
    db.commit()
    logger.info(f"🗑️  历史记录已删除: id={record_id}")
    return True


def trip_record_to_summary(record: TripRecord) -> dict:
    """转列表摘要 (不含完整行程, 减少传输量)"""
    try:
        plan = json.loads(record.plan_json)
    except json.JSONDecodeError:
        plan = {}

    return {
        "id": record.id,
        "city": record.city,
        "start_date": record.start_date,
        "end_date": record.end_date,
        "travel_days": record.travel_days,
        "transportation": record.transportation,
        "accommodation": record.accommodation,
        "preferences": json.loads(record.preferences or "[]"),
        "created_at": record.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "attraction_count": sum(
            len(day.get("attractions", [])) for day in plan.get("days", [])
        ),
        "budget_total": (plan.get("budget") or {}).get("total", 0),
        "total_tokens": record.total_tokens,
        "llm_duration_ms": record.llm_duration_ms,
    }
