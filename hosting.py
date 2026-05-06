import asyncio
import html
import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///hosting.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в переменных окружения")


class Base(DeclarativeBase):
    pass


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    welcome_bonus: Mapped[int] = mapped_column(Integer, default=1)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255))
    description_html: Mapped[str] = mapped_column(Text)
    purchase_text_html: Mapped[str] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="orders")


engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())


class AddProductState(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_purchase_text = State()


class BroadcastState(StatesGroup):
    waiting_content = State()


class ChangeBonusState(StatesGroup):
    waiting_bonus = State()


class GiveTokensState(StatesGroup):
    waiting_amount = State()


class SetTokensByIdState(StatesGroup):
    waiting_user_id = State()
    waiting_delta = State()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        s = await session.get(Settings, 1)
        if not s:
            session.add(Settings(id=1, welcome_bonus=1))
            await session.commit()


def main_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🖥 Получить сервер")
    kb.button(text="👤 Профиль")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def products_keyboard(products: list[Product]):
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=p.title, callback_data=f"product:{p.id}")
    kb.adjust(1)
    return kb.as_markup()


def buy_keyboard(product_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🪙 Купить за 1 токен", callback_data=f"buy:{product_id}")]
        ]
    )


def admin_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🚫 Админ: Бан")
    kb.button(text="✅ Админ: Разбан")
    kb.button(text="➕ Админ: Добавить товар")
    kb.button(text="📣 Админ: Рассылка")
    kb.button(text="🎁 Админ: Раздать токены всем")
    kb.button(text="🆔 Админ: Изм. токены по ID")
    kb.button(text="⚙️ Админ: Изменить welcome бонус")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    user = await session.get(User, tg_user.id)
    if user:
        user.username = tg_user.username
        user.full_name = tg_user.full_name
        await session.commit()
        return user

    settings = await session.get(Settings, 1)
    bonus = settings.welcome_bonus if settings else 1
    user = User(
        id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
        tokens=bonus,
    )
    session.add(user)
    await session.commit()
    return user


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(CommandStart())
async def cmd_start(message: Message):
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        if user.is_banned:
            await message.answer("🚫 Вы заблокированы.")
            return

        await message.answer(
            "👋 Добро пожаловать! Используйте кнопки ниже.",
            reply_markup=main_menu_keyboard(),
        )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return
    await message.answer("🛠 Панель администратора", reply_markup=admin_keyboard())


@dp.message(F.text == "🖥 Получить сервер")
async def get_server(message: Message):
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        if user.is_banned:
            await message.answer("🚫 Вы заблокированы.")
            return

        products = (await session.scalars(select(Product))).all()
        if not products:
            await message.answer("📭 Пока нет доступных серверов.")
            return
        await message.answer("🌐 Выберите сервер:", reply_markup=products_keyboard(products))


@dp.callback_query(F.data.startswith("product:"))
async def product_info(call: CallbackQuery):
    product_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            await call.answer("❌ Товар не найден", show_alert=True)
            return
        await call.message.answer(product.description_html, reply_markup=buy_keyboard(product.id))
        await call.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_product(call: CallbackQuery):
    product_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        user = await get_or_create_user(session, call.from_user)
        if user.is_banned:
            await call.answer("🚫 Вы заблокированы", show_alert=True)
            return

        product = await session.get(Product, product_id)
        if not product:
            await call.answer("❌ Товар не найден", show_alert=True)
            return

        if user.tokens < 1:
            await call.answer("🪙 Недостаточно токенов", show_alert=True)
            return

        user.tokens -= 1
        session.add(Order(user_id=user.id, product_id=product.id))
        await session.commit()
        await call.message.answer(product.purchase_text_html)
        await call.answer("✅ Покупка успешна")


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        orders = (
            await session.execute(
                select(Order, Product)
                .join(Product, Product.id == Order.product_id)
                .where(Order.user_id == user.id)
                .order_by(Order.created_at.desc())
                .limit(3)
            )
        ).all()

        lines = [f"<b>👤 Ваш профиль</b>", f"ID: <code>{user.id}</code>", f"Токены: <b>{user.tokens}</b>", "", "<b>📦 Последние 3 заказа:</b>"]
        if not orders:
            lines.append("— 😔 Нет заказов")
        else:
            for order, prod in orders:
                lines.append(f"— {html.escape(prod.title)} ({order.created_at:%Y-%m-%d %H:%M})")
        await message.answer("\n".join(lines))


@dp.message(F.text == "➕ Админ: Добавить товар")
async def admin_add_product(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AddProductState.waiting_title)
    await message.answer("✍️ Введите название товара (текст кнопки):")


@dp.message(AddProductState.waiting_title)
async def add_product_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AddProductState.waiting_description)
    await message.answer("📝 Введите описание товара (HTML поддерживается):")


@dp.message(AddProductState.waiting_description)
async def add_product_description(message: Message, state: FSMContext):
    await state.update_data(description=message.html_text or message.text)
    await state.set_state(AddProductState.waiting_purchase_text)
    await message.answer("💬 Введите текст после покупки (HTML поддерживается):")


@dp.message(AddProductState.waiting_purchase_text)
async def add_product_purchase_text(message: Message, state: FSMContext):
    data = await state.get_data()
    async with SessionLocal() as session:
        session.add(
            Product(
                title=data["title"],
                description_html=data["description"],
                purchase_text_html=message.html_text or message.text,
            )
        )
        await session.commit()
    await state.clear()
    await message.answer("✅ Товар добавлен")


@dp.message(F.text.in_({"🚫 Админ: Бан", "✅ Админ: Разбан"}))
async def admin_ban_prompt(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    action = "ban" if "Бан" in message.text else "unban"
    await state.set_state(SetTokensByIdState.waiting_user_id)
    await state.update_data(action=action)
    await message.answer("🆔 Введите ID пользователя:")


@dp.message(SetTokensByIdState.waiting_user_id)
async def process_user_id_action(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("action") in {"ban", "unban"}:
        if not message.text.isdigit():
            await message.answer("⚠️ ID должен быть числом")
            return
        user_id = int(message.text)
        async with SessionLocal() as session:
            user = await session.get(User, user_id)
            if not user:
                await message.answer("❌ Пользователь не найден")
            else:
                user.is_banned = data["action"] == "ban"
                await session.commit()
                await message.answer("✅ Готово")
        await state.clear()
        return

    await state.update_data(target_user_id=int(message.text))
    await state.set_state(SetTokensByIdState.waiting_delta)
    await message.answer("🪙 Введите число токенов (может быть отрицательным):")


@dp.message(F.text == "🆔 Админ: Изм. токены по ID")
async def admin_set_tokens_id(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(SetTokensByIdState.waiting_user_id)
    await state.update_data(action="token_delta")
    await message.answer("🆔 Введите ID пользователя:")


@dp.message(SetTokensByIdState.waiting_delta)
async def process_delta(message: Message, state: FSMContext):
    try:
        delta = int(message.text)
    except ValueError:
        await message.answer("⚠️ Введите целое число")
        return

    data = await state.get_data()
    uid = data["target_user_id"]
    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            await message.answer("❌ Пользователь не найден")
        else:
            user.tokens += delta
            await session.commit()
            await message.answer(f"Готово. Теперь у пользователя {user.tokens} токенов")
    await state.clear()


@dp.message(F.text == "🎁 Админ: Раздать токены всем")
async def admin_give_all_prompt(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(GiveTokensState.waiting_amount)
    await message.answer("❓ Сколько токенов добавить всем пользователям?")


@dp.message(GiveTokensState.waiting_amount)
async def admin_give_all(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
    except ValueError:
        await message.answer("⚠️ Введите целое число")
        return

    async with SessionLocal() as session:
        await session.execute(update(User).values(tokens=User.tokens + amount))
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Готово. Всем начислено: {amount}")


@dp.message(F.text == "⚙️ Админ: Изменить welcome бонус")
async def admin_change_bonus_prompt(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(ChangeBonusState.waiting_bonus)
    await message.answer("🎉 Введите новый welcome бонус:")


@dp.message(ChangeBonusState.waiting_bonus)
async def admin_change_bonus(message: Message, state: FSMContext):
    try:
        bonus = int(message.text)
    except ValueError:
        await message.answer("⚠️ Введите целое число")
        return

    async with SessionLocal() as session:
        settings = await session.get(Settings, 1)
        settings.welcome_bonus = bonus
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Welcome бонус изменён на {bonus}")


@dp.message(F.text == "📣 Админ: Рассылка")
async def admin_broadcast_prompt(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_content)
    await message.answer("📨 Отправьте сообщение для рассылки (текст/фото/видео, HTML поддерживается):")


@dp.message(BroadcastState.waiting_content)
async def admin_broadcast_send(message: Message, state: FSMContext):
    async with SessionLocal() as session:
        users = (await session.scalars(select(User.id).where(User.is_banned == False))).all()

    sent = 0
    failed = 0
    for uid in users:
        try:
            if message.photo:
                await bot.send_photo(uid, message.photo[-1].file_id, caption=message.html_text or message.caption)
            elif message.video:
                await bot.send_video(uid, message.video.file_id, caption=message.html_text or message.caption)
            else:
                await bot.send_message(uid, message.html_text or message.text)
            sent += 1
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(f"📊 Рассылка завершена. Отправлено: {sent}, ошибок: {failed}")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
