from fastapi import APIRouter

from app.api.deps import AuthServiceDep
from app.api.schemas import ErrorOut, LoginIn, RegisterIn, TokenOut, UserOut
from app.domain.views import UserView

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=201,
    response_model=UserOut,
    responses={409: {"model": ErrorOut}},
    summary="Register a buyer",
)
async def register(payload: RegisterIn, auth: AuthServiceDep) -> UserView:
    return await auth.register(payload.email, payload.password)


@router.post(
    "/login",
    response_model=TokenOut,
    responses={401: {"model": ErrorOut}},
    summary="Exchange email + password for a JWT",
)
async def login(payload: LoginIn, auth: AuthServiceDep) -> TokenOut:
    issued = await auth.login(payload.email, payload.password)
    return TokenOut(access_token=issued.token, expires_in=issued.expires_in)
