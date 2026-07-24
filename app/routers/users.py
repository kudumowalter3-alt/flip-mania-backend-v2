from fastapi import APIRouter, Depends

from app.models.user import User
from app.routers.deps import get_current_user
from app.schemas.user import UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


# NOTE: Priority 5 (avatar, country, language, level, XP, rank, achievements,
# games played, win streak, favourite mode) is NOT implemented yet. This
# router intentionally only covers identity for now so Priority 1 doesn't
# grow into unrelated scope.
