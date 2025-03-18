from typing import Optional
import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from gpustack.config.config import Config
from gpustack.worker.logs import LogOptionsDep
from gpustack.api.exceptions import (
    InternalServerErrorException,
    NotFoundException,
)
from gpustack.schemas.workers import Worker
from gpustack.server.deps import ListParamsDep, SessionDep
from gpustack.schemas.models import (
    ModelInstance,
    ModelInstanceCreate,
    ModelInstancePublic,
    ModelInstanceUpdate,
    ModelInstancesPublic,
)

router = APIRouter()


@router.get("", response_model=ModelInstancesPublic)
async def get_model_instances(
    session: SessionDep,
    params: ListParamsDep,
    model_id: Optional[int] = None,
    worker_id: Optional[int] = None,
    state: Optional[str] = None,
):
    fields = {}
    if model_id:
        fields["model_id"] = model_id

    if worker_id:
        fields["worker_id"] = worker_id

    if state:
        fields["state"] = state

    if params.watch:
        return StreamingResponse(
            ModelInstance.streaming(session, fields=fields),
            media_type="text/event-stream",
        )

    return await ModelInstance.paginated_by_query(
        session=session,
        fields=fields,
        page=params.page,
        per_page=params.perPage,
    )


@router.get("/{id}", response_model=ModelInstancePublic)
async def get_model_instance(session: SessionDep, id: int):
    model_instance = await ModelInstance.one_by_id(session, id)
    if not model_instance:
        raise NotFoundException(message="Model instance not found")
    return model_instance


async def fetch_model_instance(session, id):
    model_instance = await ModelInstance.one_by_id(session, id)
    if not model_instance:
        raise NotFoundException(message="Model instance not found")
    if not model_instance.worker_id:
        raise NotFoundException(message="Model instance not assigned to a worker")
    return model_instance


async def fetch_worker(session, worker_id):
    worker = await Worker.one_by_id(session, worker_id)
    if not worker:
        raise NotFoundException(message="Model instance's worker not found")
    return worker


async def fetch_logs(client, url, timeout):
    try:
        async with client.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise HTTPException(
                    status_code=resp.status,
                    detail=f"Error fetching serving logs: {resp.reason}",
                )
            return await resp.text(), resp.status

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"fetching serving logs: {str(e)}")


@router.get("/{id}/logs")
async def get_serving_logs(
    request: Request, session: SessionDep, id: int, log_options: LogOptionsDep
):
    model_instance = await fetch_model_instance(session, id)
    worker = await fetch_worker(session, model_instance.worker_id)
    server_config: Config = request.app.state.server_config

    model_instance_log_url = f"http://{worker.ip}:{server_config.worker_port}/serveLogs/{model_instance.id}?{log_options.url_encode()}"

    timeout = aiohttp.ClientTimeout(total=5 * 60, sock_connect=5)

    client: aiohttp.ClientSession = request.app.state.http_client

    if log_options.follow:
        try:
            async with client.get(model_instance_log_url, timeout=timeout) as resp:
                status_code = resp.status
        except Exception:
            status_code = 500

        async def proxy_stream():
            try:
                async with client.get(model_instance_log_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise HTTPException(
                            status_code=resp.status,
                            detail=f"Error fetching serving logs: {resp.reason}",
                        )
                    async for chunk in resp.content.iter_any():
                        yield chunk

            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Error fetching serving logs: {e}"
                )

        return StreamingResponse(
            proxy_stream(),
            media_type="application/octet-stream",
            status_code=status_code,
        )
    else:
        content, status_code = await fetch_logs(client, model_instance_log_url, timeout)

        return PlainTextResponse(content=content, status_code=status_code)


@router.post("", response_model=ModelInstancePublic)
async def create_model_instance(
    session: SessionDep, model_instance_in: ModelInstanceCreate
):
    try:
        model_instance = await ModelInstance.create(session, model_instance_in)
    except Exception as e:
        raise InternalServerErrorException(
            message=f"Failed to create model instance: {e}"
        )
    return model_instance


@router.put("/{id}", response_model=ModelInstancePublic)
async def update_model_instance(
    session: SessionDep, id: int, model_instance_in: ModelInstanceUpdate
):
    model_instance = await ModelInstance.one_by_id(session, id)
    if not model_instance:
        raise NotFoundException(message="Model instance not found")

    try:
        await model_instance.update(session, model_instance_in)
    except Exception as e:
        raise InternalServerErrorException(
            message=f"Failed to update model instance: {e}"
        )
    return model_instance


@router.delete("/{id}")
async def delete_model_instance(session: SessionDep, id: int):
    model_instance = await ModelInstance.one_by_id(session, id)
    if not model_instance:
        raise NotFoundException(message="Model instance not found")

    try:
        await model_instance.delete(session)
    except Exception as e:
        raise InternalServerErrorException(
            message=f"Failed to delete model instance: {e}"
        )
