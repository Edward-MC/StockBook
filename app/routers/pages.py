"""Page route — renders the single-page app shell; data is fetched client-side."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    readonly: bool = Query(False),
    hideAmounts: bool = Query(False),
):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "readonly": readonly or config.READONLY,
        "hide_amounts": hideAmounts or config.HIDE_AMOUNTS,
        "auto_refresh": config.AUTO_REFRESH,
    })


@router.get("/entry")
def entry_redirect(request: Request):
    # Old separate route — the entry view is now an in-page tab.
    return RedirectResponse(url="/" + (("?" + str(request.url.query)) if request.url.query else ""))
