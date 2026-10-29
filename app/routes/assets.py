import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, AsyncIterator

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from gridfs import AsyncGridFSBucket
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from app.routes.auth import _require_csrf_token, _require_same_origin, _require_session

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_SIZE = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/avif",
}


class ImageAssetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pathname: str = Field(min_length=1, max_length=300)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    size: int = Field(gt=0, le=MAX_IMAGE_SIZE)


def _image_folder(user: dict[str, Any]) -> str:
    login_name = user["email"].split("@", maxsplit=1)[0].lower()
    safe_name = re.sub(r"[^a-z0-9]+", "-", login_name).strip("-") or "user"
    return f"{safe_name[:40]}-{str(user['_id'])[-8:]}"


def _bucket(request: Request) -> AsyncGridFSBucket:
    return AsyncGridFSBucket(request.app.state.database, bucket_name="user_images")


def _asset_response(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(asset["_id"]),
        "filename": asset["filename"],
        "folder": asset["folder"],
        "content_type": asset["content_type"],
        "size": asset["size"],
        "created_at": asset["created_at"],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_image(
    asset: ImageAssetCreate,
    request: Request,
    session_data: Annotated[dict[str, Any], Depends(_require_session)],
) -> dict[str, Any]:
    _require_same_origin(request)
    _require_csrf_token(request, session_data["session"])

    if asset.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG, WebP, GIF, or AVIF image.",
        )

    user = session_data["user"]
    folder = _image_folder(user)
    expected_prefix = f"users/{folder}/"
    if (
        not asset.pathname.startswith(expected_prefix)
        or not re.fullmatch(r"[a-zA-Z0-9._-]+", asset.pathname[len(expected_prefix):])
        or not re.fullmatch(r"[a-zA-Z0-9._-]+", asset.filename)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The image storage details are invalid.",
        )

    document = {
        "owner_id": user["_id"],
        "pathname": asset.pathname,
        "filename": asset.filename,
        "folder": folder,
        "content_type": asset.content_type,
        "size": asset.size,
        "created_at": datetime.now(UTC),
    }
    result = await request.app.state.database.assets.insert_one(document)
    document["_id"] = result.inserted_id
    return _asset_response(document)


@router.get("")
async def list_images(
    request: Request,
    session_data: Annotated[dict[str, Any], Depends(_require_session)],
) -> list[dict[str, Any]]:
    cursor = request.app.state.database.assets.find(
        {"owner_id": session_data["user"]["_id"]}
    ).sort("created_at", -1)
    return [_asset_response(asset) async for asset in cursor]


@router.get("/{asset_id}/metadata")
async def get_image_metadata(asset_id: str, request: Request) -> dict[str, str]:
    if not ObjectId.is_valid(asset_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found.",
        )

    asset = await request.app.state.database.assets.find_one(
        {"_id": ObjectId(asset_id)}
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found.",
        )
    return {
        "pathname": asset["pathname"],
        "content_type": asset["content_type"],
    }


@router.get("/{asset_id}/content")
async def get_legacy_image_content(
    asset_id: str,
    request: Request,
) -> StreamingResponse:
    if not ObjectId.is_valid(asset_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found.",
        )

    try:
        image = await _bucket(request).open_download_stream(ObjectId(asset_id))
    except PyMongoError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found.",
        ) from error

    content_type = (image.metadata or {}).get("content_type")
    if content_type not in ALLOWED_IMAGE_TYPES:
        await image.close()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found.",
        )

    async def stream_image() -> AsyncIterator[bytes]:
        try:
            while chunk := await image.read(64 * 1024):
                yield chunk
        finally:
            await image.close()

    return StreamingResponse(
        stream_image(),
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
