import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os
import calendar
import shutil

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
    raise ValueError("BOT_TOKEN не найден в переменных среды")

OFFICE_MAP_PATH = "office_map.png"
TOTAL_PLACES = 13

# ID главного администратора ("мама бота")
SUPER_ADMIN_ID = 528599224

# ID администраторов (загружаются из БД при старте)
ADMIN_IDS = [SUPER_ADMIN_ID]

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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


class AdminStates(StatesGroup):
    waiting_for_user_identifier = State()
    selecting_user_booking = State()
    booking_for_user_date = State()
    booking_for_user_place = State()
    booking_for_user_confirm = State()
    change_for_user_select = State()
    change_for_user_date = State()
    change_for_user_place = State()
    change_for_user_confirm = State()
    adding_admin = State()
    removing_admin = State()
    waiting_for_map_photo = State()
    # Постоянные брони
    permanent_user_id = State()
    permanent_place_id = State()
    permanent_days = State()
    permanent_confirm = State()
    # Просмотр постоянных броней
    view_permanent_user = State()
    # Удаление постоянной брони
    delete_permanent_user = State()
    delete_permanent_select = State()


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
                    booking_type TEXT DEFAULT 'regular',
                    permanent_booking_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (place_id) REFERENCES places(id),
                    FOREIGN KEY (permanent_booking_id) REFERENCES permanent_bookings(id)
                )
            """)

            # Таблица для постоянных броней
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS permanent_bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    place_id INTEGER NOT NULL,
                    weekdays TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (place_id) REFERENCES places(id),
                    FOREIGN KEY (created_by) REFERENCES users(telegram_id)
                )
            """)

            # Таблица для администраторов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    telegram_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (added_by) REFERENCES users(telegram_id)
                )
            """)

            # МИГРАЦИЯ: Добавляем новые колонки если их нет
            try:
                cursor.execute("SELECT booking_type FROM bookings LIMIT 1")
            except sqlite3.OperationalError:
                logger.info("Migrating database: adding booking_type column")
                cursor.execute("ALTER TABLE bookings ADD COLUMN booking_type TEXT DEFAULT 'regular'")

            try:
                cursor.execute("SELECT permanent_booking_id FROM bookings LIMIT 1")
            except sqlite3.OperationalError:
                logger.info("Migrating database: adding permanent_booking_id column")
                cursor.execute("ALTER TABLE bookings ADD COLUMN permanent_booking_id INTEGER")

            cursor.execute("SELECT COUNT(*) FROM places")
            if cursor.fetchone()[0] == 0:
                for i in range(1, TOTAL_PLACES + 1):
                    cursor.execute(
                        "INSERT INTO places (id, name, description) VALUES (?, ?, ?)",
                        (i, f"Место №{i}", f"Рабочее место номер {i}")
                    )

            # Добавляем главного админа в таблицу admins, если его там нет
            cursor.execute("SELECT COUNT(*) FROM admins WHERE telegram_id = ?", (SUPER_ADMIN_ID,))
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "INSERT OR IGNORE INTO admins (telegram_id, added_by) VALUES (?, ?)",
                    (SUPER_ADMIN_ID, SUPER_ADMIN_ID)
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

            # Получаем занятые места из обычных броней
            cursor.execute("""
                SELECT place_id FROM bookings
                WHERE booking_date = ? AND status = 'active'
            """, (date,))
            booked = [row[0] for row in cursor.fetchall()]

            # Получаем места из постоянных броней на этот день недели
            cursor.execute("""
                SELECT place_id FROM permanent_bookings
                WHERE status = 'active' AND weekdays LIKE ?
            """, (f'%{weekday}%',))
            permanent_candidates = [row[0] for row in cursor.fetchall()]

            # 🔥 ИСПРАВЛЕНИЕ: Проверяем, не отменена ли конкретная дата
            # Для каждого места из постоянных броней проверяем,
            # есть ли отменённая бронь на эту дату
            permanent_booked = []
            for place_id in permanent_candidates:
                cursor.execute("""
                    SELECT COUNT(*) FROM bookings
                    WHERE place_id = ? 
                      AND booking_date = ? 
                      AND booking_type = 'permanent'
                      AND status = 'cancelled'
                """, (place_id, date))

                # Если нет отменённой брони - место занято постоянной бронью
                if cursor.fetchone()[0] == 0:
                    permanent_booked.append(place_id)

        available = []
        for place_id in range(1, TOTAL_PLACES + 1):
            # Проверяем занятость
            if place_id not in booked and place_id not in permanent_booked:
                available.append(place_id)

        return available

    def create_booking(self, user_id: int, place_id: int, date: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE user_id = ? AND booking_date = ? AND status = 'active'
            """, (user_id, date))

            if cursor.fetchone()[0] > 0:
                return False

            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE place_id = ? AND booking_date = ? AND status = 'active'
            """, (place_id, date))

            if cursor.fetchone()[0] > 0:
                return False

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
                SELECT b.id, b.place_id, b.booking_date, p.name, b.booking_type, b.permanent_booking_id
                FROM bookings b
                JOIN places p ON b.place_id = p.id
                WHERE b.user_id = ? AND b.status = 'active'
                ORDER BY b.booking_date
            """, (user_id,))

            bookings = []
            for row in cursor.fetchall():
                booking_type = row[4] if row[4] else 'regular'
                bookings.append({
                    'id': row[0],
                    'place_id': row[1],
                    'date': row[2],
                    'place_name': row[3],
                    'booking_type': booking_type,
                    'permanent_booking_id': row[5]
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

    def get_all_bookings(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT b.id, b.user_id, u.username, u.first_name, b.place_id, p.name, b.booking_date
                FROM bookings b
                JOIN places p ON b.place_id = p.id
                JOIN users u ON b.user_id = u.telegram_id
                WHERE b.status = 'active'
                ORDER BY b.booking_date, b.place_id
            """)

            bookings = []
            for row in cursor.fetchall():
                bookings.append({
                    'id': row[0],
                    'user_id': row[1],
                    'username': row[2],
                    'first_name': row[3],
                    'place_id': row[4],
                    'place_name': row[5],
                    'date': row[6]
                })
            return bookings

    def cancel_all_bookings(self) -> int:
        """Отменить все обычные брони и постоянные брони"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Отменяем все обычные брони
            cursor.execute("""
                UPDATE bookings
                SET status = 'cancelled'
                WHERE status = 'active'
            """)
            bookings_count = cursor.rowcount

            # Отменяем все постоянные брони
            cursor.execute("""
                UPDATE permanent_bookings
                SET status = 'deleted'
                WHERE status = 'active'
            """)
            permanent_count = cursor.rowcount

            conn.commit()
            logger.info(f"Cancelled {bookings_count} bookings and {permanent_count} permanent bookings")
            return bookings_count + permanent_count

    def find_user_by_username(self, username: str) -> Optional[int]:
        username = username.lstrip('@').lower()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT telegram_id FROM users
                WHERE LOWER(username) = ?
            """, (username,))
            row = cursor.fetchone()
            return row[0] if row else None

    def cancel_booking_admin(self, booking_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE bookings
                SET status = 'cancelled'
                WHERE id = ? AND status = 'active'
            """, (booking_id,))
            conn.commit()
            return cursor.rowcount > 0

    def create_booking_for_user(self, admin_user_id: int, target_user_id: int, place_id: int, date: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) FROM bookings
                WHERE place_id = ? AND booking_date = ? AND status = 'active'
            """, (place_id, date))

            if cursor.fetchone()[0] > 0:
                return False

            try:
                cursor.execute("""
                    INSERT INTO bookings (user_id, place_id, booking_date, status)
                    VALUES (?, ?, ?, 'active')
                """, (target_user_id, place_id, date))
                conn.commit()
                return True
            except:
                return False

    def create_permanent_booking(self, admin_id: int, user_id: int, place_id: int, weekdays: List[int]) -> bool:
        """Создать постоянную бронь"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                # Проверяем, нет ли уже постоянной брони на это место + эти дни у ЛЮБОГО пользователя
                cursor.execute("""
                    SELECT id, user_id, weekdays FROM permanent_bookings
                    WHERE place_id = ? AND status = 'active'
                """, (place_id,))

                existing = cursor.fetchall()
                for existing_id, existing_user_id, existing_weekdays_str in existing:
                    existing_weekdays = [int(d) for d in existing_weekdays_str.split(',')]
                    # Проверяем пересечение дней
                    if any(day in existing_weekdays for day in weekdays):
                        logger.error(
                            f"Permanent booking conflict: place {place_id} already booked by user {existing_user_id} on overlapping days")
                        return False

                # Проверяем, нет ли уже такой постоянной брони у этого пользователя
                cursor.execute("""
                    SELECT id FROM permanent_bookings
                    WHERE user_id = ? AND place_id = ? AND status = 'active'
                """, (user_id, place_id))

                if cursor.fetchone():
                    logger.error(f"Permanent booking already exists for user {user_id} place {place_id}")
                    return False

                # Сохраняем постоянную бронь
                cursor.execute("""
                    INSERT INTO permanent_bookings (user_id, place_id, weekdays, created_by, status)
                    VALUES (?, ?, ?, ?, 'active')
                """, (user_id, place_id, ','.join(map(str, weekdays)), admin_id))

                permanent_id = cursor.lastrowid

                # Создаём брони на ближайшие 60 дней
                today = datetime.now().date()
                created_count = 0
                for i in range(60):
                    check_date = today + timedelta(days=i)
                    if check_date.weekday() in weekdays:
                        date_str = check_date.strftime("%d.%m.%Y")

                        # Проверяем, нет ли уже брони
                        cursor.execute("""
                            SELECT COUNT(*) FROM bookings
                            WHERE place_id = ? AND booking_date = ? AND status = 'active'
                        """, (place_id, date_str))

                        if cursor.fetchone()[0] == 0:
                            cursor.execute("""
                                INSERT INTO bookings (user_id, place_id, booking_date, status, booking_type, permanent_booking_id)
                                VALUES (?, ?, ?, 'active', 'permanent', ?)
                            """, (user_id, place_id, date_str, permanent_id))
                            created_count += 1

                conn.commit()
                logger.info(f"Created permanent booking {permanent_id} with {created_count} dates")
                return True
            except Exception as e:
                logger.error(f"Error creating permanent booking: {e}")
                return False

    def get_permanent_bookings(self, user_id: int = None) -> List[Dict]:
        """Получить постоянные брони"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if user_id:
                cursor.execute("""
                    SELECT pb.id, pb.user_id, u.username, u.first_name, pb.place_id, p.name, pb.weekdays, pb.created_at
                    FROM permanent_bookings pb
                    JOIN users u ON pb.user_id = u.telegram_id
                    JOIN places p ON pb.place_id = p.id
                    WHERE pb.status = 'active' AND pb.user_id = ?
                    ORDER BY pb.place_id
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT pb.id, pb.user_id, u.username, u.first_name, pb.place_id, p.name, pb.weekdays, pb.created_at
                    FROM permanent_bookings pb
                    JOIN users u ON pb.user_id = u.telegram_id
                    JOIN places p ON pb.place_id = p.id
                    WHERE pb.status = 'active'
                    ORDER BY pb.user_id, pb.place_id
                """)

            bookings = []
            for row in cursor.fetchall():
                weekdays = [int(d) for d in row[6].split(',')]
                bookings.append({
                    'id': row[0],
                    'user_id': row[1],
                    'username': row[2],
                    'first_name': row[3],
                    'place_id': row[4],
                    'place_name': row[5],
                    'weekdays': weekdays,
                    'created_at': row[7]
                })
            return bookings

    def delete_permanent_booking(self, permanent_id: int) -> bool:
        """Удалить постоянную бронь и все связанные будущие брони"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                # Помечаем постоянную бронь как удалённую
                cursor.execute("""
                    UPDATE permanent_bookings
                    SET status = 'deleted'
                    WHERE id = ?
                """, (permanent_id,))

                # Удаляем все будущие брони этой постоянной брони
                # Получаем все брони с этим permanent_booking_id
                cursor.execute("""
                    SELECT id, booking_date FROM bookings
                    WHERE permanent_booking_id = ? AND status = 'active'
                """, (permanent_id,))

                bookings_to_check = cursor.fetchall()
                today = datetime.now().date()

                # Проверяем каждую бронь и удаляем только будущие
                for booking_id, booking_date_str in bookings_to_check:
                    # Конвертируем строку DD.MM.YYYY в объект date
                    booking_date = datetime.strptime(booking_date_str, "%d.%m.%Y").date()

                    # Если дата в будущем - отменяем
                    if booking_date >= today:
                        cursor.execute("""
                            UPDATE bookings
                            SET status = 'cancelled'
                            WHERE id = ?
                        """, (booking_id,))

                conn.commit()
                logger.info(f"Deleted permanent booking {permanent_id} and future bookings")
                return True
            except Exception as e:
                logger.error(f"Error deleting permanent booking: {e}")
                return False

    def add_admin(self, admin_id: int, added_by: int) -> bool:
        """Добавить администратора в БД"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO admins (telegram_id, added_by)
                    VALUES (?, ?)
                """, (admin_id, added_by))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error adding admin: {e}")
                return False

    def remove_admin(self, admin_id: int) -> bool:
        """Удалить администратора из БД (кроме главного)"""
        if admin_id == SUPER_ADMIN_ID:
            return False

        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    DELETE FROM admins WHERE telegram_id = ?
                """, (admin_id,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error removing admin: {e}")
                return False

    def get_all_admins(self) -> List[int]:
        """Получить список всех администраторов из БД"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id FROM admins")
            return [row[0] for row in cursor.fetchall()]

    def get_all_admins_with_info(self) -> List[Dict]:
        """Получить список администраторов с информацией о них"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT a.telegram_id, u.username, u.first_name
                FROM admins a
                LEFT JOIN users u ON a.telegram_id = u.telegram_id
                ORDER BY a.telegram_id
            """)

            admins = []
            for row in cursor.fetchall():
                admins.append({
                    'telegram_id': row[0],
                    'username': row[1],
                    'first_name': row[2]
                })
            return admins


# Инициализация
db = Database()

# Загружаем список администраторов из БД
ADMIN_IDS = db.get_all_admins()
logger.info(f"Loaded {len(ADMIN_IDS)} admins from database: {ADMIN_IDS}")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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


def get_admin_menu():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🪑 Забронировать место")],
            [KeyboardButton(text="📅 Мои брони")],
            [KeyboardButton(text="❌ Отменить бронь")],
            [KeyboardButton(text="🔁 Поменять бронь")],
            [KeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ")]
        ],
        resize_keyboard=True
    )
    return keyboard


def get_admin_panel_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все брони", callback_data="admin_all_bookings")],
        [InlineKeyboardButton(text="❌ Отменить все брони", callback_data="admin_cancel_all")],
        [InlineKeyboardButton(text="🗑️ Отменить бронь пользователя", callback_data="admin_cancel_user")],
        [InlineKeyboardButton(text="➕ Забронировать за пользователя", callback_data="admin_book_for_user")],
        [InlineKeyboardButton(text="🔄 Изменить бронь пользователя", callback_data="admin_change_for_user")],
        [InlineKeyboardButton(text="📌 Постоянные брони", callback_data="admin_permanent_menu")],
        [InlineKeyboardButton(text="🗺️ Заменить карту офиса", callback_data="admin_change_map")],
        [InlineKeyboardButton(text="👤 Добавить администратора", callback_data="admin_add_admin")],
        [InlineKeyboardButton(text="🗑 Удалить администратора", callback_data="admin_remove_admin")]
    ])
    return keyboard


def get_permanent_bookings_menu():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать постоянную бронь", callback_data="admin_create_permanent")],
        [InlineKeyboardButton(text="📋 Все постоянные брони", callback_data="admin_view_all_permanent")],
        [InlineKeyboardButton(text="👤 Постоянные брони пользователя", callback_data="admin_view_user_permanent")],
        [InlineKeyboardButton(text="🗑️ Удалить постоянную бронь", callback_data="admin_delete_permanent")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_to_main")]
    ])
    return keyboard


def get_weekday_keyboard(selected: List[int] = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора дней недели"""
    if selected is None:
        selected = []

    weekdays = [
        ("Пн", 0), ("Вт", 1), ("Ср", 2), ("Чт", 3),
        ("Пт", 4), ("Сб", 5), ("Вс", 6)
    ]

    buttons = []
    row = []
    for name, num in weekdays:
        check = "✅" if num in selected else "⬜️"
        row.append(InlineKeyboardButton(
            text=f"{check} {name}",
            callback_data=f"weekday_{num}"
        ))
        if len(row) == 4:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="weekday_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="weekday_cancel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_places_keyboard(available_places: List[int]) -> InlineKeyboardMarkup:
    buttons = []
    row = []

    for i, place_id in enumerate(available_places):
        row.append(InlineKeyboardButton(
            text=f"Место №{place_id}",
            callback_data=f"place_{place_id}"
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
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_cancel")
        ]
    ])
    return keyboard


def get_bookings_keyboard(bookings: List[Dict]) -> InlineKeyboardMarkup:
    buttons = []
    for booking in bookings:
        icon = "📌" if booking.get('booking_type') == 'permanent' else "📅"
        button_text = f"{icon} {booking['place_name']} - {booking['date']}"
        buttons.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"booking_{booking['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    buttons = []

    month_name = calendar.month_name[month]
    buttons.append([InlineKeyboardButton(
        text=f"📅 {month_name} {year}",
        callback_data="ignore"
    )])

    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([InlineKeyboardButton(text=day, callback_data="ignore") for day in week_days])

    month_calendar = calendar.monthcalendar(year, month)
    today = datetime.now().date()

    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date = datetime(year, month, day).date()

                if date < today:
                    row.append(InlineKeyboardButton(text="·", callback_data="ignore"))
                else:
                    date_str = date.strftime("%d.%m.%Y")
                    row.append(InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"date_{date_str}"
                    ))
        buttons.append(row)

    nav_row = []

    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1

    if datetime(prev_year, prev_month, 1).date() >= datetime(today.year, today.month, 1).date():
        nav_row.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"cal_{prev_year}_{prev_month}"
        ))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))

    nav_row.append(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_calendar"))

    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    nav_row.append(InlineKeyboardButton(
        text="▶️",
        callback_data=f"cal_{next_year}_{next_month}"
    ))

    buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_bookings_calendar_keyboard(year: int, month: int, booked_dates: List[str]) -> InlineKeyboardMarkup:
    """Календарь с выделенными забронированными днями"""
    buttons = []

    month_name = calendar.month_name[month]
    buttons.append([InlineKeyboardButton(
        text=f"📅 {month_name} {year}",
        callback_data="ignore"
    )])

    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([InlineKeyboardButton(text=day, callback_data="ignore") for day in week_days])

    month_calendar = calendar.monthcalendar(year, month)

    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date = datetime(year, month, day).date()
                date_str = date.strftime("%d.%m.%Y")

                if date_str in booked_dates:
                    # День с бронью - в квадратных скобках
                    row.append(InlineKeyboardButton(
                        text=f"[{day}]",
                        callback_data=f"view_booking_{date_str}"
                    ))
                else:
                    # Обычный день - некликабельный
                    row.append(InlineKeyboardButton(text=str(day), callback_data="ignore"))
        buttons.append(row)

    # Навигация
    nav_row = []

    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1

    nav_row.append(InlineKeyboardButton(
        text="◀️",
        callback_data=f"booking_cal_{prev_year}_{prev_month}"
    ))

    nav_row.append(InlineKeyboardButton(text="❌ Закрыть", callback_data="close_calendar"))

    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    nav_row.append(InlineKeyboardButton(
        text="▶️",
        callback_data=f"booking_cal_{next_year}_{next_month}"
    ))

    buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Обработчики команд
@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name)

    logger.info(f"User started bot: ID={user.id}, username={user.username}, name={user.first_name}")

    is_admin_user = is_admin(user.id)
    menu = get_admin_menu() if is_admin_user else get_main_menu()

    greeting = f"Привет, {user.first_name}! 👋\n\n"
    greeting += "Я помогу тебе забронировать рабочее место в офисе.\n"
    greeting += f"\n🆔 Ваш Telegram ID: <code>{user.id}</code>\n"

    if is_admin_user:
        greeting += "\n🔑 У вас есть права администратора!"

    greeting += "\nВыбери нужное действие:"

    await message.answer(greeting, reply_markup=menu, parse_mode="HTML")


@router.message(F.text == "🪑 Забронировать место")
async def start_booking(message: Message, state: FSMContext):
    now = datetime.now()
    await message.answer(
        "Выберите дату бронирования:",
        reply_markup=get_calendar_keyboard(now.year, now.month)
    )
    await state.set_state(BookingStates.waiting_for_date)


@router.message(F.text == "📅 Мои брони")
async def show_my_bookings(message: Message):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)

    if not bookings:
        await message.answer("У вас нет активных броней.")
        return

    # Если броней 3 или больше - показываем календарь
    if len(bookings) >= 3:
        booked_dates = [b['date'] for b in bookings]
        now = datetime.now()

        await message.answer(
            "📅 <b>Ваши брони</b>\n\n"
            "Выберите дату для просмотра деталей:\n"
            "[15] — забронированный день",
            reply_markup=get_bookings_calendar_keyboard(now.year, now.month, booked_dates),
            parse_mode="HTML"
        )
    else:
        # Если броней меньше 3 - показываем список как раньше
        text = "📅 Ваши активные брони:\n\n"
        for booking in bookings:
            booking_icon = "📌" if booking.get('booking_type') == 'permanent' else "•"
            text += f"{booking_icon} {booking['place_name']} - {booking['date']}"
            if booking.get('booking_type') == 'permanent':
                text += " (постоянная)"
            text += "\n"

        await message.answer(text)


@router.message(F.text == "❌ Отменить бронь")
async def start_cancel(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)

    if not bookings:
        await message.answer("У вас нет активных броней для отмены.")
        return

    # Если броней 3 или больше - показываем календарь
    if len(bookings) >= 3:
        booked_dates = [b['date'] for b in bookings]
        now = datetime.now()

        await message.answer(
            "❌ <b>Отмена брони</b>\n\n"
            "Выберите дату для отмены:\n"
            "[15] — забронированный день",
            reply_markup=get_bookings_calendar_keyboard(now.year, now.month, booked_dates),
            parse_mode="HTML"
        )
        await state.set_state(CancelStates.selecting_booking)
    else:
        # Если броней меньше 3 - показываем список
        await message.answer(
            "Выберите бронь для отмены:",
            reply_markup=get_bookings_keyboard(bookings)
        )
        await state.set_state(CancelStates.selecting_booking)


@router.message(F.text == "🔁 Поменять бронь")
async def start_change(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bookings = db.get_user_bookings(user_id)

    if not bookings:
        await message.answer("У вас нет активных броней для изменения.")
        return

    # Если броней 3 или больше - показываем календарь
    if len(bookings) >= 3:
        booked_dates = [b['date'] for b in bookings]
        now = datetime.now()

        await message.answer(
            "🔁 <b>Изменение брони</b>\n\n"
            "Выберите дату для изменения:\n"
            "[15] — забронированный день",
            reply_markup=get_bookings_calendar_keyboard(now.year, now.month, booked_dates),
            parse_mode="HTML"
        )
        await state.set_state(ChangeStates.selecting_booking)
    else:
        # Если броней меньше 3 - показываем список
        await message.answer(
            "Выберите бронь, которую хотите изменить:",
            reply_markup=get_bookings_keyboard(bookings)
        )
        await state.set_state(ChangeStates.selecting_booking)


# Обработчики календаря
@router.callback_query(F.data.startswith("cal_"))
async def process_calendar_navigation(callback: CallbackQuery):
    try:
        _, year, month = callback.data.split("_")
        year = int(year)
        month = int(month)

        await callback.message.edit_reply_markup(
            reply_markup=get_calendar_keyboard(year, month)
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in calendar navigation: {e}")
        await callback.answer("Ошибка навигации", show_alert=True)


@router.callback_query(F.data == "cancel_calendar")
async def cancel_calendar(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.answer("Выбор даты отменён")
    await state.clear()


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()


# Навигация по календарю броней
@router.callback_query(F.data.startswith("booking_cal_"))
async def process_bookings_calendar_navigation(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, year, month = callback.data.split("_")
        year = int(year)
        month = int(month)

        user_id = callback.from_user.id
        bookings = db.get_user_bookings(user_id)
        booked_dates = [b['date'] for b in bookings]

        # Определяем текст в зависимости от состояния
        current_state = await state.get_state()
        if current_state == "CancelStates:selecting_booking":
            header = "❌ <b>Отмена брони</b>\n\n"
        elif current_state == "ChangeStates:selecting_booking":
            header = "🔁 <b>Изменение брони</b>\n\n"
        else:
            header = "📅 <b>Ваши брони</b>\n\n"

        await callback.message.edit_text(
            header + "Выберите дату для просмотра деталей:\n[15] — забронированный день",
            reply_markup=get_bookings_calendar_keyboard(year, month, booked_dates),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in bookings calendar navigation: {e}")
        await callback.answer("Ошибка навигации", show_alert=True)


@router.callback_query(F.data == "close_calendar")
async def close_calendar(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.answer("Закрыто")
    await state.clear()


# Просмотр деталей брони по дате
@router.callback_query(F.data.startswith("view_booking_"))
async def view_booking_details(callback: CallbackQuery, state: FSMContext):
    try:
        date_str = callback.data.split("view_booking_")[1]
        user_id = callback.from_user.id

        # Получаем все брони пользователя
        bookings = db.get_user_bookings(user_id)

        # Находим бронь на эту дату
        booking = None
        for b in bookings:
            if b['date'] == date_str:
                booking = b
                break

        if not booking:
            await callback.answer("Бронь не найдена", show_alert=True)
            return

        # Определяем текущее состояние для формирования кнопок
        current_state = await state.get_state()

        # Формируем текст
        booking_type_text = "Постоянная бронь" if booking.get('booking_type') == 'permanent' else "Обычная бронь"
        icon = "📌" if booking.get('booking_type') == 'permanent' else "📅"

        text = (
            f"{icon} <b>Бронь на {date_str}</b>\n\n"
            f"🪑 Место: {booking['place_name']}\n"
            f"📅 Дата: {date_str}\n"
            f"📋 Тип: {booking_type_text}"
        )

        # Формируем кнопки действий
        buttons = []

        # Сохраняем ID брони в состояние для дальнейших действий
        await state.update_data(selected_booking_id=booking['id'])

        # Если мы в процессе отмены - показываем кнопку отмены
        if current_state == "CancelStates:selecting_booking":
            buttons.append([InlineKeyboardButton(text="❌ Отменить эту бронь",
                                                 callback_data=f"confirm_cancel_booking_{booking['id']}")])
        # Если в процессе изменения - показываем кнопку изменения
        elif current_state == "ChangeStates:selecting_booking":
            buttons.append([InlineKeyboardButton(text="🔁 Изменить эту бронь",
                                                 callback_data=f"confirm_change_booking_{booking['id']}")])
        # Если просто просмотр - показываем обе кнопки
        else:
            buttons.append([
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"confirm_cancel_booking_{booking['id']}"),
                InlineKeyboardButton(text="🔁 Изменить", callback_data=f"confirm_change_booking_{booking['id']}")
            ])

        # Кнопка возврата к календарю
        buttons.append([InlineKeyboardButton(text="◀️ Назад к календарю", callback_data="back_to_bookings_calendar")])

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await callback.answer()

    except Exception as e:
        logger.error(f"Error viewing booking details: {e}", exc_info=True)
        await callback.answer("Ошибка при просмотре деталей", show_alert=True)


@router.callback_query(F.data == "back_to_bookings_calendar")
async def back_to_bookings_calendar(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    bookings = db.get_user_bookings(user_id)
    booked_dates = [b['date'] for b in bookings]
    now = datetime.now()

    current_state = await state.get_state()
    if current_state == "CancelStates:selecting_booking":
        header = "❌ <b>Отмена брони</b>\n\n"
    elif current_state == "ChangeStates:selecting_booking":
        header = "🔁 <b>Изменение брони</b>\n\n"
    else:
        header = "📅 <b>Ваши брони</b>\n\n"

    await callback.message.edit_text(
        header + "Выберите дату для просмотра деталей:\n[15] — забронированный день",
        reply_markup=get_bookings_calendar_keyboard(now.year, now.month, booked_dates),
        parse_mode="HTML"
    )
    await callback.answer()


# Подтверждение отмены брони из деталей
@router.callback_query(F.data.startswith("confirm_cancel_booking_"))
async def confirm_cancel_from_details(callback: CallbackQuery, state: FSMContext):
    try:
        booking_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            await callback.answer("❌ Бронь не найдена", show_alert=True)
            return

        success = db.cancel_booking(booking_id, user_id)

        if success:
            await callback.message.edit_text(
                f"✅ Бронь {booking['place_name']} на {booking['date']} отменена."
            )
        else:
            await callback.message.edit_text("❌ Ошибка при отмене.")

        await state.clear()
        await callback.answer()
    except Exception as e:
        logger.error(f"Error canceling booking: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


# Начало изменения брони из деталей
@router.callback_query(F.data.startswith("confirm_change_booking_"))
async def confirm_change_from_details(callback: CallbackQuery, state: FSMContext):
    try:
        booking_id = int(callback.data.split("_")[-1])

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            await callback.answer("❌ Бронь не найдена", show_alert=True)
            return

        await state.update_data(old_booking_id=booking_id)

        now = datetime.now()
        await callback.message.edit_text(
            f"Текущая бронь: {booking['place_name']} на {booking['date']}\n\n"
            "Выберите новую дату:",
            reply_markup=get_calendar_keyboard(now.year, now.month)
        )

        await state.set_state(ChangeStates.waiting_for_new_date)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting change: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


# Обработчики выбора даты
@router.callback_query(F.data.startswith("date_"))
async def process_date_selection(callback: CallbackQuery, state: FSMContext):
    try:
        date_str = callback.data.split("_", 1)[1]
        current_state = await state.get_state()
        user_id = callback.from_user.id

        logger.info(f"Date selected: {date_str}, state: {current_state}")

        # Проверка для обычного бронирования
        if current_state == "BookingStates:waiting_for_date":
            if db.has_user_booking_on_date(user_id, date_str):
                await callback.answer(
                    f"❌ У вас уже есть бронь на {date_str}.\nИспользуйте '🔁 Поменять бронь'.",
                    show_alert=True
                )
                return

        available_places = db.get_available_places(date_str)

        if not available_places:
            await callback.answer(
                f"На {date_str} все места заняты.",
                show_alert=True
            )
            return

        await state.update_data(booking_date=date_str)

        try:
            await callback.message.delete()
        except:
            pass

        if os.path.exists(OFFICE_MAP_PATH):
            try:
                photo = FSInputFile(OFFICE_MAP_PATH)
                await callback.message.answer_photo(
                    photo=photo,
                    caption=f"🗺️ Карта офиса\n\nДоступные места на {date_str}:"
                )
            except Exception as e:
                logger.error(f"Error sending image: {e}")

        await callback.message.answer(
            "👇 Выберите место:",
            reply_markup=get_places_keyboard(available_places)
        )

        if current_state == "BookingStates:waiting_for_date":
            await state.set_state(BookingStates.waiting_for_place)
        elif current_state == "ChangeStates:waiting_for_new_date":
            await state.set_state(ChangeStates.waiting_for_new_place)
        elif current_state == "AdminStates:booking_for_user_date":
            await state.set_state("AdminStates:booking_for_user_place")
        elif current_state == "AdminStates:change_for_user_place":
            await state.set_state("AdminStates:change_for_user_confirm")

        await callback.answer()

    except Exception as e:
        logger.error(f"Error in date selection: {e}", exc_info=True)
        await callback.answer("Произошла ошибка", show_alert=True)


# Обработчики выбора места
@router.callback_query(F.data.startswith("place_"))
async def process_place_selection(callback: CallbackQuery, state: FSMContext):
    try:
        place_id = int(callback.data.split("_")[1])
        current_state = await state.get_state()
        data = await state.get_data()

        logger.info(f"Place selected: {place_id}, state: {current_state}")

        if current_state == "BookingStates:waiting_for_place":
            booking_date = data.get('booking_date')
            await state.update_data(place_id=place_id)

            await callback.message.answer(
                f"✅ Вы выбрали Место №{place_id} на {booking_date}.\n\n"
                "Подтвердить бронь?",
                reply_markup=get_confirmation_keyboard()
            )

            await state.set_state(BookingStates.confirming_booking)

        elif current_state == "ChangeStates:waiting_for_new_place":
            new_date = data.get('booking_date')
            old_booking_id = data.get('old_booking_id')
            await state.update_data(new_place_id=place_id)

            # Получаем информацию о старой брони
            old_booking = db.get_booking_by_id(old_booking_id)

            if old_booking:
                await callback.message.answer(
                    f"🔄 <b>Изменение брони</b>\n\n"
                    f"Меняем:\n"
                    f"📍 <s>{old_booking['place_name']} на {old_booking['date']}</s>\n\n"
                    f"На:\n"
                    f"✅ Место №{place_id} на {new_date}\n\n"
                    f"Подтвердить изменение?",
                    reply_markup=get_confirmation_keyboard(),
                    parse_mode="HTML"
                )
            else:
                await callback.message.answer(
                    f"✅ Новая бронь: Место №{place_id} на {new_date}.\n\n"
                    "Подтвердить изменение?",
                    reply_markup=get_confirmation_keyboard()
                )

            await state.set_state(ChangeStates.confirming_change)

        elif current_state == "AdminStates:booking_for_user_place":
            await state.update_data(place_id=place_id)

            await callback.message.answer(
                f"Создать бронь для пользователя {data['target_user_id']}:\n"
                f"Место №{place_id} на {data['booking_date']}?",
                reply_markup=get_confirmation_keyboard()
            )

            await state.set_state("AdminStates:booking_for_user_confirm")

        elif current_state == "AdminStates:change_for_user_confirm":
            await state.update_data(new_place_id=place_id)

            await callback.message.answer(
                f"Изменить бронь пользователя {data['target_user_id']}:\n"
                f"Новое место: №{place_id} на {data['booking_date']}?",
                reply_markup=get_confirmation_keyboard()
            )

        await callback.answer()
    except Exception as e:
        logger.error(f"Error in place selection: {e}", exc_info=True)
        await callback.answer("Произошла ошибка", show_alert=True)


# Обработчики подтверждения
@router.callback_query(F.data == "confirm_yes")
async def confirm_action(callback: CallbackQuery, state: FSMContext):
    try:
        current_state = await state.get_state()
        data = await state.get_data()
        user_id = callback.from_user.id

        logger.info(f"Confirm: state={current_state}")

        if current_state == "BookingStates:confirming_booking":
            place_id = data.get('place_id')
            booking_date = data.get('booking_date')

            success = db.create_booking(user_id, place_id, booking_date)

            if success:
                await callback.message.answer(
                    f"✅ Отлично! Место №{place_id} забронировано на {booking_date}."
                )
            else:
                await callback.message.answer(
                    "❌ Не удалось создать бронь. Место занято или у вас уже есть бронь на эту дату."
                )

            await state.clear()

        elif current_state == "ChangeStates:confirming_change":
            old_booking_id = data.get('old_booking_id')
            new_place_id = data.get('new_place_id')
            new_date = data.get('booking_date')

            db.cancel_booking(old_booking_id, user_id)
            success = db.create_booking(user_id, new_place_id, new_date)

            if success:
                await callback.message.answer(
                    f"✅ Бронь изменена! Новое место: №{new_place_id} на {new_date}."
                )
            else:
                await callback.message.answer("❌ Ошибка при изменении брони.")

            await state.clear()

        elif current_state == "AdminStates:booking_for_user_confirm":
            target_user_id = data.get('target_user_id')
            place_id = data.get('place_id')
            booking_date = data.get('booking_date')

            success = db.create_booking_for_user(user_id, target_user_id, place_id, booking_date)

            if success:
                await callback.message.answer(
                    f"✅ Бронь создана для пользователя {target_user_id}:\n"
                    f"Место №{place_id} на {booking_date}"
                )
            else:
                await callback.message.answer("❌ Ошибка. Место уже занято.")

            await state.clear()

        elif current_state == "AdminStates:change_for_user_confirm":
            old_booking_id = data.get('old_booking_id')
            target_user_id = data.get('target_user_id')
            new_place_id = data.get('new_place_id')
            new_date = data.get('booking_date')

            db.cancel_booking_admin(old_booking_id)
            success = db.create_booking_for_user(user_id, target_user_id, new_place_id, new_date)

            if success:
                await callback.message.answer(
                    f"✅ Бронь изменена для пользователя {target_user_id}:\n"
                    f"Место №{new_place_id} на {new_date}"
                )
            else:
                await callback.message.answer("❌ Ошибка при изменении.")

            await state.clear()

        await callback.answer()
    except Exception as e:
        logger.error(f"Error in confirm: {e}", exc_info=True)


@router.callback_query(F.data == "confirm_cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("❌ Действие отменено.")
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "confirm_change")
async def change_selection(callback: CallbackQuery, state: FSMContext):
    try:
        current_state = await state.get_state()
        data = await state.get_data()

        if current_state in ["BookingStates:confirming_booking", "AdminStates:booking_for_user_confirm"]:
            booking_date = data.get('booking_date')
            available_places = db.get_available_places(booking_date)

            await callback.message.answer(
                f"Выберите другое место на {booking_date}:",
                reply_markup=get_places_keyboard(available_places)
            )

            if current_state == "BookingStates:confirming_booking":
                await state.set_state(BookingStates.waiting_for_place)
            else:
                await state.set_state("AdminStates:booking_for_user_place")

        elif current_state == "ChangeStates:confirming_change":
            # Возврат к выбору нового места
            new_date = data.get('booking_date')
            available_places = db.get_available_places(new_date)

            await callback.message.answer(
                f"Выберите другое место на {new_date}:",
                reply_markup=get_places_keyboard(available_places)
            )

            await state.set_state(ChangeStates.waiting_for_new_place)

        await callback.answer()
    except Exception as e:
        logger.error(f"Error in change: {e}", exc_info=True)


# Обработчики броней
@router.callback_query(F.data.startswith("booking_"))
async def process_booking_action(callback: CallbackQuery, state: FSMContext):
    try:
        booking_id = int(callback.data.split("_")[1])
        user_id = callback.from_user.id
        current_state = await state.get_state()

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            await callback.message.answer("❌ Бронь не найдена.")
            await state.clear()
            await callback.answer()
            return

        if current_state == "CancelStates:selecting_booking":
            success = db.cancel_booking(booking_id, user_id)

            if success:
                await callback.message.answer(
                    f"✅ Бронь {booking['place_name']} на {booking['date']} отменена."
                )
            else:
                await callback.message.answer("❌ Ошибка при отмене.")

            await state.clear()

        elif current_state == "ChangeStates:selecting_booking":
            await state.update_data(old_booking_id=booking_id)

            now = datetime.now()
            await callback.message.answer(
                f"Текущая бронь: {booking['place_name']} на {booking['date']}\n\n"
                "Выберите новую дату:",
                reply_markup=get_calendar_keyboard(now.year, now.month)
            )

            await state.set_state(ChangeStates.waiting_for_new_date)

        elif current_state == "AdminStates:selecting_user_booking":
            success = db.cancel_booking_admin(booking_id)

            if success:
                await callback.message.answer(
                    f"✅ Бронь отменена: {booking['place_name']} на {booking['date']}"
                )
            else:
                await callback.message.answer("❌ Ошибка при отмене.")

            await state.clear()

        elif current_state == "AdminStates:change_for_user_date":
            await state.update_data(old_booking_id=booking_id)

            now = datetime.now()
            await callback.message.answer(
                f"Текущая бронь: {booking['place_name']} на {booking['date']}\n\n"
                "Выберите новую дату:",
                reply_markup=get_calendar_keyboard(now.year, now.month)
            )

            await state.set_state("AdminStates:change_for_user_place")

        await callback.answer()
    except Exception as e:
        logger.error(f"Error in booking action: {e}", exc_info=True)


# АДМИН-ПАНЕЛЬ
@router.message(F.text == "⚙️ АДМИН-ПАНЕЛЬ")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора.")
        return

    await message.answer(
        "🔑 <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_all_bookings")
async def admin_show_all_bookings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    bookings = db.get_all_bookings()

    if not bookings:
        await callback.message.answer("📋 Активных броней нет.")
        await callback.answer()
        return

    # Группируем брони по пользователям
    from collections import defaultdict
    bookings_by_user = defaultdict(list)

    for booking in bookings:
        bookings_by_user[booking['user_id']].append(booking)

    # Формируем текст порциями
    messages = []
    current_message = "📋 <b>Все активные брони:</b>\n\n"
    total_users = len(bookings_by_user)
    total_bookings = len(bookings)

    for user_id, user_bookings in bookings_by_user.items():
        # Берём имя пользователя из первой брони
        first_booking = user_bookings[0]
        user_display = f"@{first_booking['username']}" if first_booking['username'] else first_booking['first_name']

        # Подсчитываем количество броней
        booking_count = len(user_bookings)
        booking_word = "бронь" if booking_count == 1 else ("брони" if 2 <= booking_count <= 4 else "броней")

        user_header = f"👤 {user_display} ({booking_count} {booking_word})\n  "

        # Формируем список броней в одну строку
        booking_items = []
        for booking in sorted(user_bookings, key=lambda x: x['date']):
            # Короткая дата (ДД.ММ)
            date_parts = booking['date'].split('.')
            short_date = f"{date_parts[0]}.{date_parts[1]}"

            # Номер места
            place_num = booking['place_id']

            # Добавляем значок для постоянной брони
            perm_marker = " 📌" if booking.get('booking_type') == 'permanent' else ""

            booking_items.append(f"{short_date} → №{place_num}{perm_marker}")

        bookings_line = ", ".join(booking_items)
        user_block = user_header + bookings_line + "\n\n"

        # Проверяем, не превысит ли добавление нового пользователя лимит
        if len(current_message + user_block) > 3800:
            messages.append(current_message)
            current_message = "📋 <b>Все активные брони (продолжение):</b>\n\n"

        current_message += user_block

    # Добавляем итоговую статистику
    footer = f"━━━━━━━━━━━━━━━━━━━\n📊 {total_users} пользователя • {total_bookings} броней"

    if len(current_message + footer) > 3800:
        messages.append(current_message)
        current_message = footer
    else:
        current_message += footer

    # Добавляем последнее сообщение
    if current_message.strip():
        messages.append(current_message)

    # Отправляем все части
    for msg in messages:
        await callback.message.answer(msg, parse_mode="HTML")

    await callback.answer()


@router.callback_query(F.data == "admin_cancel_all")
async def admin_cancel_all_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отменить все", callback_data="admin_cancel_all_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_action")
        ]
    ])

    await callback.message.answer(
        "⚠️ <b>Внимание!</b>\n\nВы уверены, что хотите отменить ВСЕ брони?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel_all_confirm")
async def admin_cancel_all_execute(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    count = db.cancel_all_bookings()
    await callback.message.answer(
        f"✅ <b>Отменено записей: {count}</b>\n\n"
        f"Включая обычные и постоянные брони.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel_user")
async def admin_cancel_user_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "🔍 Введите Telegram ID или username пользователя\n"
        "Например: <code>123456</code> или <code>@username</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_user_identifier)
    await callback.answer()


@router.message(AdminStates.waiting_for_user_identifier)
async def admin_process_user_identifier(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            return

    bookings = db.get_user_bookings(user_id)

    if not bookings:
        await message.answer("У этого пользователя нет активных броней.")
        await state.clear()
        return

    await state.update_data(target_user_id=user_id)

    await message.answer(
        "Выберите бронь для отмены:",
        reply_markup=get_bookings_keyboard(bookings)
    )
    await state.set_state(AdminStates.selecting_user_booking)


@router.callback_query(F.data == "admin_book_for_user")
async def admin_book_for_user_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "🔍 Введите Telegram ID или username пользователя\n"
        "Например: <code>123456</code> или <code>@username</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.booking_for_user_date)
    await callback.answer()


@router.message(AdminStates.booking_for_user_date)
async def admin_book_for_user_get_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            return

    await state.update_data(target_user_id=user_id)

    now = datetime.now()
    await message.answer(
        f"Бронирование для пользователя ID: {user_id}\nВыберите дату:",
        reply_markup=get_calendar_keyboard(now.year, now.month)
    )


@router.callback_query(F.data == "admin_change_for_user")
async def admin_change_for_user_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "🔍 Введите Telegram ID или username пользователя\n"
        "Например: <code>123456</code> или <code>@username</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.change_for_user_select)
    await callback.answer()


@router.message(AdminStates.change_for_user_select)
async def admin_change_for_user_select_booking(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            return

    bookings = db.get_user_bookings(user_id)

    if not bookings:
        await message.answer("У этого пользователя нет активных броней.")
        await state.clear()
        return

    await state.update_data(target_user_id=user_id)

    await message.answer(
        "Выберите бронь для изменения:",
        reply_markup=get_bookings_keyboard(bookings)
    )
    await state.set_state(AdminStates.change_for_user_date)


# НОВАЯ ФУНКЦИЯ: Замена карты офиса
@router.callback_query(F.data == "admin_change_map")
async def admin_change_map_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Показываем текущую карту, если она есть
    if os.path.exists(OFFICE_MAP_PATH):
        try:
            photo = FSInputFile(OFFICE_MAP_PATH)
            await callback.message.answer_photo(
                photo=photo,
                caption="📸 <b>Текущая карта офиса</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error showing current map: {e}")

    await callback.message.answer(
        "🗺️ <b>Замена карты офиса</b>\n\n"
        "Отправьте новое изображение карты офиса.\n"
        "Принимаются только фото (PNG, JPG).\n\n"
        "Для отмены напишите /cancel",
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.waiting_for_map_photo)
    await callback.answer()


@router.message(AdminStates.waiting_for_map_photo)
async def admin_change_map_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    # Проверка на команду отмены
    if message.text == "/cancel":
        await message.answer("❌ Замена карты отменена.")
        await state.clear()
        return

    # Проверяем, что это фото или документ
    if not message.photo and not message.document:
        await message.answer(
            "⚠️ <b>Неверный формат</b>\n\n"
            "Пожалуйста, отправьте <b>фото или файл изображения</b>.\n"
            "Поддерживаются форматы: JPG, PNG, HEIC, WEBP\n\n"
            "Для отмены напишите /cancel",
            parse_mode="HTML"
        )
        return

    try:
        await message.answer("⏳ Загружаю новую карту...")

        # Создаём резервную копию старой карты
        if os.path.exists(OFFICE_MAP_PATH):
            backup_path = f"office_map_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            shutil.copy2(OFFICE_MAP_PATH, backup_path)
            logger.info(f"Backup created: {backup_path}")

        # Получаем файл
        if message.photo:
            # Если отправлено как фото (сжатое)
            photo = message.photo[-1]  # Берём максимальное разрешение
            file = await bot.get_file(photo.file_id)
            logger.info(f"Received photo: {file.file_path}")
        elif message.document:
            # Если отправлено как документ (без сжатия)
            doc = message.document

            # Проверяем mime-type
            allowed_types = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif']
            if doc.mime_type not in allowed_types:
                await message.answer(
                    f"❌ <b>Неподдерживаемый формат файла</b>\n\n"
                    f"Получен: {doc.mime_type}\n"
                    f"Поддерживаются только изображения (JPG, PNG, HEIC, WEBP)",
                    parse_mode="HTML"
                )
                return

            file = await bot.get_file(doc.file_id)
            logger.info(f"Received document: {file.file_path}, mime: {doc.mime_type}")

        # Скачиваем файл во временное место
        temp_path = f"temp_map_{message.from_user.id}.tmp"
        await bot.download_file(file.file_path, temp_path)

        # Переименовываем во финальное имя
        if os.path.exists(temp_path):
            shutil.move(temp_path, OFFICE_MAP_PATH)
            logger.info(f"Office map updated by admin {message.from_user.id}")

        # Показываем новую карту
        new_photo = FSInputFile(OFFICE_MAP_PATH)
        await message.answer_photo(
            photo=new_photo,
            caption="✅ <b>Карта офиса успешно обновлена!</b>\n\n"
                    "Новая карта будет отображаться при следующем бронировании.\n\n"
                    f"📊 Формат: {message.document.mime_type if message.document else 'JPEG (compressed)'}\n"
                    f"📁 Размер: {file.file_size / 1024:.1f} KB",
            parse_mode="HTML"
        )

        await state.clear()

    except Exception as e:
        logger.error(f"Error updating office map: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при обновлении карты</b>\n\n"
            f"Детали: {str(e)}\n\n"
            "Попробуйте отправить фото в другом формате или как документ.",
            parse_mode="HTML"
        )

        # Очищаем временный файл если он есть
        temp_path = f"temp_map_{message.from_user.id}.tmp"
        if os.path.exists(temp_path):
            os.remove(temp_path)

        await state.clear()


@router.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Получаем список админов с информацией
    admins_info = db.get_all_admins_with_info()

    admins_list = []
    for admin in admins_info:
        if admin['username']:
            admins_list.append(f"@{admin['username']} (ID: {admin['telegram_id']})")
        elif admin['first_name']:
            admins_list.append(f"{admin['first_name']} (ID: {admin['telegram_id']})")
        else:
            admins_list.append(f"ID: {admin['telegram_id']}")

    admins_text = "\n".join([f"• {info}" for info in admins_list])

    await callback.message.answer(
        f"👤 <b>Добавление администратора</b>\n\n"
        f"Текущие админы:\n{admins_text}\n\n"
        f"Введите Telegram ID или @username нового администратора:\n"
        f"Примеры: <code>123456789</code> или <code>@username</code>\n\n"
        f"⚠️ Если указываете username, пользователь должен сначала запустить бота командой /start",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.adding_admin)
    await callback.answer()


@router.message(AdminStates.adding_admin)
async def admin_add_admin_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    # Пытаемся распознать ID или username
    try:
        # Если это число - это ID
        new_admin_id = int(identifier)
    except ValueError:
        # Если не число - это username
        new_admin_id = db.find_user_by_username(identifier)
        if not new_admin_id:
            await message.answer(
                f"❌ <b>Пользователь не найден</b>\n\n"
                f"Возможные причины:\n"
                f"• Неверный username\n"
                f"• Пользователь ещё не запускал бота\n\n"
                f"💡 Попросите пользователя сначала запустить бота командой /start, "
                f"затем попробуйте снова или используйте Telegram ID.",
                parse_mode="HTML"
            )
            await state.clear()
            return

    # Проверяем, не является ли уже админом
    if new_admin_id in ADMIN_IDS:
        await message.answer(f"❌ Пользователь {new_admin_id} уже является администратором.")
    else:
        # Добавляем в БД
        success = db.add_admin(new_admin_id, message.from_user.id)

        if success:
            # Добавляем в список в памяти
            ADMIN_IDS.append(new_admin_id)

            await message.answer(
                f"✅ <b>Администратор добавлен!</b>\n\n"
                f"👤 Telegram ID: <code>{new_admin_id}</code>\n\n"
                f"Права вступили в силу немедленно!",
                parse_mode="HTML"
            )
            logger.info(f"Admin {new_admin_id} added by {message.from_user.id}")
        else:
            await message.answer("❌ Ошибка при добавлении администратора.")

    await state.clear()


@router.callback_query(F.data == "admin_remove_admin")
async def admin_remove_admin_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Получаем список админов с информацией (кроме главного)
    admins_info = db.get_all_admins_with_info()
    removable_admins = [a for a in admins_info if a['telegram_id'] != SUPER_ADMIN_ID]

    if not removable_admins:
        await callback.answer("Нет админов для удаления (мама бота защищена)", show_alert=True)
        return

    admins_list = []
    for admin in removable_admins:
        if admin['username']:
            admins_list.append(f"• @{admin['username']} (ID: <code>{admin['telegram_id']}</code>)")
        elif admin['first_name']:
            admins_list.append(f"• {admin['first_name']} (ID: <code>{admin['telegram_id']}</code>)")
        else:
            admins_list.append(f"• ID: <code>{admin['telegram_id']}</code>")

    admins_text = "\n".join(admins_list)

    # Информация о главном админе
    super_admin_info = next((a for a in admins_info if a['telegram_id'] == SUPER_ADMIN_ID), None)
    if super_admin_info and super_admin_info['username']:
        super_display = f"@{super_admin_info['username']}"
    elif super_admin_info and super_admin_info['first_name']:
        super_display = super_admin_info['first_name']
    else:
        super_display = f"ID: {SUPER_ADMIN_ID}"

    await callback.message.answer(
        f"🗑 <b>Удаление администратора</b>\n\n"
        f"Админы (доступны для удаления):\n{admins_text}\n\n"
        f"⚠️ <b>Мама бота</b> ({super_display}, ID: <code>{SUPER_ADMIN_ID}</code>) защищена\n\n"
        f"Введите Telegram ID или @username для удаления:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.removing_admin)
    await callback.answer()


@router.message(AdminStates.removing_admin)
async def admin_remove_admin_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    # Пытаемся распознать ID или username
    try:
        # Если это число - это ID
        remove_admin_id = int(identifier)
    except ValueError:
        # Если не число - это username
        remove_admin_id = db.find_user_by_username(identifier)
        if not remove_admin_id:
            await message.answer(
                f"❌ <b>Пользователь не найден</b>\n\n"
                f"Проверьте правильность username или используйте Telegram ID.",
                parse_mode="HTML"
            )
            await state.clear()
            return

    # Проверки
    if remove_admin_id == SUPER_ADMIN_ID:
        await message.answer(f"❌ Нельзя удалить маму бота (ID: {SUPER_ADMIN_ID})!")
    elif remove_admin_id not in ADMIN_IDS:
        await message.answer(f"❌ Пользователь {remove_admin_id} не является администратором.")
    else:
        # Удаляем из БД
        success = db.remove_admin(remove_admin_id)

        if success:
            # Удаляем из списка в памяти
            ADMIN_IDS.remove(remove_admin_id)

            await message.answer(
                f"✅ <b>Администратор удалён!</b>\n\n"
                f"👤 Telegram ID: <code>{remove_admin_id}</code>\n\n"
                f"Права отозваны немедленно!",
                parse_mode="HTML"
            )
            logger.info(f"Admin {remove_admin_id} removed by {message.from_user.id}")
        else:
            await message.answer("❌ Ошибка при удалении администратора.")

    await state.clear()


@router.callback_query(F.data == "admin_cancel_action")
async def admin_cancel_action(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer("Действие отменено")


# ПОСТОЯННЫЕ БРОНИ
@router.callback_query(F.data == "admin_permanent_menu")
async def admin_permanent_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.edit_text(
        "📌 <b>Постоянные брони</b>\n\n"
        "Управление постоянными бронированиями мест.",
        reply_markup=get_permanent_bookings_menu(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_back_to_main")
async def admin_back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔑 <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_create_permanent")
async def admin_create_permanent_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "➕ <b>Создание постоянной брони</b>\n\n"
        "Введите Telegram ID или @username пользователя:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.permanent_user_id)
    await callback.answer()


@router.message(AdminStates.permanent_user_id)
async def admin_permanent_get_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            return

    await state.update_data(permanent_user_id=user_id)

    await message.answer(
        f"👤 Пользователь: ID {user_id}\n\n"
        "Введите номер места (1-13):",
    )
    await state.set_state(AdminStates.permanent_place_id)


@router.message(AdminStates.permanent_place_id)
async def admin_permanent_get_place(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    try:
        place_id = int(message.text.strip())
        if place_id < 1 or place_id > TOTAL_PLACES:
            await message.answer(f"❌ Номер места должен быть от 1 до {TOTAL_PLACES}")
            return
    except ValueError:
        await message.answer("❌ Введите число от 1 до 13")
        return

    await state.update_data(permanent_place_id=place_id)

    await message.answer(
        f"🪑 Место №{place_id}\n\n"
        "Выберите дни недели для постоянной брони:",
        reply_markup=get_weekday_keyboard([])
    )
    await state.set_state(AdminStates.permanent_days)


@router.callback_query(AdminStates.permanent_days, F.data.startswith("weekday_"))
async def admin_permanent_toggle_day(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get('selected_weekdays', [])

    action = callback.data.split("_")[1]

    if action == "confirm":
        if not selected:
            await callback.answer("⚠️ Выберите хотя бы один день!", show_alert=True)
            return

        user_id = data.get('permanent_user_id')
        place_id = data.get('permanent_place_id')

        weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        days_text = ", ".join([weekday_names[d] for d in sorted(selected)])

        await callback.message.edit_text(
            f"📌 <b>Подтверждение постоянной брони</b>\n\n"
            f"👤 Пользователь: ID {user_id}\n"
            f"🪑 Место: №{place_id}\n"
            f"📅 Дни: {days_text}\n\n"
            f"Создать постоянную бронь на ближайшие 60 дней?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Создать", callback_data="permanent_create_confirm"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="permanent_create_cancel")
                ]
            ]),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.permanent_confirm)
        await callback.answer()
        return

    elif action == "cancel":
        await callback.message.delete()
        await callback.answer("Отменено")
        await state.clear()
        return

    # Toggle день
    try:
        day = int(action)
        if day in selected:
            selected.remove(day)
        else:
            selected.append(day)

        await state.update_data(selected_weekdays=selected)

        await callback.message.edit_reply_markup(
            reply_markup=get_weekday_keyboard(selected)
        )
        await callback.answer()
    except:
        await callback.answer()


@router.callback_query(F.data == "permanent_create_confirm")
async def admin_permanent_create_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    user_id = data.get('permanent_user_id')
    place_id = data.get('permanent_place_id')
    weekdays = data.get('selected_weekdays', [])

    success = db.create_permanent_booking(callback.from_user.id, user_id, place_id, weekdays)

    if success:
        weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        days_text = ", ".join([weekday_names[d] for d in sorted(weekdays)])

        await callback.message.edit_text(
            f"✅ <b>Постоянная бронь создана!</b>\n\n"
            f"👤 Пользователь: ID {user_id}\n"
            f"🪑 Место: №{place_id}\n"
            f"📅 Дни: {days_text}\n\n"
            f"Автоматически созданы брони на ближайшие 60 дней.",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при создании постоянной брони</b>\n\n"
            "Возможные причины:\n"
            "• У этого пользователя уже есть постоянная бронь на это место\n"
            "• Другой пользователь уже забронировал это место на пересекающиеся дни\n"
            "• Место уже занято на выбранные дни недели\n\n"
            "Проверьте существующие постоянные брони через меню.",
            parse_mode="HTML"
        )

    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "permanent_create_cancel")
async def admin_permanent_create_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.answer("Отменено")
    await state.clear()


@router.callback_query(F.data == "admin_view_all_permanent")
async def admin_view_all_permanent(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    permanent_bookings = db.get_permanent_bookings()

    if not permanent_bookings:
        await callback.message.answer("📋 Постоянных броней нет.")
        await callback.answer()
        return

    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    text = "📌 <b>Все постоянные брони:</b>\n\n"
    for pb in permanent_bookings:
        user_display = f"@{pb['username']}" if pb['username'] else pb['first_name']
        days_text = ", ".join([weekday_names[d] for d in sorted(pb['weekdays'])])
        text += (f"• ID {pb['id']}: <b>{pb['place_name']}</b>\n"
                 f"  👤 {user_display} (ID: {pb['user_id']})\n"
                 f"  📅 {days_text}\n\n")

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_view_user_permanent")
async def admin_view_user_permanent_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "🔍 Введите Telegram ID или @username пользователя:",
    )
    await state.set_state(AdminStates.view_permanent_user)
    await callback.answer()


@router.message(AdminStates.view_permanent_user)
async def admin_view_user_permanent_show(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            await state.clear()
            return

    permanent_bookings = db.get_permanent_bookings(user_id)

    if not permanent_bookings:
        await message.answer(f"У пользователя {user_id} нет постоянных броней.")
        await state.clear()
        return

    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    text = f"📌 <b>Постоянные брони пользователя {user_id}:</b>\n\n"
    for pb in permanent_bookings:
        days_text = ", ".join([weekday_names[d] for d in sorted(pb['weekdays'])])
        text += (f"• ID {pb['id']}: <b>{pb['place_name']}</b>\n"
                 f"  📅 {days_text}\n\n")

    await message.answer(text, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "admin_delete_permanent")
async def admin_delete_permanent_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        "🗑️ <b>Удаление постоянной брони</b>\n\n"
        "Введите Telegram ID или @username пользователя:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.delete_permanent_user)
    await callback.answer()


@router.message(AdminStates.delete_permanent_user)
async def admin_delete_permanent_select(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    identifier = message.text.strip()

    try:
        user_id = int(identifier)
    except ValueError:
        user_id = db.find_user_by_username(identifier)
        if not user_id:
            await message.answer("❌ Пользователь не найден.")
            await state.clear()
            return

    permanent_bookings = db.get_permanent_bookings(user_id)

    if not permanent_bookings:
        await message.answer(f"У пользователя {user_id} нет постоянных броней.")
        await state.clear()
        return

    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    buttons = []
    for pb in permanent_bookings:
        days_text = ", ".join([weekday_names[d] for d in sorted(pb['weekdays'])])
        buttons.append([InlineKeyboardButton(
            text=f"{pb['place_name']} ({days_text})",
            callback_data=f"delete_perm_{pb['id']}"
        )])

    await message.answer(
        "Выберите постоянную бронь для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(AdminStates.delete_permanent_select)


@router.callback_query(AdminStates.delete_permanent_select, F.data.startswith("delete_perm_"))
async def admin_delete_permanent_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    permanent_id = int(callback.data.split("_")[2])

    success = db.delete_permanent_booking(permanent_id)

    if success:
        await callback.message.edit_text(
            f"✅ Постоянная бронь ID {permanent_id} удалена!\n\n"
            "Все будущие брони по этому расписанию также отменены."
        )
    else:
        await callback.message.edit_text("❌ Ошибка при удалении.")

    await state.clear()
    await callback.answer()


# Главная функция
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())