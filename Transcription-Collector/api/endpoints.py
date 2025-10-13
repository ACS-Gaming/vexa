import logging
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from pydantic import BaseModel
from sqlalchemy import select, and_, func, distinct, text
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from shared_models.database import get_db
from shared_models.models import User, Meeting, Transcription, MeetingSession, MeetingSummary, ActionItem, MeetingInsight
from shared_models.schemas import (
    HealthResponse,
    MeetingResponse,
    MeetingListResponse,
    TranscriptionResponse,
    TranscriptionWithSummaryResponse,
    Platform,
    TranscriptionSegment,
    MeetingUpdate,
    MeetingCreate,
    MeetingStatus,
    MeetingSummaryResponse,
    MeetingSummaryCreate,
    MeetingSummaryUpdate,
    ActionItemResponse,
    ActionItemCreate,
    ActionItemUpdate,
    MeetingInsightResponse,
    SummaryGenerationRequest,
    SummaryGenerationResponse
)

from config import IMMUTABILITY_THRESHOLD
from filters import TranscriptionFilter
from api.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()
class WsMeetingRef(MeetingCreate):
    """
    Schema for WS subscription meeting reference.
    Inherits validation from MeetingCreate but only platform and native_meeting_id are relevant.
    """
    class Config:
        extra = 'ignore'

class WsAuthorizeSubscribeRequest(BaseModel):
    meetings: List[WsMeetingRef]

class WsAuthorizeSubscribeResponse(BaseModel):
    authorized: List[Dict[str, str]]
    errors: List[str] = []
    user_id: Optional[int] = None  # Include user_id for channel isolation


async def _get_full_transcript_segments(
    internal_meeting_id: int,
    db: AsyncSession,
    redis_c: aioredis.Redis
) -> List[TranscriptionSegment]:
    """
    Core logic to fetch and merge transcript segments from PG and Redis.
    """
    logger.debug(f"[_get_full_transcript_segments] Fetching for meeting ID {internal_meeting_id}")
    
    # 1. Fetch session start times for this meeting
    stmt_sessions = select(MeetingSession).where(MeetingSession.meeting_id == internal_meeting_id)
    result_sessions = await db.execute(stmt_sessions)
    sessions = result_sessions.scalars().all()
    session_times: Dict[str, datetime] = {session.session_uid: session.session_start_time for session in sessions}
    if not session_times:
        logger.warning(f"[_get_full_transcript_segments] No session start times found in DB for meeting {internal_meeting_id}.")

    # 2. Fetch transcript segments from PostgreSQL (immutable segments)
    stmt_transcripts = select(Transcription).where(Transcription.meeting_id == internal_meeting_id)
    result_transcripts = await db.execute(stmt_transcripts)
    db_segments = result_transcripts.scalars().all()

    # 3. Fetch segments from Redis (mutable segments)
    hash_key = f"meeting:{internal_meeting_id}:segments"
    redis_segments_raw = {}
    if redis_c:
        try:
            redis_segments_raw = await redis_c.hgetall(hash_key)
        except Exception as e:
            logger.error(f"[_get_full_transcript_segments] Failed to fetch from Redis hash {hash_key}: {e}", exc_info=True)

    # 4. Calculate absolute times and merge segments
    merged_segments_with_abs_time: Dict[str, Tuple[datetime, TranscriptionSegment]] = {}

    for segment in db_segments:
        key = f"{segment.start_time:.3f}"
        session_uid = segment.session_uid
        session_start = session_times.get(session_uid)
        if session_uid and session_start:
            try:
                if session_start.tzinfo is None:
                    session_start = session_start.replace(tzinfo=timezone.utc)
                absolute_start_time = session_start + timedelta(seconds=segment.start_time)
                absolute_end_time = session_start + timedelta(seconds=segment.end_time)
                segment_obj = TranscriptionSegment(
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    text=segment.text,
                    language=segment.language,
                    speaker=segment.speaker,
                    created_at=segment.created_at,
                    absolute_start_time=absolute_start_time,
                    absolute_end_time=absolute_end_time
                )
                merged_segments_with_abs_time[key] = (absolute_start_time, segment_obj)
            except Exception as calc_err:
                 logger.error(f"[API Meet {internal_meeting_id}] Error calculating absolute time for DB segment {key} (UID: {session_uid}): {calc_err}")
        else:
            logger.warning(f"[API Meet {internal_meeting_id}] Missing session UID ({session_uid}) or start time for DB segment {key}. Cannot calculate absolute time.")

    for start_time_str, segment_json in redis_segments_raw.items():
        try:
            segment_data = json.loads(segment_json)
            session_uid_from_redis = segment_data.get("session_uid")
            potential_session_key = session_uid_from_redis
            if session_uid_from_redis:
                # This logic to strip prefixes is brittle. A better solution would be to store the canonical session_uid.
                # For now, keeping it to match previous behavior.
                prefixes_to_check = [f"{p.value}_" for p in Platform]
                for prefix in prefixes_to_check:
                    if session_uid_from_redis.startswith(prefix):
                        potential_session_key = session_uid_from_redis[len(prefix):]
                        break
            session_start = session_times.get(potential_session_key) 
            if 'end_time' in segment_data and 'text' in segment_data and session_uid_from_redis and session_start:
                if session_start.tzinfo is None:
                    session_start = session_start.replace(tzinfo=timezone.utc)
                relative_start_time = float(start_time_str)
                absolute_start_time = session_start + timedelta(seconds=relative_start_time)
                absolute_end_time = session_start + timedelta(seconds=segment_data['end_time'])
                segment_obj = TranscriptionSegment(
                    start_time=relative_start_time,
                    end_time=segment_data['end_time'],
                    text=segment_data['text'],
                    language=segment_data.get('language'),
                    speaker=segment_data.get('speaker'),
                    absolute_start_time=absolute_start_time,
                    absolute_end_time=absolute_end_time
                )
                merged_segments_with_abs_time[start_time_str] = (absolute_start_time, segment_obj)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.error(f"[_get_full_transcript_segments] Error parsing Redis segment {start_time_str} for meeting {internal_meeting_id}: {e}")

    # 5. Sort based on calculated absolute time and return
    sorted_segment_tuples = sorted(merged_segments_with_abs_time.values(), key=lambda item: item[0])
    segments = [segment_obj for abs_time, segment_obj in sorted_segment_tuples]
    
    # 6. Deduplicate overlapping segments with identical text
    deduped: List[TranscriptionSegment] = []
    for seg in segments:
        if not deduped:
            deduped.append(seg)
            continue

        last = deduped[-1]
        same_text = (seg.text or "").strip() == (last.text or "").strip()
        overlaps = max(seg.start_time, last.start_time) < min(seg.end_time, last.end_time)

        if same_text and overlaps:
            # If current is fully inside last → drop current
            if seg.start_time >= last.start_time and seg.end_time <= last.end_time:
                continue
            # If current fully contains last → replace with current
            if seg.start_time <= last.start_time and seg.end_time >= last.end_time:
                deduped[-1] = seg
                continue

        deduped.append(seg)

    return deduped

@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    """Health check endpoint"""
    redis_status = "healthy"
    db_status = "healthy"
    
    try:
        redis_c = getattr(request.app.state, 'redis_client', None)
        if not redis_c: raise ValueError("Redis client not initialized in app.state")
        await redis_c.ping()
    except Exception as e:
        redis_status = f"unhealthy: {str(e)}"
    
    try:
        await db.execute(text("SELECT 1")) 
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    return HealthResponse(
        status="healthy" if redis_status == "healthy" and db_status == "healthy" else "unhealthy",
        redis=redis_status,
        database=db_status,
        timestamp=datetime.now().isoformat()
    )

@router.get("/meetings", 
            response_model=MeetingListResponse,
            summary="Get list of all meetings for the current user",
            dependencies=[Depends(get_current_user)])
async def get_meetings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a list of all meetings initiated by the authenticated user."""
    stmt = select(Meeting).where(Meeting.user_id == current_user.id).order_by(Meeting.created_at.desc())
    result = await db.execute(stmt)
    meetings = result.scalars().all()
    return MeetingListResponse(meetings=[MeetingResponse.from_orm(m) for m in meetings])
    
@router.get("/transcripts/summary/{native_meeting_id}",
            response_model=TranscriptionWithSummaryResponse,
            summary="Get transcript summary for a specific meeting by native ID",
            dependencies=[Depends(get_current_user)])
async def get_transcript_summary(
    native_meeting_id: str,
    request: Request,  # Added for redis_client access
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieves the transcript and summary for a meeting by native meeting ID.
    Searches across all platforms for the most recent meeting with this ID.
    """
    logger.debug(f"[API] User {current_user.id} requested summary for native meeting ID {native_meeting_id}")
    
    # Find the most recent meeting with this native ID across all platforms
    stmt_meeting = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        logger.warning(f"[API] No meeting found for user {current_user.id}, native ID '{native_meeting_id}'")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for native ID {native_meeting_id}"
        )

    internal_meeting_id = meeting.id
    redis_c = getattr(request.app.state, 'redis_client', None)

    # Get transcript segments
    sorted_segments = await _get_full_transcript_segments(internal_meeting_id, db, redis_c)
    
    # Get summary data
    stmt_summary = select(MeetingSummary).where(
        MeetingSummary.meeting_id == internal_meeting_id
    ).order_by(MeetingSummary.created_at.desc())
    
    result_summary = await db.execute(stmt_summary)
    summary = result_summary.scalars().first()
    
    # Get action items
    stmt_action_items = select(ActionItem).where(
        ActionItem.meeting_id == internal_meeting_id
    ).order_by(ActionItem.created_at.asc())
    
    result_action_items = await db.execute(stmt_action_items)
    action_items = result_action_items.scalars().all()
    
    logger.info(f"[API Meet {internal_meeting_id}] Retrieved {len(sorted_segments)} segments, summary: {'yes' if summary else 'no'}, action items: {len(action_items)}")
    
    # Build response
    meeting_details = MeetingResponse.from_orm(meeting)
    response_data = meeting_details.dict()
    response_data["segments"] = sorted_segments
    
    # Manually construct summary response to avoid lazy loading issues
    if summary:
        summary_response = MeetingSummaryResponse(
            id=summary.id,
            meeting_id=summary.meeting_id,
            summary_data=summary.summary_data or {},
            overview=summary.overview,
            subject_line=summary.subject_line,
            sentiment=summary.sentiment,
            key_points=summary.key_points,
            decisions=summary.decisions,
            questions=summary.questions,
            challenges=summary.challenges,
            participants=summary.participants,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
            action_items=[ActionItemResponse.from_orm(item) for item in action_items],
            insights=None  # We'll handle insights separately if needed
        )
        response_data["summary"] = summary_response
    else:
        response_data["summary"] = None
        
    response_data["action_items"] = [ActionItemResponse.from_orm(item) for item in action_items]
    
    return TranscriptionWithSummaryResponse(**response_data)


@router.post("/transcripts/summary/{native_meeting_id}",
             response_model=SummaryGenerationResponse,
             summary="Generate or fetch meeting summary by native ID",
             dependencies=[Depends(get_current_user)])
async def fetch_transcript_summary(
    native_meeting_id: str,
    generation_request: SummaryGenerationRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Generates a new summary or returns existing summary for a meeting by native ID.
    This endpoint integrates with the summarization service to create structured summaries.
    """
    logger.info(f"[API] User {current_user.id} requesting summary generation for native meeting ID {native_meeting_id}")
    
    # Find the most recent meeting with this native ID
    stmt_meeting = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for native ID {native_meeting_id}"
        )

    internal_meeting_id = meeting.id
    
    # Check if summary already exists
    stmt_existing_summary = select(MeetingSummary).where(
        MeetingSummary.meeting_id == internal_meeting_id
    ).order_by(MeetingSummary.created_at.desc())
    
    result_existing = await db.execute(stmt_existing_summary)
    existing_summary = result_existing.scalars().first()
    
    if existing_summary and not generation_request.force_regenerate:
        logger.info(f"[API] Returning existing summary for meeting {internal_meeting_id}")
        return SummaryGenerationResponse(
            summary=MeetingSummaryResponse.from_orm(existing_summary),
            generated=False,
            message="Existing summary returned. Use force_regenerate=true to create a new one."
        )
    
    # Get transcript data for summarization
    redis_c = getattr(request.app.state, 'redis_client', None)
    sorted_segments = await _get_full_transcript_segments(internal_meeting_id, db, redis_c)
    
    if not sorted_segments:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No transcript data available for this meeting"
        )
    
    # Convert segments to text for summarization
    transcript_text = "\n".join([
        f"[{getattr(segment, 'start_time', 'N/A')}-{getattr(segment, 'end_time', 'N/A')}] {getattr(segment, 'speaker', 'Unknown')}: {getattr(segment, 'text', '')}"
        for segment in sorted_segments
    ])
    
    # Prepare meeting metadata
    meeting_metadata = {
        "meeting_id": str(internal_meeting_id),
        "meeting_date": meeting.start_time.strftime('%Y-%m-%d') if meeting.start_time else None,
        "participants": meeting.data.get('participants', []) if meeting.data else [],
        "platform": meeting.platform,
        "native_meeting_id": meeting.platform_specific_id
    }
    
    try:
        # Import and use summarizer (assuming it's available in the environment)
        from workflows.summarize import Summarizer
        
        summarizer = Summarizer()
        summary_result = summarizer.summarize(transcript_text, meeting_metadata)
        
        if not summary_result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate summary"
            )
        
        # Create or update summary in database
        if existing_summary and generation_request.force_regenerate:
            # Update existing summary
            existing_summary.summary_data = summary_result
            existing_summary.overview = summary_result.get('overview', [])
            existing_summary.subject_line = summary_result.get('subject_line')
            existing_summary.sentiment = summary_result.get('sentiment')
            existing_summary.key_points = summary_result.get('key_points', [])
            existing_summary.decisions = summary_result.get('decisions', [])
            existing_summary.questions = summary_result.get('questions', [])
            existing_summary.challenges = summary_result.get('challenges', [])
            existing_summary.participants = summary_result.get('participants', [])
            existing_summary.updated_at = datetime.utcnow()
            summary_obj = existing_summary
        else:
            # Create new summary
            summary_obj = MeetingSummary(
                meeting_id=internal_meeting_id,
                summary_data=summary_result,
                overview=summary_result.get('overview', []),
                subject_line=summary_result.get('subject_line'),
                sentiment=summary_result.get('sentiment'),
                key_points=summary_result.get('key_points', []),
                decisions=summary_result.get('decisions', []),
                questions=summary_result.get('questions', []),
                challenges=summary_result.get('challenges', []),
                participants=summary_result.get('participants', [])
            )
            db.add(summary_obj)
        
        # Handle action items
        action_items_data = summary_result.get('action_items', [])
        if action_items_data:
            # Remove existing action items if regenerating
            if generation_request.force_regenerate:
                stmt_delete_actions = select(ActionItem).where(ActionItem.meeting_id == internal_meeting_id)
                result_existing_actions = await db.execute(stmt_delete_actions)
                existing_actions = result_existing_actions.scalars().all()
                for action in existing_actions:
                    await db.delete(action)
            
            # Create new action items
            for item_data in action_items_data:
                if isinstance(item_data, dict):
                    action_item = ActionItem(
                        meeting_id=internal_meeting_id,
                        task=item_data.get('task', ''),
                        owner=item_data.get('owner'),
                        due_date=datetime.strptime(item_data['due_date'], '%Y-%m-%d').date() if item_data.get('due_date') else None,
                        description=f"Generated from meeting summary"
                    )
                    db.add(action_item)
        
        # Generate insights if requested
        if generation_request.include_insights:
            word_count = len(transcript_text.split()) if transcript_text else 0
            
            insight_obj = MeetingInsight(
                meeting_id=internal_meeting_id,
                word_count=word_count,
                total_speaking_time=sum([
                    int(getattr(segment, 'end_time', 0)) - int(getattr(segment, 'start_time', 0))
                    for segment in sorted_segments
                    if getattr(segment, 'start_time', None) and getattr(segment, 'end_time', None)
                ]),
                sentiment_scores={"overall": summary_result.get('sentiment', 'neutral')},
                topics_discussed=summary_result.get('key_points', [])
            )
            db.add(insight_obj)
        
        await db.commit()
        await db.refresh(summary_obj)
        
        logger.info(f"[API] Successfully generated summary for meeting {internal_meeting_id}")
        
        return SummaryGenerationResponse(
            summary=MeetingSummaryResponse.from_orm(summary_obj),
            generated=True,
            message="Summary generated successfully"
        )
        
    except ImportError:
        logger.error("[API] Summarizer not available - workflows.summarize module not found")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Summarization service not available"
        )
    except Exception as e:
        logger.error(f"[API] Failed to generate summary: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate summary: {str(e)}"
        )

@router.post("/internal/summaries/{native_meeting_id}",
             summary="[Internal] Save pre-generated summary data",
             include_in_schema=False)
async def save_pre_generated_summary(
    native_meeting_id: str,
    summary_data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Internal endpoint for workflows to save pre-generated summary data.
    This endpoint accepts already-generated summary data and saves it to the database.
    """
    logger.info(f"[Internal API] Saving pre-generated summary for native meeting ID {native_meeting_id}")
    
    # Find the most recent meeting with this native ID
    stmt_meeting = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for native ID {native_meeting_id}"
        )

    internal_meeting_id = meeting.id
    
    # Check if summary already exists
    stmt_existing_summary = select(MeetingSummary).where(
        MeetingSummary.meeting_id == internal_meeting_id
    ).order_by(MeetingSummary.created_at.desc())
    
    result_existing = await db.execute(stmt_existing_summary)
    existing_summary = result_existing.scalars().first()
    
    if existing_summary:
        # Update existing summary
        existing_summary.summary_data = summary_data
        existing_summary.overview = summary_data.get('overview', [])
        existing_summary.key_points = summary_data.get('key_points', [])
        existing_summary.decisions = summary_data.get('decisions', [])
        existing_summary.updated_at = datetime.utcnow()
        summary_obj = existing_summary
    else:
        # Create new summary
        summary_obj = MeetingSummary(
            meeting_id=internal_meeting_id,
            summary_data=summary_data,
            overview=summary_data.get('overview', []),
            key_points=summary_data.get('key_points', []),
            decisions=summary_data.get('decisions', [])
        )
        db.add(summary_obj)
    
    # Handle action items
    action_items_data = summary_data.get('action_items', [])
    if action_items_data:
        # Remove existing action items
        stmt_delete_actions = select(ActionItem).where(ActionItem.meeting_id == internal_meeting_id)
        result_existing_actions = await db.execute(stmt_delete_actions)
        existing_actions = result_existing_actions.scalars().all()
        for action in existing_actions:
            await db.delete(action)
        
        # Create new action items
        for item_data in action_items_data:
            if isinstance(item_data, dict):
                action_item = ActionItem(
                    meeting_id=internal_meeting_id,
                    task=item_data.get('task', ''),
                    owner=item_data.get('owner'),
                    due_date=datetime.strptime(item_data['due_date'], '%Y-%m-%d').date() if item_data.get('due_date') else None,
                    description=f"Generated from meeting summary"
                )
                db.add(action_item)
    
    await db.commit()
    await db.refresh(summary_obj)
    
    logger.info(f"[Internal API] Successfully saved pre-generated summary for meeting {internal_meeting_id}")
    
    return {"message": "Summary saved successfully", "summary_id": summary_obj.id}

@router.get("/transcripts/{platform}/{native_meeting_id}",
            response_model=TranscriptionResponse,
            summary="Get transcript for a specific meeting by platform and native ID",
            dependencies=[Depends(get_current_user)])
async def get_transcript_by_native_id(
    platform: Platform,
    native_meeting_id: str,
    request: Request, # Added for redis_client access
    meeting_id: Optional[int] = Query(None, description="Optional specific database meeting ID. If provided, returns that exact meeting. If not provided, returns the latest meeting for the platform/native_meeting_id combination."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves the meeting details and transcript segments for a meeting specified by its platform and native ID.
    
    Behavior:
    - If meeting_id is provided: Returns the exact meeting with that database ID (must belong to user and match platform/native_meeting_id)
    - If meeting_id is not provided: Returns the latest matching meeting record for the user (backward compatible behavior)
    
    Combines data from both PostgreSQL (immutable segments) and Redis Hashes (mutable segments).
    """
    logger.debug(f"[API] User {current_user.id} requested transcript for {platform.value} / {native_meeting_id}, meeting_id={meeting_id}")
    redis_c = getattr(request.app.state, 'redis_client', None)
    
    if meeting_id is not None:
        # Get specific meeting by database ID
        # Validate it belongs to user and matches platform/native_meeting_id for consistency
        stmt_meeting = select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.user_id == current_user.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id
        )
        logger.debug(f"[API] Looking for specific meeting ID {meeting_id} with platform/native validation")
    else:
        # Get latest meeting by platform/native_meeting_id (default behavior)
        stmt_meeting = select(Meeting).where(
            Meeting.user_id == current_user.id,
            Meeting.platform == platform.value,
            Meeting.platform_specific_id == native_meeting_id
        ).order_by(Meeting.created_at.desc())
        logger.debug(f"[API] Looking for latest meeting for platform/native_id")

    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        if meeting_id is not None:
            logger.warning(f"[API] No meeting found for user {current_user.id}, platform '{platform.value}', native ID '{native_meeting_id}', meeting_id '{meeting_id}'")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting not found for platform {platform.value}, ID {native_meeting_id}, and meeting_id {meeting_id}"
            )
        else:
            logger.warning(f"[API] No meeting found for user {current_user.id}, platform '{platform.value}', native ID '{native_meeting_id}'")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}"
            )

    internal_meeting_id = meeting.id
    logger.debug(f"[API] Found meeting record ID {internal_meeting_id}, fetching segments...")

    sorted_segments = await _get_full_transcript_segments(internal_meeting_id, db, redis_c)
    
    logger.info(f"[API Meet {internal_meeting_id}] Merged and sorted into {len(sorted_segments)} total segments.")
    
    meeting_details = MeetingResponse.from_orm(meeting)
    response_data = meeting_details.dict()
    response_data["segments"] = sorted_segments
    return TranscriptionResponse(**response_data)


@router.post("/ws/authorize-subscribe",
            response_model=WsAuthorizeSubscribeResponse,
            summary="Authorize WS subscription for meetings",
            description="Validates that the authenticated user is allowed to subscribe to the given meetings and that identifiers are valid.",
            dependencies=[Depends(get_current_user)])
async def ws_authorize_subscribe(
    payload: WsAuthorizeSubscribeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    authorized: List[Dict[str, str]] = []
    errors: List[str] = []

    meetings = payload.meetings or []
    if not meetings:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'meetings' must be a non-empty list")

    for idx, meeting_ref in enumerate(meetings):
        platform_value = meeting_ref.platform.value if isinstance(meeting_ref.platform, Platform) else str(meeting_ref.platform)
        native_id = meeting_ref.native_meeting_id

        # Validate platform/native ID format via construct_meeting_url
        try:
            constructed = Platform.construct_meeting_url(platform_value, native_id)
        except Exception:
            constructed = None
        if not constructed:
            errors.append(f"meetings[{idx}] invalid native_meeting_id for platform '{platform_value}'")
            continue

        stmt_meeting = select(Meeting).where(
            Meeting.user_id == current_user.id,
            Meeting.platform == platform_value,
            Meeting.platform_specific_id == native_id
        ).order_by(Meeting.created_at.desc()).limit(1)

        result = await db.execute(stmt_meeting)
        meeting = result.scalars().first()
        if not meeting:
            errors.append(f"meetings[{idx}] not authorized or not found for user")
            continue

        authorized.append({
            "platform": platform_value, 
            "native_id": native_id,
            "user_id": str(current_user.id),
            "meeting_id": str(meeting.id)
        })

    return WsAuthorizeSubscribeResponse(authorized=authorized, errors=errors, user_id=current_user.id)


# --- Summary and Action Items Management Endpoints ---

@router.get("/summaries/{meeting_id}",
            response_model=MeetingSummaryResponse,
            summary="Get summary for a specific meeting by meeting ID",
            dependencies=[Depends(get_current_user)])
async def get_meeting_summary_by_id(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get the most recent summary for a specific meeting ID."""
    # Verify meeting belongs to user
    stmt_meeting = select(Meeting).where(
        Meeting.id == meeting_id,
        Meeting.user_id == current_user.id
    )
    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found"
        )
    
    # Get summary
    stmt_summary = select(MeetingSummary).where(
        MeetingSummary.meeting_id == meeting_id
    ).order_by(MeetingSummary.created_at.desc())
    
    result_summary = await db.execute(stmt_summary)
    summary = result_summary.scalars().first()
    
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No summary found for this meeting"
        )
    
    return MeetingSummaryResponse.from_orm(summary)

@router.put("/summaries/{summary_id}",
            response_model=MeetingSummaryResponse,
            summary="Update a meeting summary",
            dependencies=[Depends(get_current_user)])
async def update_meeting_summary(
    summary_id: int,
    summary_update: MeetingSummaryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update an existing meeting summary."""
    # Get summary and verify ownership
    stmt = select(MeetingSummary).join(Meeting).where(
        MeetingSummary.id == summary_id,
        Meeting.user_id == current_user.id
    )
    result = await db.execute(stmt)
    summary = result.scalars().first()
    
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Summary not found"
        )
    
    # Update fields
    update_data = summary_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(summary, field, value)
    
    summary.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(summary)
    
    return MeetingSummaryResponse.from_orm(summary)

@router.get("/action-items",
            response_model=List[ActionItemResponse],
            summary="Get all action items for the current user",
            dependencies=[Depends(get_current_user)])
async def get_user_action_items(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all action items for the current user, optionally filtered by status."""
    stmt = select(ActionItem).join(Meeting).where(
        Meeting.user_id == current_user.id
    )
    
    if status:
        stmt = stmt.where(ActionItem.status == status)
    
    stmt = stmt.order_by(ActionItem.created_at.desc())
    
    result = await db.execute(stmt)
    action_items = result.scalars().all()
    
    return [ActionItemResponse.from_orm(item) for item in action_items]

@router.get("/meetings/{meeting_id}/action-items",
            response_model=List[ActionItemResponse],
            summary="Get action items for a specific meeting",
            dependencies=[Depends(get_current_user)])
async def get_meeting_action_items(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all action items for a specific meeting."""
    # Verify meeting belongs to user
    stmt_meeting = select(Meeting).where(
        Meeting.id == meeting_id,
        Meeting.user_id == current_user.id
    )
    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found"
        )
    
    # Get action items
    stmt = select(ActionItem).where(
        ActionItem.meeting_id == meeting_id
    ).order_by(ActionItem.created_at.asc())
    
    result = await db.execute(stmt)
    action_items = result.scalars().all()
    
    return [ActionItemResponse.from_orm(item) for item in action_items]

@router.post("/meetings/{meeting_id}/action-items",
             response_model=ActionItemResponse,
             summary="Create a new action item for a meeting",
             dependencies=[Depends(get_current_user)])
async def create_action_item(
    meeting_id: int,
    action_item: ActionItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new action item for a meeting."""
    # Verify meeting belongs to user
    stmt_meeting = select(Meeting).where(
        Meeting.id == meeting_id,
        Meeting.user_id == current_user.id
    )
    result_meeting = await db.execute(stmt_meeting)
    meeting = result_meeting.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found"
        )
    
    # Create action item
    action_item_data = action_item.dict()
    if action_item_data.get('due_date'):
        action_item_data['due_date'] = datetime.strptime(action_item_data['due_date'], '%Y-%m-%d').date()
    
    db_action_item = ActionItem(
        meeting_id=meeting_id,
        **action_item_data
    )
    
    db.add(db_action_item)
    await db.commit()
    await db.refresh(db_action_item)
    
    return ActionItemResponse.from_orm(db_action_item)

@router.put("/action-items/{action_item_id}",
            response_model=ActionItemResponse,
            summary="Update an action item",
            dependencies=[Depends(get_current_user)])
async def update_action_item(
    action_item_id: int,
    action_item_update: ActionItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update an existing action item."""
    # Get action item and verify ownership
    stmt = select(ActionItem).join(Meeting).where(
        ActionItem.id == action_item_id,
        Meeting.user_id == current_user.id
    )
    result = await db.execute(stmt)
    action_item = result.scalars().first()
    
    if not action_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action item not found"
        )
    
    # Update fields
    update_data = action_item_update.dict(exclude_unset=True)
    
    # Handle date conversion
    if 'due_date' in update_data and update_data['due_date']:
        update_data['due_date'] = datetime.strptime(update_data['due_date'], '%Y-%m-%d').date()
    
    # Handle completed status
    if update_data.get('status') == 'completed' and not action_item.completed_at:
        update_data['completed_at'] = datetime.utcnow()
    elif update_data.get('status') != 'completed' and action_item.completed_at:
        update_data['completed_at'] = None
    
    for field, value in update_data.items():
        setattr(action_item, field, value)
    
    action_item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(action_item)
    
    return ActionItemResponse.from_orm(action_item)

@router.delete("/action-items/{action_item_id}",
              summary="Delete an action item",
              dependencies=[Depends(get_current_user)])
async def delete_action_item(
    action_item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete an action item."""
    # Get action item and verify ownership
    stmt = select(ActionItem).join(Meeting).where(
        ActionItem.id == action_item_id,
        Meeting.user_id == current_user.id
    )
    result = await db.execute(stmt)
    action_item = result.scalars().first()
    
    if not action_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action item not found"
        )
    
    await db.delete(action_item)
    await db.commit()
    
    return {"message": "Action item deleted successfully"}


@router.get("/internal/transcripts/{meeting_id}",
            response_model=List[TranscriptionSegment],
            summary="[Internal] Get all transcript segments for a meeting",
            include_in_schema=False)
async def get_transcript_internal(
    meeting_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Internal endpoint for services to fetch all transcript segments for a given meeting ID."""
    logger.debug(f"[Internal API] Transcript segments requested for meeting {meeting_id}")
    redis_c = getattr(request.app.state, 'redis_client', None)
    
    meeting = await db.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting with ID {meeting_id} not found."
        )
        
    segments = await _get_full_transcript_segments(meeting_id, db, redis_c)
    return segments

@router.patch("/meetings/{platform}/{native_meeting_id}",
             response_model=MeetingResponse,
             summary="Update meeting data by platform and native ID",
             dependencies=[Depends(get_current_user)])
async def update_meeting_data(
    platform: Platform,
    native_meeting_id: str,
    meeting_update: MeetingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Updates the user-editable data (name, participants, languages, notes) for the latest meeting matching the platform and native ID."""
    
    logger.info(f"[API] User {current_user.id} updating meeting {platform.value}/{native_meeting_id}")
    logger.debug(f"[API] Raw meeting_update object: {meeting_update}")
    logger.debug(f"[API] meeting_update.data type: {type(meeting_update.data)}")
    logger.debug(f"[API] meeting_update.data content: {meeting_update.data}")
    
    stmt = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform == platform.value,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())
    
    result = await db.execute(stmt)
    meeting = result.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}"
        )
        
    # Extract update data from the MeetingDataUpdate object
    try:
        if hasattr(meeting_update.data, 'dict'):
            # meeting_update.data is a MeetingDataUpdate pydantic object
            update_data = meeting_update.data.dict(exclude_unset=True)
            logger.debug(f"[API] Extracted update_data via .dict(): {update_data}")
        else:
            # Fallback: meeting_update.data is already a dict
            update_data = meeting_update.data
            logger.debug(f"[API] Using update_data as dict: {update_data}")
    except AttributeError:
        # Handle case where data might be parsed differently
        update_data = meeting_update.data
        logger.debug(f"[API] Fallback update_data: {update_data}")
    
    # Remove None values from update_data
    update_data = {k: v for k, v in update_data.items() if v is not None}
    logger.debug(f"[API] Final update_data after filtering None values: {update_data}")
    
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No data provided for update."
        )
        
    if meeting.data is None:
        meeting.data = {}
        logger.debug(f"[API] Initialized empty meeting.data")
        
    logger.debug(f"[API] Current meeting.data before update: {meeting.data}")
        
    # Only allow updating restricted fields: name, participants, languages, notes
    allowed_fields = {'name', 'participants', 'languages', 'notes'}
    updated_fields = []
    
    # Create a new copy of the data dict to ensure SQLAlchemy detects the change
    new_data = dict(meeting.data) if meeting.data else {}
    
    for key, value in update_data.items():
        if key in allowed_fields and value is not None:
            new_data[key] = value
            updated_fields.append(f"{key}={value}")
            logger.debug(f"[API] Updated field {key} = {value}")
        else:
            logger.debug(f"[API] Skipped field {key} (not in allowed_fields or value is None)")
    
    # Assign the new dict to ensure SQLAlchemy detects the change
    meeting.data = new_data
    
    # Mark the field as modified to ensure SQLAlchemy detects the change
    from sqlalchemy.orm import attributes
    attributes.flag_modified(meeting, "data")
    
    logger.info(f"[API] Updated fields: {', '.join(updated_fields) if updated_fields else 'none'}")
    logger.debug(f"[API] Final meeting.data after update: {meeting.data}")

    await db.commit()
    await db.refresh(meeting)
    
    logger.debug(f"[API] Meeting.data after commit and refresh: {meeting.data}")
    
    return MeetingResponse.from_orm(meeting)

@router.delete("/meetings/{platform}/{native_meeting_id}",
              summary="Delete meeting transcripts and anonymize meeting data",
              dependencies=[Depends(get_current_user)])
async def delete_meeting(
    platform: Platform,
    native_meeting_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Purges transcripts and anonymizes meeting data for finalized meetings.
    
    Only allows deletion for meetings in finalized states (completed, failed).
    Deletes all transcripts but preserves meeting and session records for telemetry.
    Scrubs PII from meeting record while keeping telemetry data.
    """
    
    stmt = select(Meeting).where(
        Meeting.user_id == current_user.id,
        Meeting.platform == platform.value,
        Meeting.platform_specific_id == native_meeting_id
    ).order_by(Meeting.created_at.desc())
    
    result = await db.execute(stmt)
    meeting = result.scalars().first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting not found for platform {platform.value} and ID {native_meeting_id}"
        )
    
    internal_meeting_id = meeting.id
    
    # Check if already redacted (idempotency)
    if meeting.data and meeting.data.get('redacted'):
        logger.info(f"[API] Meeting {internal_meeting_id} already redacted, returning success")
        return {"message": f"Meeting {platform.value}/{native_meeting_id} transcripts already deleted and data anonymized"}
    
    # Check if meeting is in finalized state
    finalized_states = {MeetingStatus.COMPLETED.value, MeetingStatus.FAILED.value}
    if meeting.status not in finalized_states:
        logger.warning(f"[API] User {current_user.id} attempted to delete non-finalized meeting {internal_meeting_id} (status: {meeting.status})")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Meeting not finalized; cannot delete transcripts. Current status: {meeting.status}"
        )
    
    logger.info(f"[API] User {current_user.id} purging transcripts and anonymizing meeting {internal_meeting_id}")
    
    # Delete transcripts from PostgreSQL
    stmt_transcripts = select(Transcription).where(Transcription.meeting_id == internal_meeting_id)
    result_transcripts = await db.execute(stmt_transcripts)
    transcripts = result_transcripts.scalars().all()
    
    for transcript in transcripts:
        await db.delete(transcript)
    
    # Delete transcript segments from Redis and remove from active meetings
    redis_c = getattr(request.app.state, 'redis_client', None)
    if redis_c:
        try:
            hash_key = f"meeting:{internal_meeting_id}:segments"
            # Use pipeline for atomic operations
            async with redis_c.pipeline(transaction=True) as pipe:
                pipe.delete(hash_key)
                pipe.srem("active_meetings", str(internal_meeting_id))
                results = await pipe.execute()
            logger.debug(f"[API] Deleted Redis hash {hash_key} and removed from active_meetings")
        except Exception as e:
            logger.error(f"[API] Failed to delete Redis data for meeting {internal_meeting_id}: {e}")
    
    # Scrub PII from meeting record while preserving telemetry
    original_data = meeting.data or {}
    
    # Keep only telemetry fields
    telemetry_fields = {'status_transition', 'completion_reason', 'error', 'diagnostics'}
    scrubbed_data = {k: v for k, v in original_data.items() if k in telemetry_fields}
    
    # Add redaction marker for idempotency
    scrubbed_data['redacted'] = True
    
    # Update meeting record with scrubbed data
    meeting.platform_specific_id = None  # Clear native meeting ID (this makes constructed_meeting_url return None)
    meeting.data = scrubbed_data
    
    # Note: We keep Meeting and MeetingSession records for telemetry
    await db.commit()
    
    logger.info(f"[API] Successfully purged transcripts and anonymized meeting {internal_meeting_id}")
    
    return {"message": f"Meeting {platform.value}/{native_meeting_id} transcripts deleted and data anonymized"} 