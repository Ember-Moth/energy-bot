"""users 表访问。"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
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
    statement = insert(User).values(id=user_id, first_name=first_name, language_code=language_code)
    user = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[User.id],
            set_={
                "first_name": statement.excluded.first_name,
                "language_code": statement.excluded.language_code,
                "updated_at": func.now(),
            },
        )
        .returning(User)
        .execution_options(populate_existing=True)
    )
    assert user is not None
    return user
