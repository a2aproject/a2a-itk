import asyncio
import json
import logging
import os
from typing import Annotated, Any

logger = logging.getLogger('notifications_app')

from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel


class Notification(BaseModel):
    """Encapsulates default push notification data."""

    event: dict[str, Any]
    token: str | None = None
    headers: dict[str, str] = {}


#: Where a payload of each kind names its task. An A2A 1.x payload is a
#: StreamResponse: one member, named for the kind. `messageUpdate` is the kind
#: A2A 1.1 added.
#:
#: A2A also delivers `artifactUpdate` and `message`, and those are refused.
#: Storing them changes what the conformance test `PUSH-DELIV-003`
#: reads, and that test accepts only a task or a status update, so it has to
#: be corrected in the corpus first.
_TASK_ID_FIELD_V1 = {
    'task': 'id',
    'statusUpdate': 'taskId',
    'messageUpdate': 'taskId',
}


def _extract_task_id_v1(data: dict) -> str | None:
    # Handles A2A 1.x payloads
    for kind, field in _TASK_ID_FIELD_V1.items():
        if isinstance(data.get(kind), dict):
            task_id = data[kind].get(field)
            # Notifications are stored and read by the id as a string.
            return task_id if isinstance(task_id, str) else None
    return None


def _extract_task_id_v03(data: dict) -> str | None:
    # Handles v0.3 task messages
    if data.get('kind') == 'task' and 'id' in data:
        return data['id']
    if data.get('kind') in {'status-update', 'artifact-update'}:
        task_id = data.get('taskId')
        if isinstance(task_id, str) and task_id:
            return task_id
    return None


def create_notifications_app() -> FastAPI:
    """Creates a simple push notification ingesting HTTP+REST application."""
    app = FastAPI()

    log_level = os.environ.get('ITK_LOG_LEVEL', 'INFO').upper()
    logger.setLevel(log_level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    store_lock = asyncio.Lock()
    store: dict[str, list[Notification]] = {}

    @app.post('/notifications')
    async def add_notification(request: Request):
        """Endpoint for ingesting notifications from agents. It receives a JSON
        payload and stores it in-memory.
        """
        # Token is optional for SDK-agnostic server
        token = request.headers.get('x-a2a-notification-token')
        try:
            try:
                json_data = await request.json()
                logger.debug(
                    'Received notification payload: %s', json.dumps(json_data)
                )
            except Exception as e:
                raise HTTPException(
                    status_code=400, detail=f'Invalid JSON: {e}'
                )

            task_id = None
            event_to_store = json_data

            if not isinstance(json_data, dict):
                logger.error(
                    'Notification payload is not a dictionary: %s',
                    json.dumps(json_data),
                )
                raise HTTPException(
                    status_code=400,
                    detail='Notification payload must be a dictionary.',
                )

            task_id = _extract_task_id_v1(json_data)
            if not task_id:
                task_id = _extract_task_id_v03(json_data)

            if not task_id:
                logger.error(
                    'Failed to extract task_id from payload: %s',
                    json.dumps(json_data),
                )
                raise HTTPException(
                    status_code=400,
                    detail='Missing "task_id" in push notification.',
                )

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        async with store_lock:
            if task_id not in store:
                store[task_id] = []
            store[task_id].append(
                Notification(
                    event=event_to_store,
                    token=token,
                    headers={k.lower(): v for k, v in request.headers.items()},
                )
            )
        return {
            'status': 'received',
        }

    @app.get('/notifications')
    async def list_all_notifications():
        """Helper endpoint for retrieving all ingested notifications."""
        result = []
        async with store_lock:
            for task_notifications in store.values():
                result.extend(task_notifications)
        return {'notifications': result}

    @app.get('/{task_id}/notifications')
    async def list_notifications_by_task(
        task_id: Annotated[
            str, Path(title='The ID of the task to list the notifications for.')
        ],
    ):
        """Helper endpoint for retrieving ingested notifications for a given task."""
        async with store_lock:
            notifications = store.get(task_id, [])
        return {'notifications': notifications}

    @app.get('/health')
    def health_check():
        """Helper endpoint for checking if the server is up."""
        return {'status': 'ok'}

    return app
