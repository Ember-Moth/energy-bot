from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 状态"), KeyboardButton(text="❓ 帮助")],
    ],
    resize_keyboard=True,
    input_field_placeholder="请选择操作…",
)
