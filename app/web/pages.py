from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import segno
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.models import (
    AppUser,
    Business,
    BusinessLocation,
    Challenge,
    ChallengeClaim,
    Customer,
    LoyaltyProgram,
    Product,
    ShopStaff,
    UserRole,
)
from app.web.auth import TTL, read_session, set_session, verify_init_data
from app.web.insights import activity, analytics, games, league, utc
from app.web.loyalty import (
    WEEKDAYS,
    accept_staff_invite,
    add_platform_admin,
    add_product,
    add_promo,
    apply_for_business,
    apply_scan,
    archive_promo,
    business_people,
    can_scan_for_shop,
    client_cards,
    create_earn_ticket,
    create_promo_link,
    create_redeem_ticket,
    create_shop,
    create_staff_invite,
    ensure_customer,
    extra_admin_ids,
    geocode_address,
    guest_payload,
    get_or_create_user,
    inn_digits,
    join_promo_token,
    list_shop_staff,
    live_qr_payload,
    normalize_schedule,
    parse_promo_start,
    parse_staff_start,
    pending_businesses,
    pending_invites,
    program_open,
    platform_admin_ids,
    promo_rule,
    recent_promo_links,
    record_purchase_for_guest,
    remove_platform_admin,
    resolve_promo_link,
    save_shop_location,
    schedule_label,
    set_shop_review,
    shop_directory,
    shop_for_member,
    shop_for_owner,
    shop_history,
    shop_is_live,
    shop_products,
    shop_programs,
    shop_public,
    ticket_payload,
    update_product,
)
from app.web.max_login import tickets

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@dataclass
class CurrentUser:
    max_user_id: int
    display_name: str
    username: str | None
    role: str
    first_name: str
    is_owner: bool = False
    shop_id: str | None = None
    can_stats: bool = False
    can_earn: bool = False
    can_scan: bool = False
    can_edit: bool = False
    owns_shop: bool = False
    is_cashier: bool = False
    is_admin: bool = False
    shop_status: str = ""
    schedule: str = ""
    guest_mode: bool = False

    @property
    def in_business(self) -> bool:
        return self.is_owner or bool(self.shop_id)

    @property
    def pending_shop(self) -> bool:
        return self.owns_shop and self.shop_status == "pending"

    @property
    def guest_view(self) -> bool:
        return (not self.in_business) or self.guest_mode

    @property
    def cabinet_path(self) -> str:
        if self.guest_view:
            return "/me"
        if self.is_owner:
            return "/biz"
        if self.is_cashier:
            if self.can_stats:
                return "/biz"
            if self.can_scan:
                return "/biz/scan"
            if self.can_earn:
                return "/biz/earn"
            return "/me"
        return "/me"


def _pretty_name(name: str) -> str:
    parts = [part for part in name.strip().split() if part]
    if not parts:
        return "Гость"
    return " ".join(part[0].upper() + part[1:] for part in parts)


def _first_name(display: str | None) -> str:
    return _pretty_name(display or "гость").split()[0]


async def _user(request: Request) -> CurrentUser | None:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return None
    uid = read_session(request, request.app.state.settings.webhook_secret)
    if uid is None:
        return None
    async with factory() as session:
        row = await session.scalar(select(AppUser).where(AppUser.max_user_id == uid))
        if row is None:
            return None
        staff = await session.scalar(select(ShopStaff).where(ShopStaff.max_user_id == uid))
        owned = await shop_for_owner(session, uid)
        is_owner = False
        shop_id = None
        can_stats = can_earn = can_scan = can_edit = False
        is_cashier = False
        schedule = ""
        shop_status = owned.status if owned is not None else ""
        if owned is not None and shop_is_live(owned):
            is_owner = True
            can_stats = can_earn = can_scan = can_edit = True
            shop_id = owned.id
        elif staff is not None and staff.kind != "owner":
            staff_shop = await session.get(Business, staff.business_id)
            if staff_shop is not None and shop_is_live(staff_shop):
                is_cashier = True
                can_stats, can_earn, can_scan, can_edit = (
                    staff.can_stats,
                    staff.can_earn,
                    staff.can_scan,
                    staff.can_edit,
                )
                shop_id = staff.business_id
                schedule = schedule_label(staff.schedule_days, staff.shift_from, staff.shift_to)
        return CurrentUser(
            max_user_id=row.max_user_id,
            display_name=row.display_name,
            username=row.username,
            role=row.role,
            first_name=_first_name(row.display_name),
            is_owner=is_owner,
            shop_id=shop_id,
            can_stats=can_stats,
            can_earn=can_earn,
            can_scan=can_scan,
            can_edit=can_edit,
            owns_shop=owned is not None,
            is_cashier=is_cashier,
            is_admin=uid in await platform_admin_ids(session, request.app.state.settings),
            shop_status=shop_status,
            schedule=schedule,
            guest_mode=_guest_mode(request, is_owner=is_owner, is_cashier=is_cashier),
        )


def _qr_svg(payload: str) -> str:
    return segno.make(payload, micro=False).svg_inline(scale=5, border=4, omitsize=True)


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    settings = request.app.state.settings
    data = {
        "title": "Картыч",
        "bot_username": settings.max_bot_username,
        "miniapp_url": settings.miniapp_url,
        "request": request,
        "user": extra.get("user"),
        "tab": extra.get("tab", ""),
        "flash": request.query_params.get("flash", ""),
        "demo_enabled": settings.app_env in {"test", "development"},
    }
    data.update(extra)
    return data


def _guest_mode(request: Request, *, is_owner: bool, is_cashier: bool) -> bool:
    if not (is_owner or is_cashier):
        return False
    path = request.url.path
    if path.startswith("/me"):
        return True
    if path.startswith("/biz") or path.startswith("/admin"):
        return False
    view = request.query_params.get("view") or request.cookies.get("cup_cabinet", "")
    return view == "guest"


def _biz_home(user: CurrentUser) -> str:
    if user.is_owner or user.can_stats:
        return "/biz"
    if user.can_scan:
        return "/biz/scan"
    if user.can_earn:
        return "/biz/earn"
    return "/me"


def _set_cabinet(response: Response, request: Request, value: str) -> None:
    settings = request.app.state.settings
    secure = settings.is_production or request.url.scheme == "https"
    response.set_cookie(
        "cup_cabinet",
        value,
        httponly=True,
        secure=secure,
        samesite="none" if secure else "lax",
        max_age=TTL,
        path="/",
    )


def _flag(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "on", "true", "yes"}


def _coords(latitude: str, longitude: str) -> tuple[float, float] | None:
    lat_raw, lng_raw = latitude.strip(), longitude.strip()
    if not lat_raw and not lng_raw:
        return None
    try:
        lat, lng = float(lat_raw), float(lng_raw)
    except ValueError:
        raise HTTPException(422, "Проверьте координаты")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(422, "Проверьте координаты")
    return lat, lng


async def _notify_admins(request: Request, text: str) -> None:
    client = getattr(request.app.state, "max_client", None)
    factory = getattr(request.app.state, "session_factory", None)
    if client is None:
        return
    ids = set(request.app.state.settings.admin_ids())
    if factory is not None:
        async with factory() as session:
            ids |= await extra_admin_ids(session)
    for uid in ids:
        try:
            await client.send_message(text=text, user_id=uid)
        except Exception:
            continue


def _denied(
    user: CurrentUser | None,
    need: str | None = None,
    *,
    owner_only: bool = False,
) -> RedirectResponse | None:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if owner_only and not user.is_owner:
        if user.pending_shop:
            return RedirectResponse("/biz/apply", status_code=303)
        return RedirectResponse("/settings", status_code=303)
    if not user.in_business:
        if user.pending_shop:
            return RedirectResponse("/biz/apply", status_code=303)
        return RedirectResponse("/me", status_code=303)
    allowed = {
        "stats": user.can_stats,
        "earn": user.can_earn,
        "scan": user.can_scan,
        "edit": user.can_edit,
    }
    if need and not allowed.get(need, False):
        return RedirectResponse(user.cabinet_path, status_code=303)
    return None


def _public_base(request: Request) -> str:
    settings = request.app.state.settings
    base = (settings.public_base_url or "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


@router.get("/")
async def home(request: Request):
    user = await _user(request)
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(request, user=user, title="Картыч", tab="landing"),
    )


@router.get("/qr/{business_id}.png")
async def qr_png(business_id: str, request: Request) -> Response:
    from io import BytesIO

    payload = live_qr_payload(request.app.state.settings.webhook_secret, business_id)
    buffer = BytesIO()
    segno.make(payload, micro=False).save(buffer, kind="png", scale=6, border=2)
    return Response(content=buffer.getvalue(), media_type="image/png")


@router.get("/app")
async def miniapp(request: Request):
    user = await _user(request)
    if user is not None:
        return RedirectResponse(user.cabinet_path, status_code=303)
    return templates.TemplateResponse(
        request,
        "app.html",
        _ctx(request, authed=False, role="none"),
    )


@router.post("/app/auth")
async def miniapp_auth(request: Request) -> JSONResponse:
    body = await request.json()
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    parsed = verify_init_data(str(body.get("init_data") or ""), settings.max_bot_token)
    demo = body.get("demo")
    if (
        parsed is None
        and demo in {"client", "business"}
        and settings.app_env in {"test", "development"}
    ):
        max_user_id = -11 if demo == "client" else -22
        name = "Демо-гость" if demo == "client" else "Демо-бизнес"
        username = "demo_client" if demo == "client" else "demo_shop"
    elif parsed is not None:
        user_info = parsed["user"]
        max_user_id = int(user_info["id"])
        name = " ".join(
            part for part in [user_info.get("first_name"), user_info.get("last_name")] if part
        ).strip()
        username = user_info.get("username")
    else:
        return JSONResponse({"ok": False, "error": "need_max"}, status_code=401)

    async with factory() as session:
        user = await get_or_create_user(session, max_user_id, name or "Гость", username)
        if demo == "business" and user.role == UserRole.NONE.value:
            user.role = UserRole.BUSINESS.value
            if await shop_for_owner(session, user.max_user_id) is None:
                await create_shop(session, user, name="Моя точка", city="Москва", verified=True)
        elif user.role == UserRole.NONE.value:
            user.role = UserRole.CLIENT.value
        start_param = (parsed.get("start_param") or "") if parsed is not None else ""
        token = parse_staff_start(start_param)
        if token:
            await accept_staff_invite(session, user, token)
        promo_id = parse_promo_start(start_param)
        if promo_id:
            await join_promo_token(session, user, promo_id)
        await session.commit()
        role = user.role
        uid = user.max_user_id
    response = JSONResponse({"ok": True, "role": role, "start_param": start_param})
    set_session(
        response,
        settings.webhook_secret,
        uid,
        secure=settings.is_production or request.url.scheme == "https",
    )
    return response


@router.get("/app/session")
async def session_status(request: Request):
    user = await _user(request)
    if user is None:
        return JSONResponse({"ok": False}, status_code=401, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True, "role": user.role}, headers={"Cache-Control": "no-store"})


@router.get("/app/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return templates.TemplateResponse(request, "partials/gate.html", _ctx(request))
    if user.role in {UserRole.NONE.value, ""}:
        factory = request.app.state.session_factory
        async with factory() as session:
            db_user = await session.scalar(
                select(AppUser).where(AppUser.max_user_id == user.max_user_id)
            )
            if db_user is not None:
                db_user.role = UserRole.CLIENT.value
                await session.commit()
                user.role = UserRole.CLIENT.value
        return templates.TemplateResponse(
            request, "partials/cabinet.html", _ctx(request, user=user, tab="client")
        )
    tab = request.query_params.get("tab") or (
        "business" if user.role == UserRole.BUSINESS.value else "client"
    )
    return templates.TemplateResponse(
        request, "partials/cabinet.html", _ctx(request, user=user, tab=tab)
    )


@router.post("/app/role", response_class=HTMLResponse)
async def set_role(request: Request, role: str = Form(...)) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return templates.TemplateResponse(request, "partials/gate.html", _ctx(request))
    if role not in {UserRole.CLIENT.value, UserRole.BUSINESS.value}:
        role = UserRole.CLIENT.value
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        if db_user is not None:
            db_user.role = role
            await session.commit()
            user = CurrentUser(
                db_user.max_user_id,
                db_user.display_name,
                db_user.username,
                db_user.role,
                _first_name(db_user.display_name),
            )
    tab = "business" if role == UserRole.BUSINESS.value else "client"
    return templates.TemplateResponse(
        request, "partials/cabinet.html", _ctx(request, user=user, tab=tab)
    )


@router.get("/app/client", response_class=HTMLResponse)
async def client_home(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return templates.TemplateResponse(request, "partials/gate.html", _ctx(request))
    factory = request.app.state.session_factory
    async with factory() as session:
        cards = await client_cards(session, user.max_user_id)
        shops = await shop_directory(session)
        visited = {card["shop_id"] for card in cards}
        other = [shop for shop in shops if shop.id not in visited]
    return templates.TemplateResponse(
        request,
        "partials/client.html",
        _ctx(
            request,
            user=user,
            cards=cards,
            shops=other,
            flash=request.query_params.get("flash") or "",
        ),
    )


@router.get("/app/business", response_class=HTMLResponse)
async def business_home(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return templates.TemplateResponse(request, "partials/gate.html", _ctx(request))
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        people: list[dict[str, Any]] = []
        program = None
        if shop is not None:
            from app.web.loyalty import active_program

            program = await active_program(session, shop.id)
            people = await business_people(session, shop)
    return templates.TemplateResponse(
        request,
        "partials/business.html",
        _ctx(request, user=user, shop=shop, program=program, people=people),
    )


@router.post("/app/shop", response_class=HTMLResponse)
async def save_shop(
    request: Request,
    name: str = Form(...),
    city: str = Form(""),
    stamps: int = Form(7),
    reward: str = Form("Подарок"),
) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return templates.TemplateResponse(request, "partials/gate.html", _ctx(request))
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        assert db_user is not None
        shop = await create_shop(
            session, db_user, name=name, city=city, stamps=stamps, reward=reward
        )
        await session.commit()
        from app.web.loyalty import active_program

        program = await active_program(session, shop.id)
        people = await business_people(session, shop)
        user = CurrentUser(
            db_user.max_user_id,
            db_user.display_name,
            db_user.username,
            db_user.role,
            _first_name(db_user.display_name),
        )
    return templates.TemplateResponse(
        request,
        "partials/business.html",
        _ctx(request, user=user, shop=shop, program=program, people=people),
    )


@router.get("/app/business/qr", response_class=HTMLResponse)
async def business_qr(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return HTMLResponse("<p class='muted'>Сначала войди в кабинет.</p>")
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
    if shop is None or not shop_is_live(shop):
        return HTMLResponse("<p class='muted'>Сначала подтвердим точку.</p>")
    payload = live_qr_payload(request.app.state.settings.webhook_secret, shop.id)
    return templates.TemplateResponse(
        request,
        "partials/qr.html",
        _ctx(
            request,
            qr_svg=_qr_svg(payload),
            shop=shop,
            updated_at=datetime.now(UTC).strftime("%H:%M:%S"),
            payload=payload,
        ),
    )


@router.post("/app/scan")
async def scan_qr(request: Request) -> JSONResponse:
    user = await _user(request)
    if user is None:
        return JSONResponse(
            {"ok": False, "message": "Сначала открой приложение в MAX."}, status_code=401
        )
    body = await request.json()
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        assert db_user is not None
        result = await apply_scan(
            session,
            user=db_user,
            code=str(body.get("code") or ""),
            secret=request.app.state.settings.webhook_secret,
        )
        await session.commit()
    return JSONResponse(
        {
            "ok": result.ok,
            "message": result.message,
            "visits": result.visits,
            "goal": result.goal,
            "reward": result.reward,
            "shop": result.shop,
            "next": result.next_url,
        }
    )


@router.get("/app/balance", response_class=HTMLResponse)
async def miniapp_balance(request: Request) -> HTMLResponse:
    user = await _user(request)
    visits = 0
    goal = 7
    if user is not None:
        factory = request.app.state.session_factory
        async with factory() as session:
            cards = await client_cards(session, user.max_user_id)
        if cards:
            visits = cards[0]["visits"]
            goal = cards[0]["goal"]
    return templates.TemplateResponse(
        request,
        "partials/balance.html",
        {"visits": visits, "goal": goal, "updated_at": datetime.now(UTC).strftime("%H:%M:%S")},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    try:
        import pandas as pd
        import plotly.express as px
    except ImportError:
        chart_html = '<p class="muted">График появится на полном стеке.</p>'
    else:
        factory = getattr(request.app.state, "session_factory", None)
        rows = [{"day": name, "visits": 0} for name in ("пн", "вт", "ср", "чт", "пт", "сб", "вс")]
        if factory is not None:
            async with factory() as session:
                from sqlalchemy import func

                from app.db.models import Visit

                total = await session.scalar(select(func.count()).select_from(Visit))
                rows[-1]["visits"] = int(total or 0)
        frame = pd.DataFrame(rows)
        fig = px.bar(frame, x="day", y="visits", title="Визиты")
        fig.update_layout(margin={"l": 16, "r": 16, "t": 48, "b": 16}, height=280)
        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx(request, title="Картыч · дашборд", chart_html=chart_html)
    )


@router.get("/shops")
async def shops_page(request: Request):
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/me/shops", status_code=303)


@router.get("/shops/{shop_id}", response_class=HTMLResponse)
async def shop_page(request: Request, shop_id: str) -> HTMLResponse:
    user = await _user(request)
    factory = request.app.state.session_factory
    async with factory() as session:
        data = await shop_public(session, shop_id)
    if data is None:
        return HTMLResponse("Точка не найдена", status_code=404)
    return templates.TemplateResponse(
        request,
        "shop.html",
        _ctx(
            request,
            user=user,
            title=data["shop"].name,
            shop=data["shop"],
            groups=data["groups"],
            programs=data["programs"],
            rules=data["rules"],
        ),
    )


@router.get("/help")
async def help_page(request: Request) -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


@router.get("/login")
async def login_page(request: Request):
    user = await _user(request)
    promo = (request.query_params.get("promo") or "").strip()
    invite = (request.query_params.get("invite") or "").strip()
    if user is not None:
        if promo:
            return RedirectResponse(f"/promo/{promo}", status_code=303)
        return RedirectResponse(user.cabinet_path, status_code=303)
    error = ""
    if request.query_params.get("err") == "expired":
        error = "Сессия входа истекла. Нажми «Войти через MAX» ещё раз."
    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(
            request,
            user=user,
            title="Картыч · вход",
            error=error,
            wait_token="",
            max_url="",
            invite=invite,
            promo=promo,
        ),
    )


@router.post("/login")
async def login_max(
    request: Request,
    invite: str = Form(""),
    promo: str = Form(""),
):
    user = await _user(request)
    if user is not None:
        return RedirectResponse(user.cabinet_path, status_code=303)
    token_invite = invite.strip() if len(invite.strip()) == 16 else ""
    token_promo = promo.strip() if len(promo.strip()) == 16 else ""
    token = tickets.create(role="client", invite=token_invite, promo=token_promo)
    bot = request.app.state.settings.max_bot_username or "t136_hakaton_max_bot"
    max_url = f"https://max.ru/{bot}?start={tickets.payload(token)}"
    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(
            request,
            user=None,
            title="Картыч · вход",
            error="",
            wait_token=token,
            max_url=max_url,
            invite=token_invite,
            promo=token_promo,
        ),
    )


@router.get("/login/status/{token}")
async def login_status(token: str) -> JSONResponse:
    return JSONResponse({"status": tickets.status(token)})


@router.get("/login/complete/{token}")
async def login_complete(request: Request, token: str) -> RedirectResponse:
    item = tickets.consume(token)
    if item is None:
        return RedirectResponse("/login?err=expired", status_code=303)
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    invite = str(item.get("invite") or "")
    promo = str(item.get("promo") or "")
    path = "/me"
    async with factory() as session:
        user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == int(item["user_id"]))
        )
        if user is None:
            user = await get_or_create_user(session, int(item["user_id"]), "Гость MAX", None)
        if user.role == UserRole.NONE.value:
            user.role = UserRole.CLIENT.value
        if invite:
            ok, _msg = await accept_staff_invite(session, user, invite)
            if ok:
                path = "/biz/scan"
        elif promo:
            ok, _msg, _shop = await join_promo_token(session, user, promo)
            if ok:
                path = "/me/qr"
        await session.commit()
    response = RedirectResponse(path, status_code=303)
    set_session(
        response,
        settings.webhook_secret,
        int(item["user_id"]),
        secure=settings.is_production or request.url.scheme == "https",
    )
    return response


async def _sign_in(
    request: Request,
    *,
    name: str,
    role: str,
    user_id: int,
    username: str | None,
) -> RedirectResponse:
    role = role if role in {"client", "business"} else "client"
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    clean = name.strip() or "Гость"
    async with factory() as session:
        user = await get_or_create_user(session, user_id, clean, username)
        user.display_name = clean
        user.role = role
        if (
            role == "business"
            and username in {"demo_shop"}
            and await shop_for_owner(session, user.max_user_id) is None
        ):
            await create_shop(session, user, name="Моя точка", city="Москва", verified=True)
        await session.commit()
    response = RedirectResponse("/biz" if role == "business" else "/me", status_code=303)
    set_session(
        response,
        settings.webhook_secret,
        user_id,
        secure=settings.is_production or request.url.scheme == "https",
    )
    return response


@router.post("/login/demo")
async def login_demo(request: Request, role: str = Form("client")) -> RedirectResponse:
    if request.app.state.settings.app_env not in {"test", "development"}:
        raise HTTPException(404)
    body_role = role if role in {"client", "business"} else "client"
    name = "Демо-гость" if body_role == "client" else "Демо-бизнес"
    username = "demo_client" if body_role == "client" else "demo_shop"
    user_id = -11 if body_role == "client" else -22
    return await _sign_in(request, name=name, role=body_role, user_id=user_id, username=username)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("cup_session", path="/")
    if request.app.state.settings.is_production or request.url.scheme == "https":
        response.headers.append(
            "set-cookie",
            "cup_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=None; Partitioned",
        )
    return response


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    factory = request.app.state.session_factory
    bot = request.app.state.settings.max_bot_username or "t136_hakaton_max_bot"
    base = _public_base(request)
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        location = await session.get(BusinessLocation, shop.id) if shop else None
        cards = await client_cards(session, user.max_user_id)
        events = await activity(session, user_id=user.max_user_id)
        ranking = await league(session, user.max_user_id)
        staff_rows = await list_shop_staff(session, shop) if shop and user.is_owner else []
        invites = await pending_invites(session, shop) if shop and user.is_owner else []
        products = (
            await shop_products(session, shop.id, active_only=False)
            if shop and user.can_edit
            else []
        )
        staff_self = None
        if shop and user.is_cashier:
            staff_self = await session.scalar(
                select(ShopStaff).where(ShopStaff.max_user_id == user.max_user_id)
            )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            user=user,
            title="Профиль",
            shop=shop,
            location=location,
            cards=cards,
            visits=len(events),
            bonus=sum(c["bonus"] for c in cards),
            ranking=ranking,
            staff_rows=staff_rows,
            invites=invites,
            products=products,
            staff_self=staff_self,
            weekdays=WEEKDAYS,
            invite_site=f"{base}/join",
            invite_max=f"https://max.ru/{bot}?start=",
            tab="settings",
            apply_url="/biz/apply",
        ),
    )


@router.post("/settings")
async def settings_save(request: Request) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/settings", status_code=303)


@router.get("/as/guest")
async def as_guest(request: Request) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    response = RedirectResponse("/me", status_code=303)
    _set_cabinet(response, request, "guest")
    return response


@router.get("/as/biz")
async def as_biz(request: Request) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.in_business:
        return RedirectResponse("/biz/apply" if user.owns_shop else "/me", status_code=303)
    response = RedirectResponse(_biz_home(user), status_code=303)
    _set_cabinet(response, request, "biz")
    return response


@router.get("/biz/apply", response_class=HTMLResponse)
async def biz_apply_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.is_owner:
        return RedirectResponse("/biz", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_owner(session, user.max_user_id)
        location = await session.get(BusinessLocation, shop.id) if shop else None
    return templates.TemplateResponse(
        request,
        "biz_apply.html",
        _ctx(
            request,
            user=user,
            title="Открыть точку",
            shop=shop,
            location=location,
            tab="settings",
        ),
    )


@router.post("/biz/geocode")
async def biz_geocode(request: Request) -> JSONResponse:
    user = await _user(request)
    if user is None:
        return JSONResponse({"ok": False, "message": "Сначала войди"}, status_code=401)
    body = await request.json()
    found = await geocode_address(str(body.get("q") or ""))
    if found is None:
        return JSONResponse({"ok": False, "message": "Адрес не найден. Укажи точку на карте."})
    return JSONResponse({"ok": True, **found})


@router.post("/biz/apply")
async def biz_apply_post(
    request: Request,
    name: str = Form(..., min_length=1, max_length=160),
    city: str = Form("", max_length=160),
    address: str = Form("", max_length=200),
    website: str = Form("", max_length=240),
    inn: str = Form(""),
    director_name: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.is_owner:
        return RedirectResponse("/biz", status_code=303)
    coords = _coords(latitude, longitude)
    if coords is None and address.strip():
        found = await geocode_address(" ".join(part for part in [city, address, name] if part.strip()))
        if found is not None:
            coords = (found["lat"], found["lng"])
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        if db_user is None:
            return RedirectResponse("/login", status_code=303)
        shop, state = await apply_for_business(
            session,
            db_user,
            name=name,
            city=city,
            address=address,
            inn=inn,
            director_name=director_name,
            website=website,
            latitude=None if coords is None else coords[0],
            longitude=None if coords is None else coords[1],
        )
        await session.commit()
    if state == "already":
        return RedirectResponse("/biz", status_code=303)
    await _notify_admins(
        request,
        "Новая заявка на точку в Картыч.\n"
        f"«{shop.name}» · {shop.city} {shop.address}\n"
        f"Сайт: {shop.website or 'нет'}\n"
        f"ИНН: {shop.inn or 'нет'} · {shop.director_name or 'без ФИО'}\n"
        f"Проверить: {_public_base(request)}/admin",
    )
    return RedirectResponse(
        "/biz/apply?flash=" + quote("Заявку отправили. Когда подтвердим точку — откроется кабинет бизнеса."),
        status_code=303,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/me", status_code=303)
    factory = request.app.state.session_factory
    pinned = request.app.state.settings.admin_ids()
    async with factory() as session:
        pending = await pending_businesses(session)
        locations = {
            loc.business_id: loc
            for loc in (await session.scalars(select(BusinessLocation))).all()
        }
        extra = await extra_admin_ids(session)
        admins = []
        for uid in sorted(pinned | extra):
            person = await session.scalar(select(AppUser).where(AppUser.max_user_id == uid))
            admins.append(
                {
                    "id": uid,
                    "name": (person.display_name if person else "") or f"MAX {uid}",
                    "pinned": uid in pinned,
                }
            )
    return templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            user=user,
            title="Заявки бизнеса",
            pending=pending,
            locations=locations,
            admins=admins,
            tab="settings",
        ),
    )


@router.post("/admin/admins")
async def admin_add(request: Request, max_user_id: int = Form(...)) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/me", status_code=303)
    factory = request.app.state.session_factory
    pinned = request.app.state.settings.admin_ids()
    async with factory() as session:
        ok, message = await add_platform_admin(
            session, max_user_id, added_by=user.max_user_id, pinned=pinned
        )
        await session.commit()
    return RedirectResponse("/admin?flash=" + quote(message), status_code=303)


@router.post("/admin/admins/{max_user_id}/remove")
async def admin_remove(request: Request, max_user_id: int) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/me", status_code=303)
    factory = request.app.state.session_factory
    pinned = request.app.state.settings.admin_ids()
    async with factory() as session:
        ok, message = await remove_platform_admin(session, max_user_id, pinned=pinned)
        await session.commit()
    return RedirectResponse("/admin?flash=" + quote(message), status_code=303)


@router.post("/admin/{shop_id}/review")
async def admin_review(
    request: Request, shop_id: str, action: str = Form(...)
) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/me", status_code=303)
    approved = action == "approve"
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await session.get(Business, shop_id)
        if shop is None:
            return RedirectResponse("/admin", status_code=303)
        owner = await set_shop_review(session, shop, approved=approved)
        await session.commit()
        name = shop.name
        owner_id = owner.max_user_id if owner is not None else None
    note = (
        f"Точку «{name}» подтвердили. Кабинет бизнеса открыт."
        if approved
        else f"Заявку «{name}» отклонили. Можно отправить новую с сайта."
    )
    client = getattr(request.app.state, "max_client", None)
    if client is not None and owner_id is not None:
        try:
            await client.send_message(text=note, user_id=owner_id)
        except Exception:
            pass
    return RedirectResponse("/admin?flash=" + quote(note), status_code=303)


@router.get("/me", response_class=HTMLResponse)
async def me_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        cards = await client_cards(session, user.max_user_id)
        shops = await shop_directory(session)
        linked = {card["shop_id"] for card in cards}
        other = [shop for shop in shops if shop.id not in linked]
    return templates.TemplateResponse(
        request,
        "me.html",
        _ctx(request, user=user, title="Карты", cards=cards, shops=other, tab="cards"),
    )


@router.get("/me/shops", response_class=HTMLResponse)
async def me_shops_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        shops = await shop_directory(session)
        cards = await client_cards(session, user.max_user_id)
        linked = {card["shop_id"] for card in cards}
        locations = {
            loc.business_id: {"lat": loc.latitude, "lng": loc.longitude}
            for loc in (await session.scalars(select(BusinessLocation))).all()
        }
        markers = [
            {
                "id": s.id,
                "name": s.name,
                "city": s.city,
                "address": s.address,
                "lat": locations[s.id]["lat"],
                "lng": locations[s.id]["lng"],
            }
            for s in shops
            if s.id in locations
        ]
    return templates.TemplateResponse(
        request,
        "me_shops.html",
        _ctx(
            request,
            user=user,
            title="Рядом",
            shops=shops,
            linked=linked,
            markers=markers,
            tab="shops",
        ),
    )


@router.post("/me/join/{shop_id}")
async def join_shop(request: Request, shop_id: str) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        from app.db.models import Business

        shop = await session.get(Business, shop_id)
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        if shop is not None and db_user is not None:
            if not shop_is_live(shop):
                return RedirectResponse(
                    "/me/shops?flash=" + quote("Эта точка ещё не подтверждена"),
                    status_code=303,
                )
            if db_user.role == UserRole.NONE.value:
                db_user.role = UserRole.CLIENT.value
            await ensure_customer(session, shop, db_user)
            await session.commit()
    return RedirectResponse("/me", status_code=303)


@router.get("/promo/{token}", response_class=HTMLResponse)
async def promo_page(request: Request, token: str) -> HTMLResponse:
    user = await _user(request)
    factory = request.app.state.session_factory
    async with factory() as session:
        _link, program, shop = await resolve_promo_link(session, token)
        linked = False
        if user is not None and shop is not None:
            linked = (
                await session.scalar(
                    select(Customer.id).where(
                        Customer.max_user_id == user.max_user_id,
                        Customer.business_id == shop.id,
                    )
                )
                is not None
            )
    if program is None or shop is None:
        return templates.TemplateResponse(
            request,
            "promo.html",
            _ctx(
                request,
                user=user,
                title="Ссылка истекла",
                shop=None,
                program=None,
                rule="",
                linked=False,
                own=False,
                token=token,
                expired=True,
                tab="promo",
            ),
            status_code=404,
        )
    own = bool(user and shop.owner_max_user_id == user.max_user_id)
    return templates.TemplateResponse(
        request,
        "promo.html",
        _ctx(
            request,
            user=user,
            title=program.title,
            shop=shop,
            program=program,
            rule=promo_rule(program),
            linked=linked,
            own=own,
            token=token,
            expired=False,
            tab="promo",
        ),
    )


@router.post("/promo/{token}/join")
async def promo_join(request: Request, token: str) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse(f"/login?promo={token}", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        assert db_user is not None
        ok, message, shop = await join_promo_token(session, db_user, token)
        await session.commit()
    if not ok:
        return RedirectResponse(f"/promo/{token}?flash=" + quote(message), status_code=303)
    if shop and shop.owner_max_user_id == user.max_user_id:
        return RedirectResponse("/biz/promos?flash=" + quote(message), status_code=303)
    return RedirectResponse("/me/qr?flash=" + quote(message), status_code=303)


@router.get("/me/redeem/{shop_id}", response_class=HTMLResponse)
async def redeem_qr_page(request: Request, shop_id: str) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    factory = request.app.state.session_factory
    secret = request.app.state.settings.webhook_secret
    async with factory() as session:
        from app.db.models import Business

        shop = await session.get(Business, shop_id)
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        if shop is None or db_user is None:
            return RedirectResponse("/me", status_code=303)
        ticket = await create_redeem_ticket(session, db_user, shop, 50)
        await session.commit()
    if ticket is None:
        return HTMLResponse("Недостаточно бонусов.", status_code=400)
    payload = ticket_payload(secret, ticket.id)
    return templates.TemplateResponse(
        request,
        "qr_page.html",
        _ctx(
            request,
            user=user,
            title="QR списания",
            heading="QR списания",
            caption=f"Покажи кассиру «{shop.name}». Спишут {ticket.bonus} бонусов.",
            qr_svg=_qr_svg(payload),
            payload=payload,
            back="/me",
        ),
    )


@router.get("/biz", response_class=HTMLResponse)
async def biz_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "stats")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        people = await business_people(session, shop) if shop else []
        history = await shop_history(session, shop) if shop else []
        products = await shop_products(session, shop.id) if shop else []
        programs = await shop_programs(session, shop.id) if shop else []
        period = request.query_params.get("period", "week")
        period = period if period in {"week", "month"} else "week"
        stats = await analytics(session, shop, period)
    return templates.TemplateResponse(
        request,
        "analytics.html",
        _ctx(
            request,
            user=user,
            title="Аналитика",
            stats=stats,
            shop=shop,
            people=people,
            history=history,
            products=products,
            programs=programs,
            payload="",
            qr_svg="",
            tab="analytics",
        ),
    )


@router.get("/biz/promos", response_class=HTMLResponse)
@router.get("/biz/contests", response_class=HTMLResponse)
async def biz_contests_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        programs = (
            list(
                (
                    await session.scalars(
                        select(LoyaltyProgram).where(LoyaltyProgram.business_id == shop.id)
                    )
                ).all()
            )
            if shop
            else []
        )
        links = await recent_promo_links(session, shop) if shop else {}
        challenges = await games(session, user.max_user_id, shop.id) if shop else []
    return templates.TemplateResponse(
        request,
        "biz_contests.html",
        _ctx(
            request,
            user=user,
            title="Акции",
            shop=shop,
            programs=programs,
            challenges=challenges,
            promo_links=links,
            promo_site=f"{_public_base(request)}/promo",
            promo_max=(
                f"https://max.ru/{request.app.state.settings.max_bot_username or 't136_hakaton_max_bot'}?start="
            ),
            tab="promos",
        ),
    )


@router.get("/biz/clients", response_class=HTMLResponse)
async def biz_clients_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "stats")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        stats = await analytics(session, shop, "month")
        people = stats["people"]
    return templates.TemplateResponse(
        request,
        "biz_clients.html",
        _ctx(request, user=user, title="Клиенты", shop=shop, people=people, tab="clients"),
    )


@router.post("/biz/setup")
async def biz_setup(
    request: Request,
    name: str = Form(..., min_length=1, max_length=160),
    city: str = Form("", max_length=160),
    address: str = Form("", max_length=200),
    website: str = Form("", max_length=240),
    latitude: str = Form(""),
    longitude: str = Form(""),
    inn: str = Form(""),
    director_name: str = Form(""),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, owner_only=True)
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        if db_user is not None:
            shop = await shop_for_member(session, user.max_user_id)
            if shop is None:
                return RedirectResponse("/biz/apply", status_code=303)
            shop.name, shop.city, shop.address = name.strip(), city.strip(), address.strip()
            if not shop.name:
                raise HTTPException(422, "Укажите название точки")
            shop.inn = inn_digits(inn)
            shop.director_name = director_name.strip()
            shop.website = website.strip()[:240]
            coords = _coords(latitude, longitude)
            if coords is not None:
                await save_shop_location(session, shop, coords[0], coords[1])
            else:
                loc = await session.get(BusinessLocation, shop.id)
                if loc:
                    await session.delete(loc)
            await session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/biz/product")
async def biz_product(
    request: Request,
    name: str = Form(..., min_length=1, max_length=160),
    group_name: str = Form("Основное", max_length=80),
    price_rub: int = Form(0, ge=0, le=1000000),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is not None:
            await add_product(session, shop, name=name, group_name=group_name, price_rub=price_rub)
            await session.commit()
    return RedirectResponse("/settings?flash=" + quote("Товар добавлен"), status_code=303)


@router.post("/biz/product/{product_id}")
async def biz_product_update(
    request: Request,
    product_id: str,
    name: str = Form(..., min_length=1, max_length=160),
    group_name: str = Form("Основное", max_length=80),
    price_rub: int = Form(0, ge=0, le=1000000),
    action: str = Form("save"),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is not None:
            product = await session.get(Product, product_id)
            if product is not None and product.business_id == shop.id:
                await update_product(
                    session,
                    shop,
                    product_id,
                    name=name,
                    group_name=group_name,
                    price_rub=price_rub,
                    active=(not product.is_active) if action == "hide" else True,
                )
                await session.commit()
    note = "Товар скрыт" if action == "hide" else "Меню обновлено"
    return RedirectResponse("/settings?flash=" + quote(note), status_code=303)


@router.post("/biz/promo")
async def biz_promo(
    request: Request,
    title: str = Form("", max_length=160),
    kind: str = Form("visits", pattern="^(visits|quantity|amount|stamp_card)$"),
    goal: int = Form(5, ge=1, le=1000000),
    stamps: int = Form(0, ge=0, le=1000000),
    qty: int = Form(0, ge=0, le=1000000),
    amount: int = Form(0, ge=0, le=1000000),
    group_name: str = Form("", max_length=80),
    reward: str = Form("Подарок", max_length=160),
    bonus: int = Form(0, ge=0, le=10000),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is not None:
            mark = goal if goal > 0 else 5
            name = title.strip() or reward
            await add_promo(
                session,
                shop,
                kind=kind,
                title=name,
                stamps=stamps or mark,
                qty=qty or mark,
                amount=amount or (mark if kind == "amount" else 0),
                group_name=group_name.strip() or name,
                reward=reward,
                bonus=bonus,
            )
            await session.commit()
    return RedirectResponse("/biz/promos", status_code=303)


@router.get("/biz/earn")
async def biz_earn_form(request: Request) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "earn")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        programs = await shop_programs(session, shop.id) if shop else []
    return templates.TemplateResponse(
        request,
        "earn_form.html",
        _ctx(request, user=user, shop=shop, programs=programs, title="QR для гостя", tab="qr"),
    )


@router.post("/biz/earn", response_class=HTMLResponse)
async def biz_earn_make(
    request: Request,
    program_id: str = Form(""),
    items: str = Form("Покупка", min_length=1, max_length=200),
    qty: int = Form(1, ge=1, le=10000),
    amount_rub: int = Form(0, ge=0, le=1000000),
    place: str = Form("", max_length=200),
) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "earn")
    if denied:
        return denied
    factory = request.app.state.session_factory
    secret = request.app.state.settings.webhook_secret
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is None:
            return RedirectResponse("/biz", status_code=303)
        if program_id:
            program = await session.get(LoyaltyProgram, program_id)
            if program is None or program.business_id != shop.id or not program_open(program):
                raise HTTPException(422, "Выберите активную акцию своей точки")
        ticket = await create_earn_ticket(
            session,
            shop,
            program_id=program_id or None,
            amount_rub=amount_rub,
            qty=qty,
            items=items,
            place=place,
        )
        await session.commit()
    payload = ticket_payload(secret, ticket.id)
    return templates.TemplateResponse(
        request,
        "biz.html",
        _ctx(
            request,
            user=user,
            title="QR",
            shop=shop,
            qr_svg=_qr_svg(payload),
            payload=payload,
            tab="qr",
        ),
    )


@router.get("/biz/scan", response_class=HTMLResponse)
async def biz_scan_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "scan")
    if denied:
        return denied
    return templates.TemplateResponse(
        request,
        "scan.html",
        _ctx(
            request,
            user=user,
            title="Сканер",
            tab="scan",
            flash=request.query_params.get("flash") or "",
        ),
    )


@router.post("/biz/scan")
async def biz_scan_post(request: Request, code: str = Form("")) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "scan")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        assert db_user is not None
        result = await apply_scan(
            session, user=db_user, code=code, secret=request.app.state.settings.webhook_secret
        )
        await session.commit()
    if result.next_url:
        return RedirectResponse(result.next_url, status_code=303)
    return RedirectResponse("/biz/scan?flash=" + quote(result.message), status_code=303)




@router.get("/join/{token}")
async def join_staff(request: Request, token: str) -> RedirectResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse(f"/login?invite={token}", status_code=303)
    factory = request.app.state.session_factory
    async with factory() as session:
        db_user = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        assert db_user is not None
        ok, message = await accept_staff_invite(session, db_user, token)
        await session.commit()
    path = "/biz/scan" if ok else "/settings"
    return RedirectResponse(f"{path}?flash=" + quote(message), status_code=303)


@router.post("/biz/staff/invite")
async def invite_staff(
    request: Request,
    can_stats: str = Form(""),
    can_earn: str = Form(""),
    can_scan: str = Form(""),
    can_edit: str = Form(""),
    shift_from: str = Form("10:00"),
    shift_to: str = Form("22:00"),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, owner_only=True)
    if denied:
        return denied
    form = await request.form()
    days = [str(value) for value in form.getlist("days")]
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is None:
            return RedirectResponse("/settings", status_code=303)
        await create_staff_invite(
            session,
            shop,
            created_by=user.max_user_id,
            can_stats=_flag(can_stats),
            can_earn=_flag(can_earn),
            can_scan=_flag(can_scan)
            or not any(_flag(item) for item in (can_stats, can_earn, can_edit)),
            can_edit=_flag(can_edit),
            schedule_days=",".join(days),
            shift_from=shift_from,
            shift_to=shift_to,
        )
        await session.commit()
    return RedirectResponse("/biz/staff?flash=" + quote("Ссылка для кассира готова"), status_code=303)


@router.get("/biz/staff", response_class=HTMLResponse)
async def biz_staff_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, owner_only=True)
    if denied:
        return denied
    factory = request.app.state.session_factory
    bot = request.app.state.settings.max_bot_username or "t136_hakaton_max_bot"
    base = _public_base(request)
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        staff_rows = await list_shop_staff(session, shop) if shop else []
        invites = await pending_invites(session, shop) if shop else []
    return templates.TemplateResponse(
        request,
        "biz_staff.html",
        _ctx(
            request,
            user=user,
            title="Сотрудники",
            shop=shop,
            staff_rows=staff_rows,
            invites=invites,
            weekdays=WEEKDAYS,
            invite_site=f"{base}/join",
            invite_max=f"https://max.ru/{bot}?start=",
            tab="staff",
        ),
    )


@router.post("/biz/staff/{staff_id}")
async def update_staff(
    request: Request,
    staff_id: str,
    action: str = Form("save"),
    can_stats: str = Form(""),
    can_earn: str = Form(""),
    can_scan: str = Form(""),
    can_edit: str = Form(""),
    shift_from: str = Form("10:00"),
    shift_to: str = Form("22:00"),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, owner_only=True)
    if denied:
        return denied
    form = await request.form()
    days = [str(value) for value in form.getlist("days")]
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        row = await session.get(ShopStaff, staff_id)
        if shop is None or row is None or row.business_id != shop.id or row.kind == "owner":
            return RedirectResponse("/biz/staff", status_code=303)
        if action == "remove":
            await session.delete(row)
        else:
            row.can_stats = _flag(can_stats)
            row.can_earn = _flag(can_earn)
            row.can_scan = _flag(can_scan)
            row.can_edit = _flag(can_edit)
            days_value, start, end = normalize_schedule(days, shift_from, shift_to)
            row.schedule_days = days_value
            row.shift_from = start
            row.shift_to = end
        await session.commit()
    return RedirectResponse("/biz/staff?flash=" + quote("Сотрудники обновлены"), status_code=303)


@router.get("/me/qr", response_class=HTMLResponse)
async def guest_qr_page(request: Request) -> HTMLResponse:
    user = await _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    payload = guest_payload(request.app.state.settings.webhook_secret, user.max_user_id)
    return templates.TemplateResponse(
        request,
        "qr_page.html",
        _ctx(
            request,
            user=user,
            title="Мой QR",
            heading="Мой QR",
            caption="На кассе нажми QR. Кассир считает код и запишет покупку в акцию.",
            qr_svg=_qr_svg(payload),
            payload=payload,
            back="/me",
            tab="qr",
            is_guest=True,
        ),
    )


@router.get("/biz/charge/{guest_id}", response_class=HTMLResponse)
async def charge_form(request: Request, guest_id: int) -> HTMLResponse:
    user = await _user(request)
    denied = _denied(user, "scan")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        guest = await session.scalar(select(AppUser).where(AppUser.max_user_id == guest_id))
        programs = await shop_programs(session, shop.id) if shop else []
    if shop is None or guest is None:
        return RedirectResponse("/biz/scan?flash=" + quote("Гость не найден"), status_code=303)
    return templates.TemplateResponse(
        request,
        "charge_form.html",
        _ctx(
            request,
            user=user,
            shop=shop,
            guest=guest,
            programs=programs,
            title="Записать покупку",
            tab="scan",
        ),
    )


@router.post("/biz/charge/{guest_id}")
async def charge_guest(
    request: Request,
    guest_id: int,
    program_id: str = Form(""),
    items: str = Form("Покупка", min_length=1, max_length=200),
    qty: int = Form(1, ge=1, le=10000),
    amount_rub: int = Form(0, ge=0, le=1000000),
    place: str = Form("", max_length=200),
) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "scan")
    if denied:
        return denied
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        cashier = await session.scalar(
            select(AppUser).where(AppUser.max_user_id == user.max_user_id)
        )
        guest = await session.scalar(select(AppUser).where(AppUser.max_user_id == guest_id))
        if shop is None or cashier is None or guest is None:
            return RedirectResponse("/biz/scan?flash=" + quote("Гость не найден"), status_code=303)
        if not await can_scan_for_shop(session, cashier, shop):
            return RedirectResponse(user.cabinet_path, status_code=303)
        if program_id:
            program = await session.get(LoyaltyProgram, program_id)
            if program is None or program.business_id != shop.id:
                raise HTTPException(422, "Выберите акцию своей точки")
        result = await record_purchase_for_guest(
            session,
            shop=shop,
            guest=guest,
            program_id=program_id or None,
            amount_rub=amount_rub,
            qty=qty,
            items=items,
            place=place,
        )
        await session.commit()
    return RedirectResponse("/biz/scan?flash=" + quote(result.message), status_code=303)


@router.get("/me/league")
async def league_page(request: Request):
    user = await _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    period = request.query_params.get("period", "week")
    period = period if period in {"week", "today", "season"} else "week"
    async with request.app.state.session_factory() as session:
        ranking = await league(session, user.max_user_id, period)
        challenges = await games(session, user.max_user_id)
    return templates.TemplateResponse(
        request,
        "league.html",
        _ctx(
            request, user=user, ranking=ranking, challenges=challenges, title="Лига", tab="league"
        ),
    )


@router.get("/me/cards/{shop_id}")
async def card_page(request: Request, shop_id: str):
    user = await _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    async with request.app.state.session_factory() as session:
        cards = await client_cards(session, user.max_user_id)
        card = next((c for c in cards if c["shop_id"] == shop_id), None)
    if not card:
        raise HTTPException(404, "Карта не найдена")
    return templates.TemplateResponse(
        request, "card.html", _ctx(request, user=user, card=card, title=card["name"], tab="cards")
    )


@router.get("/me/scan")
async def client_scan_page(request: Request):
    user = await _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "scan.html", _ctx(request, user=user, title="Сканер", tab="qr")
    )


@router.post("/me/rewards/{shop_id}/{program_id}")
async def reward_qr_page(request: Request, shop_id: str, program_id: str):
    from app.db.models import Ticket
    from app.web.loyalty import progress_for, promo_goal, reward_redemptions

    user = await _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    async with request.app.state.session_factory() as session:
        customer = await session.scalar(
            select(Customer).where(
                Customer.max_user_id == user.max_user_id, Customer.business_id == shop_id
            )
        )
        program = await session.get(LoyaltyProgram, program_id)
        if not customer or not program or program.business_id != shop_id:
            raise HTTPException(404)
        current = await progress_for(session, customer.id, program)
        redeemed = await reward_redemptions(session, customer.id, program.id)
        if current // promo_goal(program) <= redeemed:
            raise HTTPException(400, "Подарок пока недоступен")
        ticket = Ticket(
            kind="reward",
            business_id=shop_id,
            customer_id=customer.id,
            program_id=program.id,
            max_user_id=user.max_user_id,
        )
        session.add(ticket)
        await session.commit()
        payload = ticket_payload(request.app.state.settings.webhook_secret, ticket.id)
    return templates.TemplateResponse(
        request,
        "qr_page.html",
        _ctx(
            request,
            user=user,
            title="Получить подарок",
            heading="Твой подарок",
            caption=f"Покажи кассиру: {program.reward_title}",
            qr_svg=_qr_svg(payload),
            payload=payload,
            back=f"/me/cards/{shop_id}",
            tab="cards",
            is_reward=True,
        ),
    )


@router.post("/biz/promos/{program_id}/link")
async def make_promo_link(request: Request, program_id: str) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        program = await session.get(LoyaltyProgram, program_id)
        if not shop or not program or program.business_id != shop.id or not program_open(program):
            raise HTTPException(404)
        await create_promo_link(session, shop, program, created_by=user.max_user_id)
        await session.commit()
    return RedirectResponse("/biz/promos?flash=" + quote("Новая ссылка готова"), status_code=303)


@router.post("/biz/promos/{program_id}/toggle")
async def toggle_promo(request: Request, program_id: str):
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        program = await session.get(LoyaltyProgram, program_id)
        if not shop or not program or program.business_id != shop.id:
            raise HTTPException(404)
        if program.archived_at is None:
            program.is_active = not program.is_active
            await session.commit()
    return RedirectResponse("/biz/promos", status_code=303)


@router.post("/biz/promos/{program_id}/delete")
async def delete_promo(request: Request, program_id: str) -> RedirectResponse:
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        program = await session.get(LoyaltyProgram, program_id)
        if not shop or not program or program.business_id != shop.id:
            raise HTTPException(404)
        await archive_promo(session, program)
        await session.commit()
    return RedirectResponse(
        "/biz/promos?flash=" + quote("Акцию сняли. Прогресс гостей сохранён — штампы не сгорают."),
        status_code=303,
    )


@router.get("/biz/games")
async def business_games_page(request: Request):
    return RedirectResponse("/biz/promos#games", status_code=303)


@router.post("/biz/games")
async def create_game(
    request: Request,
    title: str = Form(..., min_length=1, max_length=160),
    goal: int = Form(..., ge=1, le=100),
    bonus: int = Form(..., ge=1, le=10000),
):
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        if not shop:
            raise HTTPException(403)
        if not title.strip():
            raise HTTPException(422, "Введите название задания")
        session.add(
            Challenge(business_id=shop.id, title=title.strip(), goal=goal, reward_bonus=bonus)
        )
        await session.commit()
    return RedirectResponse("/biz/promos#games", status_code=303)


@router.post("/biz/games/{game_id}/toggle")
async def toggle_game(request: Request, game_id: str):
    user = await _user(request)
    denied = _denied(user, "edit")
    if denied:
        return denied
    async with request.app.state.session_factory() as session:
        shop = await shop_for_member(session, user.max_user_id)
        game = await session.get(Challenge, game_id)
        if not shop or not game or game.business_id != shop.id:
            raise HTTPException(404)
        game.is_active = not game.is_active
        await session.commit()
    return RedirectResponse("/biz/promos#games", status_code=303)


@router.post("/me/games/{game_id}/claim")
async def claim_game(request: Request, game_id: str):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    user = await _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    async with request.app.state.session_factory() as session:
        game = await session.get(Challenge, game_id)
        if not game or not game.is_active:
            raise HTTPException(404)
        customer = await session.scalar(
            select(Customer)
            .where(
                Customer.max_user_id == user.max_user_id, Customer.business_id == game.business_id
            )
            .with_for_update()
        )
        if not customer:
            raise HTTPException(400, "Сначала выполните задание")
        exists = await session.scalar(
            select(ChallengeClaim).where(
                ChallengeClaim.challenge_id == game.id, ChallengeClaim.customer_id == customer.id
            )
        )
        if exists:
            return RedirectResponse(
                "/me/league?flash=" + quote("Награда уже получена"), status_code=303
            )
        events = await activity(session, business_id=game.business_id, user_id=user.max_user_id)
        if sum(1 for e in events if e["at"] >= utc(game.created_at)) < game.goal:
            raise HTTPException(400, "Задание ещё не выполнено")
        session.add(ChallengeClaim(challenge_id=game.id, customer_id=customer.id))
        try:
            await session.flush()
            await session.execute(
                update(Customer)
                .where(Customer.id == customer.id)
                .values(bonus=Customer.bonus + game.reward_bonus)
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
    return RedirectResponse(
        "/me/league?flash=" + quote("Бонусы за задание начислены на карту точки"), status_code=303
    )
