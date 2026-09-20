"""users 表访问。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import User


async def upsert_user(
    session: AsyncSession,
    *,
    user_id: int,
    first_name: str,
    language_code: str = "",
) -> User:
    """按 telegram ID 落库;已存在则更新姓名与语言(不动 tron_address 等用户绑定字段)。"""
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=first_name, language_code=language_code)
        session.add(user)
    else:
        user.first_name = first_name
        user.language_code = language_code
    await session.flush()
    return user
