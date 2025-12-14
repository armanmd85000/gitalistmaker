import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Union

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("list-maker")

# t.me/username/123 OR t.me/c/123456789/123
LINK_RE = re.compile(r"(?:https?://)?t\.me/(c/)?([^/\s]+)/(\d+)", re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.(me|dog)/\S+)", re.IGNORECASE)

ChatRef = Union[int, str]  # -100... or username


def parse_tme_link(link: str) -> Tuple[ChatRef, int]:
    m = LINK_RE.search(link.strip())
    if not m:
        raise ValueError("Invalid t.me link")
    is_c = bool(m.group(1))
    chat_part = m.group(2)
    msg_id = int(m.group(3))
    if is_c:
        internal = int(chat_part)
        chat_id = int(f"-100{internal}")
        return chat_id, msg_id
    else:
        return chat_part, msg_id  # username, msg_id


async def resolve_chat_id(app: Client, chat_ref: ChatRef) -> int:
    if isinstance(chat_ref, int):
        return chat_ref
    chat = await app.get_chat(chat_ref)
    return chat.id


async def resolve_username(app: Client, chat_id: int) -> Optional[str]:
    chat = await app.get_chat(chat_id)
    return chat.username


def clean_caption(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = URL_IN_TEXT_RE.sub("", t)      # remove URLs
    t = re.sub(r"\s+", " ", t).strip() # normalize whitespace
    return t.casefold()


def make_post_link(chat_username: Optional[str], chat_id: int, msg_id: int) -> str:
    if chat_username:
        return f"https://t.me/{chat_username}/{msg_id}"
    internal = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{internal}/{msg_id}"


async def iter_range(app: Client, chat_id: int, start_id: int, end_id: int, chunk: int = 200):
    lo, hi = min(start_id, end_id), max(start_id, end_id)
    for base in range(lo, hi + 1, chunk):
        ids = list(range(base, min(base + chunk, hi + 1)))
        msgs = await app.get_messages(chat_id, ids)
        for m in msgs:
            if m and not m.empty:
                yield m


async def build_a_index(app: Client, chat_a: int, start_a: int, end_a: int) -> Dict[str, int]:
    index: Dict[str, int] = {}
    async for m in iter_range(app, chat_a, start_a, end_a):
        if not m.photo:
            continue
        key = clean_caption(m.caption or "")
        if key and key not in index:
            index[key] = m.id
    return index


async def safe_copy(app: Client, from_chat: int, msg_id: int, to_chat: ChatRef, caption: str):
    try:
        await app.copy_message(to_chat, from_chat, msg_id, caption=caption)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await app.copy_message(to_chat, from_chat, msg_id, caption=caption)


@dataclass
class State:
    source_x: Optional[ChatRef] = None
    target_a: Optional[ChatRef] = None
    target_b: Optional[ChatRef] = None

    x_start: Optional[str] = None
    x_end: Optional[str] = None
    a_start: Optional[str] = None
    a_end: Optional[str] = None

    waiting_for: Optional[str] = None  # tracks what input we expect next


STATE = State()


def owner_only(_, __, m: Message) -> bool:
    return bool(m.from_user and m.from_user.id == Config.OWNER_ID)


app = Client(
    "list_userbot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    session_string=Config.SESSION_STRING
)


@app.on_message(filters.command("start") & filters.create(owner_only))
async def cmd_start(_, message: Message):
    text = (
        "✅ **List Maker Userbot** (photos only)\n\n"
        "Step 1: Set chats\n"
        "• `/setsourcelist <username_or_-100id>`  (Source X)\n"
        "• `/settargeta <username_or_-100id>`     (Target A)\n"
        "• `/settargetb <username_or_-100id>`     (Target B)\n\n"
        "Step 2: Set ranges (post links)\n"
        "• `/setxrange` then send X first link, then X last link\n"
        "• `/setarange` then send A first link, then A last link\n\n"
        "Step 3: Run\n"
        "• `/run`\n"
        "• `/status` to check current settings\n"
        "• `/reset` to clear everything"
    )
    await message.reply(text)


def normalize_chat_ref(s: str) -> ChatRef:
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    if s.lstrip("-").isdigit():
        return int(s)
    return s


@app.on_message(filters.command("setsourcelist") & filters.create(owner_only))
async def cmd_set_source(_, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: `/setsourcelist <username_or_-100id>`")
    STATE.source_x = normalize_chat_ref(message.command[1])
    await message.reply("✅ Source list X set.")


@app.on_message(filters.command("settargeta") & filters.create(owner_only))
async def cmd_set_a(_, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: `/settargeta <username_or_-100id>`")
    STATE.target_a = normalize_chat_ref(message.command[1])
    await message.reply("✅ Target A set.")


@app.on_message(filters.command("settargetb") & filters.create(owner_only))
async def cmd_set_b(_, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: `/settargetb <username_or_-100id>`")
    STATE.target_b = normalize_chat_ref(message.command[1])
    await message.reply("✅ Target B set.")


@app.on_message(filters.command("setxrange") & filters.create(owner_only))
async def cmd_set_xrange(_, message: Message):
    if not STATE.source_x:
        return await message.reply("❌ Set Source X first using `/setsourcelist ...`")
    STATE.waiting_for = "x_first"
    STATE.x_start = STATE.x_end = None
    await message.reply("Send **Source X FIRST post link** now.")


@app.on_message(filters.command("setarange") & filters.create(owner_only))
async def cmd_set_arange(_, message: Message):
    if not STATE.target_a:
        return await message.reply("❌ Set Target A first using `/settargeta ...`")
    STATE.waiting_for = "a_first"
    STATE.a_start = STATE.a_end = None
    await message.reply("Send **Target A FIRST post link** now.")


@app.on_message(filters.command("status") & filters.create(owner_only))
async def cmd_status(_, message: Message):
    await message.reply(
        f"**Current Settings**\n"
        f"Source X: `{STATE.source_x}`\n"
        f"Target A: `{STATE.target_a}`\n"
        f"Target B: `{STATE.target_b}`\n\n"
        f"X range: `{STATE.x_start}` → `{STATE.x_end}`\n"
        f"A range: `{STATE.a_start}` → `{STATE.a_end}`\n"
    )


@app.on_message(filters.command("reset") & filters.create(owner_only))
async def cmd_reset(_, message: Message):
    global STATE
    STATE = State()
    await message.reply("✅ Reset done.")


@app.on_message(filters.text & filters.create(owner_only))
async def handle_text(_, message: Message):
    if not STATE.waiting_for:
        return

    txt = message.text.strip()

    if STATE.waiting_for == "x_first":
        STATE.x_start = txt
        STATE.waiting_for = "x_last"
        return await message.reply("Now send **Source X LAST post link**.")

    if STATE.waiting_for == "x_last":
        STATE.x_end = txt
        STATE.waiting_for = None
        return await message.reply("✅ Source X range set.\nNow set A range using `/setarange`.")

    if STATE.waiting_for == "a_first":
        STATE.a_start = txt
        STATE.waiting_for = "a_last"
        return await message.reply("Now send **Target A LAST post link**.")

    if STATE.waiting_for == "a_last":
        STATE.a_end = txt
        STATE.waiting_for = None
        return await message.reply("✅ Target A range set.\nNow run using `/run`.")


@app.on_message(filters.command("run") & filters.create(owner_only))
async def cmd_run(client: Client, message: Message):
    # Validate config
    if not (STATE.source_x and STATE.target_a and STATE.target_b):
        return await message.reply("❌ Set Source X, Target A, Target B first. Use `/start`.")
    if not (STATE.x_start and STATE.x_end and STATE.a_start and STATE.a_end):
        return await message.reply("❌ Set both ranges first: `/setxrange` and `/setarange`.")

    # Resolve X range links -> msg ids (chat in link is not trusted; we use the configured chat)
    try:
        _, x_start_id = parse_tme_link(STATE.x_start)
        _, x_end_id = parse_tme_link(STATE.x_end)
        _, a_start_id = parse_tme_link(STATE.a_start)
        _, a_end_id = parse_tme_link(STATE.a_end)
    except Exception as e:
        return await message.reply(f"❌ Link parse error: {e}")

    # Resolve chat ids
    chat_x = await resolve_chat_id(client, STATE.source_x)
    chat_a = await resolve_chat_id(client, STATE.target_a)
    chat_b = STATE.target_b

    a_username = await resolve_username(client, chat_a)

    progress = await message.reply("⏳ Building index from Target A (photos only)...")
    a_index = await build_a_index(client, chat_a, a_start_id, a_end_id)

    await progress.edit(f"✅ Indexed {len(a_index)} photo captions from A.\n⏳ Processing Source X photos...")

    processed = matched = not_found = 0

    async for x_msg in iter_range(client, chat_x, x_start_id, x_end_id):
        if not x_msg.photo:
            continue

        processed += 1
        x_key = clean_caption(x_msg.caption or "")
        if not x_key:
            continue

        a_mid = a_index.get(x_key)
        if not a_mid:
            not_found += 1
            continue

        a_msg = await client.get_messages(chat_a, a_mid)
        if not a_msg or a_msg.empty or not a_msg.photo:
            not_found += 1
            continue

        link = make_post_link(a_username, chat_a, a_mid)
        a_caption = (a_msg.caption or "").strip()
        final_caption = (a_caption + f"\n\n🔗 Link: {link}").strip()

        await safe_copy(client, chat_a, a_mid, chat_b, final_caption)
        matched += 1

        await asyncio.sleep(Config.DELAY_SECONDS)

    await progress.edit(
        "✅ **Done!**\n"
        f"Photos read from X-range: {processed}\n"
        f"Matched & sent to B: {matched}\n"
        f"No match in A-range: {not_found}"
    )


if __name__ == "__main__":
    app.run()
