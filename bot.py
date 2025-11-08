import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os
import calendar

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile
)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных среды. Установите его командой: export BOT_TOKEN='ваш_токен'")

OFFICE_MAP_PATH = "office_map.png"
TOTAL_PLACES = 13

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Стабильные брони (место: список дней недели, 0=понедельник)
PERMANENT_BOOKINGS = {
    7: [1, 3]  # Место №7 забронировано по вторникам (1) и четвергам (3)
}


# FSM States
class BookingStates(StatesGroup):
    waiting_for_date = State()
    waiting_for_place = State()
    confirming_booking = State()


class CancelStates(StatesGroup):
    selecting_booking = State()


class ChangeStates(StatesGroup):
    selecting_booking = State()
    waiting_for_new_date = State()
    waiting_for_new_place = State()
    confirming_change = State()


# База данных
class Database:
    def __init__(self, db_path: str = "office_booking.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS places (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    place_id INTEGER NOT NULL,
                    booking_date DATE NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (place_id) REFERENCES places(id)
                )
            """)
            
            cursor.execute("SELECT COUNT(*) FROM places")
            if cursor.fetchone()[0] == 0:
                for i in range(1, TOTAL_PLACES + 1):
                    cursor.execute(
                        "INSERT INTO places (id, name, description) VALUES (?, ?, ?)",
                        (i, f"Место №{i}", f"Рабочее место номер {i}")
                    )
            
            conn.commit()

    def add_user(self, telegram_id: int, username: str, first_name: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (telegram_id, username, first_name)
                VALUES (?, ?, ?)
            """, (telegram_id, username or "", first_name or ""))
            conn.commit()

    def has_user_booking_on_date(self, user_id: int, date: str) -> bool:
        """Проверить, есть ли у пользователя бронь на эту дату"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE user_id = ? AND booking_date = ? AND status = 'active'
            """, (user_id, date))
            return cursor.fetchone()[0] > 0

    def get_available_places(self, date: str) -> List[int]:
        weekday = datetime.strptime(date, "%d.%m.%Y").weekday()
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT place_id FROM bookings
                WHERE booking_date = ? AND status = 'active'
            """, (date,))
            booked = [row[0] for row in cursor.fetchall()]
        
        available = []
        for place_id in range(1, TOTAL_PLACES + 1):
            if place_id in PERMANENT_BOOKINGS:
                if weekday in PERMANENT_BOOKINGS[place_id]:
                    continue
            
            if place_id not in booked:
                available.append(place_id)
        
        return available

    def create_booking(self, user_id: int, place_id: int, date: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Проверить, есть ли уже бронь у пользователя на эту дату
            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE user_id = ? AND booking_date = ? AND status = 'active'
            """, (user_id, date))
            
            if cursor.fetchone()[0] > 0:
                return False  # У пользователя уже есть бронь на эту дату
            
            # Проверить, не занято ли уже это место на эту дату
            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE place_id = ? AND booking_date = ? AND status = 'active'
            """, (place_id, date))
            
            if cursor.fetchone()[0] > 0:
                return False  # Место уже занято
            
            try:
                cursor.execute("""
                    INSERT INTO bookings (user_id, place_id, booking_date, status)
                    VALUES (?, ?, ?, 'active')
                """, (user_id, place_id, date))
                conn.commit()
                return True
            except:
                return False

    def get_user_bookings(self, user_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT b.id, b.place_id, b.booking_date, p.name
                FROM bookings b
                JOIN places p ON b.place_id = p.id
                WHERE b.user_id = ? AND b.status = 'active'
                ORDER BY b.booking_date
            """, (user_id,))
            
            bookings = []
            for row in cursor.fetchall():
                bookings.append({
                    'id': row[0],
                    'place_id': row[1],
                    'date': row[2],
                    'place_name': row[3]
                })
            return bookings

    def cancel_booking(self, booking_id: int, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE bookings
                SET status = 'cancelled'
                WHERE id = ? AND user_id = ? AND status = 'active'
            """, (booking_id, user_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_booking_by_id(self, booking_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT b.id, b.user_id, b.place_id, b.booking_date, p.name
                FROM bookings b
                JOIN places p ON b.place_id = p.id
                WHERE b.id = ? AND b.status = 'active'
            """, (booking_id,))
            
            row = cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'user_id': row[1],
                    'place_id': row[2],
                    'date': row[3],
                    'place_name': row[4]
                }
            return None


# Инициализация
db = Database()
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()


# Клавиатуры
def get_main_menu():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🪑 Забронировать место")],
            [KeyboardButton(text="📅 Мои брони")],
            [KeyboardButton(text="❌ Отменить бронь")],
            [KeyboardButton(text="🔁 Поменять бронь")]
        ],
        resize_keyboard=True
    )
    return keyboard


def get_places_keyboard(available_places: List[int]) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    
    for i, place_id in enumerate(available_places):
        row.append(InlineKeyboardButton(
            text=f"{place_id}️⃣ Место №{place_id}",
            callback_data=f"select_place:{place_id}"
        ))
        
        if len(row) == 2 or i == len(available_places) - 1:
            buttons.append(row)
            row = []
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ ОК", callback_data="confirm_yes"),
            InlineKeyboardButton(text="🔁 Поменять", callback_data="confirm_change"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="confirm_cancel")
        ]
    ])
    return keyboard


def get_bookings_keyboard(bookings: List[Dict]) -> InlineKeyboardMarkup:
    buttons = []
    for booking in bookings:
        buttons.append([InlineKeyboardButton(
            text=f"{booking['place_name']} - {booking['date']}",
            callback_data=f"booking:{booking['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """Создать календарь для выбора даты"""
    buttons = []
    
    # Заголовок с месяцем и годом
    month_name = calendar.month_name[month]
    buttons.append([InlineKeyboardButton(
        text=f"📅 {month_name} {year}",
        callback_data="ignore"
    )])
    
    # Дни недели
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([InlineKeyboardButton(text=day, callback_data="ignore") for day in week_days])
    
    # Получить календарь месяца
    month_calendar = calendar.monthcalendar(year, month)
    today = datetime.now().date()
    
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                # Пустая ячейка
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date = datetime(year, month, day).date()
                
                # Проверка, что дата не в прошлом
                if date < today:
                    row.append(InlineKeyboardButton(text="·", callback_data="ignore"))
                else:
                    date_str = date.strftime("%d.%m.%Y")
                    row.append(InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"date:{date_str}"
                    ))
        buttons.append(row)
    
    # Навигация по месяцам
    nav_row = []
    
    # Предыдущий месяц
    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1
    
    # Только если предыдущий месяц не в прошлом
    if datetime(prev_year, prev_month, 1).date() >= datetime(today.year, today.month, 1).date():
        nav_row.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"calendar:{prev_year}:{prev_month}"
        ))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
    
    nav_row.append(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_calendar"))
    
    # Следующий месяц
    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1
    
    nav_row.append(InlineKeyboardButton(
        text="▶️",
        callback_data=f"calendar:{next_year}:{next_month}"
    ))
    
    buttons.append(nav_row)
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Обработчики команд
@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name)
    
    await message.answer(
        f"Привет, {user.first_name}! 👋\n\n"
        "Я помогу тебе забронировать рабочее место в офисе.\n"
        "Выбери нужное действие:",
        reply_markup=get_main_menu()
    )


@router.message(F.text == "🪑 Забронировать место")
async def start_booking(message: Message, state: FSMContext):
    now = datetime.now()
    await message.answer(
        "Выберите дату бронирования:",
        reply_markup=get_calendar_keyboard(now.year, now.month)
    )
    await state.set_state(BookingStates.waiting_for_date)


@router.message(BookingStates.waiting_for_date)
async def process_date_text(message: Message, state: FSMContext):
    """На случай если пользователь введёт дату текстом вместо календаря"""
    await message.answer(
        "Пожалуйста, используйте календарь выше для выбора даты 📅"
    )


# Обработчики callback
@router.callback_query(F.data.startswith("calendar:"))
async def process_calendar_navigation(callback: CallbackQuery, state: FSMContext):
    """Навигация по календарю"""
    try:
        _, year, month = callback.data.split(":")
        year = int(year)
        month = int(month)
        
        await callback.message.edit_reply_markup(
            reply_markup=get_calendar_keyboard(year, month)
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in calendar navigation: {e}")
        await callback.answer("Ошибка навигации", show_alert=True)


@router.callback_query(F.data.startswith("date:"))
async def process_date_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора даты из календаря"""
    try:
        date_str = callback.data.split(":")[1]
        current_state = await state.get_state()
        user_id = callback.from_user.id
        
        logger.info(f"User {user_id} selected date {date_str}, state: {current_state}")
        
        # Проверка для нового бронирования (не для изменения)
        if current_state == BookingStates.waiting_for_date:
            # Проверить, есть ли уже бронь на эту дату
            if db.has_user_booking_on_date(user_id, date_str):
                await callback.answer(
                    f"❌ У вас уже есть бронь на {date_str}.\n"
                    f"Используйте '🔁 Поменять бронь' для изменения.",
                    show_alert=True
                )
                return
        
        available_places = db.get_available_places(date_str)
        
        if not available_places:
            await callback.answer(
                f"К сожалению, на {date_str} все места заняты. Выберите другую дату.",
                show_alert=True
            )
            return
        
        if current_state == BookingStates.waiting_for_date:
            await state.update_data(booking_date=date_str)
            
            if os.path.exists(OFFICE_MAP_PATH):
                try:
                    photo = FSInputFile(OFFICE_MAP_PATH)
                    await callback.message.answer_photo(
                        photo=photo,
                        caption=f"🗺️ Карта офиса\n\nДоступные места на {date_str}:"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки изображения: {e}")
            
            await callback.message.answer(
                "👇 Выберите место:",
                reply_markup=get_places_keyboard(available_places)
            )
            
            await state.set_state(BookingStates.waiting_for_place)
            
        elif current_state == ChangeStates.waiting_for_new_date:
            await state.update_data(new_booking_date=date_str)
            
            if os.path.exists(OFFICE_MAP_PATH):
                try:
                    photo = FSInputFile(OFFICE_MAP_PATH)
                    await callback.message.answer_photo(
                        photo=photo,
                        caption=f"🗺️ Карта офиса\n\nДоступные места на {date_str}:"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки изображения: {e}")
            
            await callback.message.answer(
                "👇 Выберите новое место:",
                reply_markup=get_places_keyboard(available_places)
            )
            
            await state.set_state(ChangeStates.waiting_for_new_place)
        
        await callback.answer()
        
        # Удалить сообщение с календарём
        try:
            await callback.message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Error in date selection: {e}", exc_info=True)
        await callback.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data == "cancel_calendar")
async def cancel_calendar(callback: CallbackQuery, state: FSMContext):
    """Отмена выбора даты"""
    await callback.message.delete()
    await callback.answer("Выбор даты отменён")
    await state.clear()


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    """Игнорировать неактивные кнопки календаря"""
    await callback.answer()


@router.callback_query(F.data == "retry_place_selection")
async def retry_place_selection(callback: CallbackQuery, state: FSMContext):
    """Повторный выбор места без возврата к календарю"""
    try:
        current_state = await state.get_state()
        data = await state.get_data()
        
        if current_state == BookingStates.confirming_booking:
            booking_date = data.get('booking_date')
            available_places = db.get_available_places(booking_date)
            
            if available_places:
                if os.path.exists(OFFICE_MAP_PATH):
                    try:
                        photo = FSInputFile(OFFICE_MAP_PATH)
                        await callback.message.answer_photo(
                            photo=photo,
                            caption=f"🗺️ Карта офиса\n\nДоступные места на {booking_date}:"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки изображения: {e}")
                
                await callback.message.answer(
                    "👇 Выберите место:",
                    reply_markup=get_places_keyboard(available_places)
                )
                
                await state.set_state(BookingStates.waiting_for_place)
            else:
                await callback.message.answer(
                    "❌ К сожалению, все места уже заняты."
                )
                await state.clear()
                
        elif current_state == ChangeStates.confirming_change:
            new_date = data.get('new_booking_date')
            available_places = db.get_available_places(new_date)
            
            if available_places:
                if os.path.exists(OFFICE_MAP_PATH):
                    try:
                        photo = FSInputFile(OFFICE_MAP_PATH)
                        await callback.message.answer_photo(
                            photo=photo,
                            caption=f"🗺️ Карта офиса\n\nДоступные места на {new_date}:"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки изображения: {e}")
                
                await callback.message.answer(
                    "👇 Выберите новое место:",
                    reply_markup=get_places_keyboard(available_places)
                )
                
                await state.set_state(ChangeStates.waiting_for_new_place)
            else:
                await callback.message.answer(
                    "❌ К сожалению, все места уже заняты."
                )
                await state.clear()
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in retry_place_selection: {e}", exc_info=True)
        await callback.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("select_place:"))
async def process_place_selection(callback: CallbackQuery, state: FSMContext):
    try:
        place_id = int(callback.data.split(":")[1])
        current_state = await state.get_state()
        data = await state.get_data()
        
        logger.info(f"User {callback.from_user.id} selected place {place_id}, state: {current_state}")
        
        if current_state == BookingStates.waiting_for_place:
            booking_date = data.get('booking_date')
            await state.update_data(place_id=place_id)
            
            await callback.message.answer(
                f"✅ Вы выбрали Место №{place_id} на {booking_date}.\n\n"
                "Подтвердить бронь?",
                reply_markup=get_confirmation_keyboard()
            )
            
            await state.set_state(BookingStates.confirming_booking)
            
        elif current_state == ChangeStates.waiting_for_new_place:
            new_date = data.get('new_booking_date')
            await state.update_data(new_place_id=place_id)
            
            await callback.message.answer(
                f"✅ Новая бронь: Место №{place_id} на {new_date}.\n\n"
                "Подтвердить изменение?",
                reply_markup=get_confirmation_keyboard()
            )
            
            await state.set_state(ChangeStates.confirming_change)
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await callback.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data == "confirm_yes")
async def confirm_booking(callback: CallbackQuery, state: FSMContext):
    try:
        current_state = await state.get_state()
        data = await state.get_data()
        user_id = callback.from_user.id
        
        if current_state == BookingStates.confirming_booking:
            place_id = data.get('place_id')
            booking_date = data.get('booking_date')
            
            success = db.create_booking(user_id, place_id, booking_date)
            
            if success:
                await callback.message.answer(
                    f"✅ Отлично! Место №{place_id} забронировано на {booking_date}."
                )
                await state.clear()
            else:
                # Получить доступные места
                available_places = db.get_available_places(booking_date)
                
                if available_places:
                    retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔄 Выбрать другое место",
                            callback_data="retry_place_selection"
                        )]
                    ])
                    
                    await callback.message.answer(
                        "❌ Не удалось создать бронь.\n"
                        "Возможные причины:\n"
                        "• Место уже занято другим пользователем\n"
                        "• У вас уже есть бронь на эту дату\n\n"
                        "Попробуйте выбрать другое место:",
                        reply_markup=retry_keyboard
                    )
                    # Состояние остаётся, чтобы можно было выбрать другое место
                else:
                    await callback.message.answer(
                        "❌ Не удалось создать бронь.\n"
                        "К сожалению, все места на эту дату уже заняты."
                    )
                    await state.clear()
            
        elif current_state == ChangeStates.confirming_change:
            old_booking_id = data.get('old_booking_id')
            new_place_id = data.get('new_place_id')
            new_date = data.get('new_booking_date')
            
            db.cancel_booking(old_booking_id, user_id)
            success = db.create_booking(user_id, new_place_id, new_date)
            
            if success:
                await callback.message.answer(
                    f"✅ Бронь успешно изменена!\n"
                    f"Новая бронь: Место №{new_place_id} на {new_date}."
                )
                await state.clear()
            else:
                # Получить доступные места
                available_places = db.get_available_places(new_date)
                
                if available_places:
                    retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔄 Выбрать другое место",
                            callback_data="retry_place_selection"
                        )]
                    ])
                    
                    await callback.message.answer(
                        "❌ Не удалось изменить бронь.\n"
                        "Возможно, это место уже занято другим пользователем.\n\n"
                        "Попробуйте выбрать другое место:",
                        reply_markup=retry_keyboard
                    )
                    # Состояние остаётся
                else:
                    await callback.message.answer(
                        "❌ Не удалось изменить бронь.\n"
                        "К сожалению, все места на эту дату уже заняты."
                    )
                    await state.clear()
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)


@router.callback_query(F.data == "confirm_change")
async def change_selection(callback: CallbackQuery, state: FSMContext):
    try:
        current_state = await state.get_state()
        data = await state.get_data()
        
        if current_state == BookingStates.confirming_booking:
            booking_date = data.get('booking_date')
            available_places = db.get_available_places(booking_date)
            
            await callback.message.answer(
                f"Выберите другое место на {booking_date}:",
                reply_markup=get_places_keyboard(available_places)
            )
            
            await state.set_state(BookingStates.waiting_for_place)
            
        elif current_state == ChangeStates.confirming_change:
            new_date = data.get('new_booking_date')
            available_places = db.get_available_places(new_date)
            
            await callback.message.answer(
                f"Выберите другое место на {new_date}:",
                reply_markup=get_places_keyboard(available_places)
            )
            
            await state.set_state(ChangeStates.waiting_for_new_place)
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)


@router.callback_query(F.data == "confirm_cancel")
async def cancel_selection(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("❌ Бронирование отменено.")
    await state.clear()
    await callback.answer()


@router.message(F.text == "📅 Мои брони")
async def show_my_bookings(message: Message):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)
    
    if not bookings:
        await message.answer("У вас нет активных броней.")
        return
    
    text = "📅 Ваши активные брони:\n\n"
    for booking in bookings:
        text += f"• {booking['place_name']} - {booking['date']}\n"
    
    await message.answer(text)


@router.message(F.text == "❌ Отменить бронь")
async def start_cancel(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)
    
    if not bookings:
        await message.answer("У вас нет активных броней для отмены.")
        return
    
    await message.answer(
        "Выберите бронь для отмены:",
        reply_markup=get_bookings_keyboard(bookings)
    )
    await state.set_state(CancelStates.selecting_booking)


@router.callback_query(F.data.startswith("booking:"))
async def process_booking_action(callback: CallbackQuery, state: FSMContext):
    try:
        booking_id = int(callback.data.split(":")[1])
        user_id = callback.from_user.id
        current_state = await state.get_state()
        
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            await callback.message.answer("❌ Бронь не найдена.")
            await state.clear()
            await callback.answer()
            return
        
        if current_state == CancelStates.selecting_booking:
            success = db.cancel_booking(booking_id, user_id)
            
            if success:
                await callback.message.answer(
                    f"✅ Бронь {booking['place_name']} на {booking['date']} отменена."
                )
            else:
                await callback.message.answer("❌ Ошибка при отмене брони.")
            
            await state.clear()
            
        elif current_state == ChangeStates.selecting_booking:
            await state.update_data(old_booking_id=booking_id)
            
            now = datetime.now()
            await callback.message.answer(
                f"Текущая бронь: {booking['place_name']} на {booking['date']}\n\n"
                "Выберите новую дату:"
            )
            await callback.message.answer(
                "📅 Календарь:",
                reply_markup=get_calendar_keyboard(now.year, now.month)
            )
            
            await state.set_state(ChangeStates.waiting_for_new_date)
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)


@router.message(F.text == "🔁 Поменять бронь")
async def start_change(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)
    
    if not bookings:
        await message.answer("У вас нет активных броней для изменения.")
        return
    
    await message.answer(
        "Выберите бронь, которую хотите изменить:",
        reply_markup=get_bookings_keyboard(bookings)
    )
    await state.set_state(ChangeStates.selecting_booking)


@router.message(ChangeStates.waiting_for_new_date)
async def process_new_date(message: Message, state: FSMContext):
    """Показать календарь для изменения брони"""
    now = datetime.now()
    await message.answer(
        "Выберите новую дату:",
        reply_markup=get_calendar_keyboard(now.year, now.month)
    )


# Главная функция
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())