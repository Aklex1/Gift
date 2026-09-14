"""Клавиатуры Telegram-бота."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu() -> ReplyKeyboardMarkup:
    """Главное меню владельца."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Кандидаты"), KeyboardButton(text="💼 Портфель")],
            [KeyboardButton(text="📊 PnL"), KeyboardButton(text="⚙️ Стратегии")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🔌 Площадки")],
            [KeyboardButton(text="🛠 Настройки"), KeyboardButton(text="🛑 СТОП")],
        ],
        resize_keyboard=True,
    )


def candidate_actions(candidate_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой кандидата."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Купить", callback_data=f"buy:{candidate_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Пропустить", callback_data=f"skip:{candidate_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧮 Обоснование", callback_data=f"why:{candidate_id}"
                )
            ],
        ]
    )


def confirm_buy(candidate_id: int) -> InlineKeyboardMarkup:
    """Второе подтверждение перед реальным списанием Stars."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, купить", callback_data=f"confirmbuy:{candidate_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="cancel"),
            ]
        ]
    )


def position_actions(position_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой позиции."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏷 Выставить", callback_data=f"list:{position_id}"
                ),
                InlineKeyboardButton(
                    text="🚫 Снять", callback_data=f"unlist:{position_id}"
                ),
            ]
        ]
    )


def strategy_actions(strategy_id: int, enabled: bool) -> InlineKeyboardMarkup:
    """Кнопки управления стратегией."""
    toggle = "⏸ Выключить" if enabled else "▶️ Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle, callback_data=f"strtoggle:{strategy_id}"
                ),
                InlineKeyboardButton(
                    text="💵 Бюджет", callback_data=f"strbudget:{strategy_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎚 Режим", callback_data=f"strmode:{strategy_id}"
                ),
                InlineKeyboardButton(
                    text="📈 Мин. ROI", callback_data=f"strroi:{strategy_id}"
                ),
            ],
        ]
    )


def mode_choice(strategy_id: int) -> InlineKeyboardMarkup:
    """Выбор режима исполнения стратегии."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="SAFE", callback_data=f"setmode:{strategy_id}:safe"
                ),
                InlineKeyboardButton(
                    text="SEMI", callback_data=f"setmode:{strategy_id}:semi"
                ),
                InlineKeyboardButton(
                    text="AUTO", callback_data=f"setmode:{strategy_id}:auto"
                ),
            ]
        ]
    )
