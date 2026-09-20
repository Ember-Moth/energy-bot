from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# 按钮文案常量:handler 按文本匹配时引用,避免两处写串了
BTN_STATUS = "📊 状态"
BTN_HELP = "❓ 帮助"

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="请选择操作…",
)
