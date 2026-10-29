import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.routes.auth import _require_csrf_token, _require_same_origin, _require_session

router = APIRouter()
settings = get_settings()

SITE_PAGE_SLUG_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?"


class SiteNavItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=40)
    pageSlug: str = Field(pattern=f"^{SITE_PAGE_SLUG_PATTERN}$")


class SiteBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    type: Literal[
        "container",
        "section",
        "heading",
        "text",
        "image",
        "button",
        "navbar",
        "card",
        "columns",
        "spacer",
        "divider",
        "footer",
        "circle",
        "carousel",
    ]
    text: str = Field(max_length=2000)
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    rotation: int = Field(default=0, ge=-180, le=180)
    width: int = Field(ge=1, le=4000)
    height: int = Field(ge=1, le=10000)
    color: str = Field(pattern=r"^(#[0-9a-fA-F]{6}|transparent)$")
    backgroundColor: str = Field(pattern=r"^(#[0-9a-fA-F]{6}|transparent)$")
    url: str = Field(max_length=2048)
    images: list[str] = Field(default_factory=list, max_length=3)
    navItems: list[SiteNavItem] | None = Field(default=None, max_length=10)

    @field_validator("images")
    @classmethod
    def validate_images(cls, images: list[str]) -> list[str]:
        for image in images:
            if len(image) > 2048:
                raise ValueError("Image URLs must be 2048 characters or fewer.")
            if re.fullmatch(r"/api/images/[a-f0-9]{24}", image, flags=re.IGNORECASE):
                continue
            parsed = urlsplit(image)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Images must use an http(s) URL or a saved image URL.")
        return images


class SitePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    slug: str = Field(pattern=f"^{SITE_PAGE_SLUG_PATTERN}$")
    title: str = Field(min_length=1, max_length=40)
    blocks: list[SiteBlock] = Field(max_length=500)


class SiteDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[SiteBlock] = Field(max_length=500)
    pages: list[SitePage] = Field(default_factory=list, max_length=20)
    page_width: int = Field(ge=320, le=4000)
    page_height: int = Field(ge=400, le=10000)
    canvas_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    animation: Literal["none", "fade", "rise", "zoom"] = "none"

    @model_validator(mode="after")
    def validate_pages_and_navigation(self) -> "SiteDesign":
        if not self.pages:
            return self

        slugs = [page.slug for page in self.pages]
        if len(slugs) != len(set(slugs)):
            raise ValueError("Page slugs must be unique.")
        page_ids = [page.id for page in self.pages]
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("Page IDs must be unique.")
        if "home" not in slugs:
            raise ValueError("A site must include a Home page with slug 'home'.")

        page_slugs = set(slugs)
        for page in self.pages:
            for block in page.blocks:
                for item in block.navItems or []:
                    if item.pageSlug not in page_slugs:
                        raise ValueError(
                            f"Navigation item '{item.label}' links to a page that does not exist."
                        )
        return self


class PublishedSite(SiteDesign):
    slug: str


def _validate_slug(slug: str) -> None:
    if not re.fullmatch(SITE_PAGE_SLUG_PATTERN, slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use a 1-40 character slug with lowercase letters, numbers, and hyphens.",
        )


def _site_response(site: dict[str, Any]) -> PublishedSite:
    return PublishedSite(
        slug=site["slug"],
        blocks=site["blocks"],
        pages=site.get("pages", []),
        page_width=site["page_width"],
        page_height=site["page_height"],
        canvas_color=site["canvas_color"],
        animation=site.get("animation", "none"),
    )


@router.put("/{slug}", response_model=PublishedSite)
async def save_site(
    slug: str,
    design: SiteDesign,
    request: Request,
    session_data: Annotated[dict[str, Any], Depends(_require_session)],
) -> PublishedSite:
    _validate_slug(slug)
    _require_same_origin(request)
    _require_csrf_token(request, session_data["session"])

    collection = request.app.state.database.sites
    await collection.create_index("slug", unique=True)
    owner_id = session_data["user"]["_id"]
    site = {
        "slug": slug,
        "owner_id": owner_id,
        **design.model_dump(),
    }

    existing = await collection.find_one({"slug": slug})
    if existing is not None and existing.get("owner_id") != owner_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This website address is already in use. Choose another slug.",
        )

    try:
        await collection.update_one(
            {"slug": slug, "owner_id": owner_id},
            {"$set": site},
            upsert=True,
        )
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This website address is already in use. Choose another slug.",
        ) from error

    return PublishedSite(slug=slug, **design.model_dump())


@router.get("/{slug}", response_model=PublishedSite)
async def get_site(slug: str, request: Request) -> PublishedSite:
    _validate_slug(slug)
    site = await request.app.state.database.sites.find_one({"slug": slug})
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )
    return _site_response(site)
