import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, Union

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("multi-list-maker")

# Link formats:
# - https://t.me/username/123
# - https://t.me/c/123456789/123
LINK_RE = re.compile(r"(?:https?://)?t\.me/(c/)?([^/\s]+)/(\d+)", re.IGNORECASE)

# Remove URLs inside caption:
URL_IN_TEXT_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.(me|dog)/\S+)", re.IGNORECASE)

ChatRef = Union[int, str]  # -100... or username


def normalize_chat_ref(s: str) -> ChatRef:
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    if s.lstrip("-").isdigit():
        return int(s)
    return s


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
    """Remove URLs, normalize whitespace, case-insensitive."""
    if not text:
        return ""
    t = text.strip()
    t = URL_IN_TEXT_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.casefold()


def make_post_link(chat_username: Optional[str], chat_id: int, msg_id: int) -> str:
    """Public if username exists else /c/ private format."""
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


async def build_index_for_target(app: Client, chat_a: int, start_a: int, end_a: int) -> Dict[str, int]:
    """
    Index ONLY photos in target channel range:
      cleaned_caption -> FIRST message_id
    """
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
        log.warning(f"FloodWait {e.value}s — sleeping...")
        await asyncio.sleep(e.value)
        await app.copy_message(to_chat, from_chat, msg_id, caption=caption)


def owner_only(_, __, m: Message) -> bool:
    return bool(m.from_user and m.from_user.id == Config.OWNER_ID)


@dataclass
class TargetPair:
    target_a: Optional[ChatRef] = None
    target_list: Optional[ChatRef] = None
    a_start: Optional[str] = None
    a_end: Optional[str] = None


@dataclass
class State:
    source_x: Optional[ChatRef] = None
    x_start: Optional[str] = None
    x_end: Optional[str] = None

    # supports 2 targets as requested (can extend easily)
    targets: Dict[int, TargetPair] = field(default_factory=lambda: {1: TargetPair(), 2: TargetPair()})

    waiting_for: Optional[str] = None  # x_first/x_last/a_first_n/a_last_n


# Load defaults from env into state
STATE = State(
    source_x=normalize_chat_ref(Config.SOURCE_X) if Config.SOURCE_X else None,
)

# Apply env defaults for targets
if Config.TARGET1_A:
    STATE.targets[1].target_a = normalize_chat_ref(Config.TARGET1_A)
if Config.TARGET1_LIST:
    STATE.targets[1].target_list = normalize_chat_ref(Config.TARGET1_LIST)
if Config.TARGET2_A:
    STATE.targets[2].target_a = normalize_chat_ref(Config.TARGET2_A)
if Config.TARGET2_LIST:
    STATE.targets[2].target_list = normalize_chat_ref(Config.TARGET2_LIST)


app = Client(
    "multi_list_userbot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    session_string=Config.SESSION_STRING
)


def target_summary() -> str:
    lines = []
    for n in sorted(STATE.targets.keys()):
        t = STATE.targets[n]
        lines.append(
            f"**Target {n}:** A=`{t.target_a}`  LIST=`{t.target_list}`  "
            f"Range=({t.a_start} → {t.a_end})"
        )
    return "\n".join(lines)


@app.on_message(filters.command("start") & filters.create(owner_only))
async def cmd_start(_, message: Message):
    missing = []
    if not STATE.source_x:
        missing.append("Source X (use `/setsourcelist ...`)")

    for n in sorted(STATE.targets.keys()):
        t = STATE.targets[n]
        if not t.target_a:
            missing.append(f"Target{n} A (use `/settarget {n} ...`)")
        if not t.target_list:
            missing.append(f"Target{n} LIST (use `/setlist {n} ...`)")

    help_text = (
        "✅ **Multi-Target List Maker Userbot** (photos only)\n\n"
        "**Set chats**\n"
        "• `/setsourcelist <username_or_-100id>`\n"
        "• `/settarget <n> <username_or_-100id>`   (Target A for n)\n"
        "• `/setlist <n> <username_or_-100id>`     (Target List for n)\n\n"
        "**Set ranges**\n"
        "• `/setxrange`  (then send X first link, then X last link)\n"
        "• `/setarange <n>` (then send A first link, then A last link for target n)\n\n"
        "**Run**\n"
        "• `/run`  (processes X range; for each X photo tries Target1 then Target2)\n"
        "• `/status`  |  `/reset`\n\n"
        f"{target_summary()}\n"
    )

    if missing:
        await message.reply(
            help_text +
            "\n⚠️ **Missing:**\n- " + "\n- ".join(missing)
        )
    else:
        await message.reply(help_text + "\n✅ All channels are set. Now set ranges and run.")


@app.on_message(filters.command("setsourcelist") & filters.create(owner_only))
async def cmd_setsourcelist(_, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: `/setsourcelist <username_or_-100id>`")
    STATE.source_x = normalize_chat_ref(message.command[1])
    await message.reply("✅ Source X set.")


@app.on_message(filters.command("settarget") & filters.create(owner_only))
async def cmd_settarget(_, message: Message):
    if len(message.command) < 3:
        return await message.reply("Usage: `/settarget <n> <username_or_-100id>`")
    n = int(message.command[1])
    if n not in STATE.targets:
        return await message.reply("❌ Only targets 1 and 2 are supported in this version.")
    STATE.targets[n].target_a = normalize_chat_ref(message.command[2])
    await message.reply(f"✅ Target {n} A set.")


@app.on_message(filters.command("setlist") & filters.create(owner_only))
async def cmd_setlist(_, message: Message):
    if len(message.command) < 3:
        return await message.reply("Usage: `/setlist <n> <username_or_-100id>`")
    n = int(message.command[1])
    if n not in STATE.targets:
        return await message.reply("❌ Only targets 1 and 2 are supported in this version.")
    STATE.targets[n].target_list = normalize_chat_ref(message.command[2])
    await message.reply(f"✅ Target {n} LIST set.")


@app.on_message(filters.command("setxrange") & filters.create(owner_only))
async def cmd_setxrange(_, message: Message):
    if not STATE.source_x:
        return await message.reply("❌ Set Source X first: `/setsourcelist ...`")
    STATE.waiting_for = "x_first"
    STATE.x_start = STATE.x_end = None
    await message.reply("Send **Source X FIRST post link** now.")


@app.on_message(filters.command("setarange") & filters.create(owner_only))
async def cmd_setarange(_, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: `/setarange <n>`")
    n = int(message.command[1])
    if n not in STATE.targets:
        return await message.reply("❌ Only targets 1 and 2 are supported.")
    if not STATE.targets[n].target_a:
        return await message.reply(f"❌ Set Target {n} A first using `/settarget {n} ...`")

    STATE.waiting_for = f"a_first_{n}"
    STATE.targets[n].a_start = STATE.targets[n].a_end = None
    await message.reply(f"Send **Target {n} A FIRST post link** now.")


@app.on_message(filters.command("status") & filters.create(owner_only))
async def cmd_status(_, message: Message):
    await message.reply(
        f"**Source X:** `{STATE.source_x}`\n"
        f"**X range:** `{STATE.x_start}` → `{STATE.x_end}`\n\n"
        f"{target_summary()}"
    )


@app.on_message(filters.command("reset") & filters.create(owner_only))
async def cmd_reset(_, message: Message):
    global STATE
    STATE = State()
    await message.reply("✅ Reset done. Use `/start` again.")


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
        return await message.reply("✅ Source X range set.\nNow set Target ranges using `/setarange 1` and `/setarange 2`.")

    # Target ranges
    if STATE.waiting_for.startswith("a_first_"):
        n = int(STATE.waiting_for.split("_")[-1])
        STATE.targets[n].a_start = txt
        STATE.waiting_for = f"a_last_{n}"
        return await message.reply(f"Now send **Target {n} A LAST post link**.")

    if STATE.waiting_for.startswith("a_last_"):
        n = int(STATE.waiting_for.split("_")[-1])
        STATE.targets[n].a_end = txt
        STATE.waiting_for = None
        return await message.reply(f"✅ Target {n} range set.\nWhen ready, run `/run`.")


@app.on_message(filters.command("run") & filters.create(owner_only))
async def cmd_run(client: Client, message: Message):
    # Validate
    if not STATE.source_x:
        return await message.reply("❌ Source X missing. Set with `/setsourcelist ...`")
    if not (STATE.x_start and STATE.x_end):
        return await message.reply("❌ X range missing. Set with `/setxrange`")

    for n in sorted(STATE.targets.keys()):
        t = STATE.targets[n]
        if not (t.target_a and t.target_list):
            return await message.reply(f"❌ Target {n} A or LIST missing. Use `/settarget {n}` and `/setlist {n}`")
        if not (t.a_start and t.a_end):
            return await message.reply(f"❌ Target {n} range missing. Use `/setarange {n}`")

    # Parse message IDs from links (we trust configured chats, link chat part is ignored)
    try:
        _, x_start_id = parse_tme_link(STATE.x_start)
        _, x_end_id = parse_tme_link(STATE.x_end)
    except Exception as e:
        return await message.reply(f"❌ X link parse error: {e}")

    target_specs = {}
    for n in sorted(STATE.targets.keys()):
        t = STATE.targets[n]
        try:
            _, a_start_id = parse_tme_link(t.a_start)
            _, a_end_id = parse_tme_link(t.a_end)
        except Exception as e:
            return await message.reply(f"❌ Target {n} A link parse error: {e}")
        target_specs[n] = (a_start_id, a_end_id)

    # Resolve chat IDs
    chat_x = await resolve_chat_id(client, STATE.source_x)

    # Build indexes per target
    progress = await message.reply("⏳ Building indexes for targets (photos only)...")

    indexes: Dict[int, Dict[str, int]] = {}
    a_chat_ids: Dict[int, int] = {}
    a_usernames: Dict[int, Optional[str]] = {}

    for n in sorted(STATE.targets.keys()):
        t = STATE.targets[n]
        chat_a = await resolve_chat_id(client, t.target_a)  # type: ignore
        a_chat_ids[n] = chat_a
        a_usernames[n] = await resolve_username(client, chat_a)
        a_start_id, a_end_id = target_specs[n]

        idx = await build_index_for_target(client, chat_a, a_start_id, a_end_id)
        indexes[n] = idx
        await progress.edit(f"✅ Indexed Target {n}: {len(idx)} captions\n⏳ Building remaining...")

    await progress.edit("✅ Indexes ready.\n⏳ Processing Source X photos and forwarding matches...")

    processed_x = 0
    total_sent = {1: 0, 2: 0}
    total_not_found = {1: 0, 2: 0}

    async for x_msg in iter_range(client, chat_x, x_start_id, x_end_id):
        if not x_msg.photo:
            continue
        processed_x += 1
        key = clean_caption(x_msg.caption or "")
        if not key:
            continue

        # For each target pair, try match and send
        for n in sorted(STATE.targets.keys()):
            t = STATE.targets[n]
            idx = indexes[n]
            a_mid = idx.get(key)

            if not a_mid:
                total_not_found[n] += 1
                continue

            chat_a = a_chat_ids[n]
            a_msg = await client.get_messages(chat_a, a_mid)
            if not a_msg or a_msg.empty or not a_msg.photo:
                total_not_found[n] += 1
                continue

            link = make_post_link(a_usernames[n], chat_a, a_mid)
            a_caption = (a_msg.caption or "").strip()
            final_caption = (a_caption + f"\n\n🔗 Link: {link}").strip()

            await safe_copy(client, chat_a, a_mid, t.target_list, final_caption)  # type: ignore
            total_sent[n] += 1

            await asyncio.sleep(Config.DELAY_SECONDS)

    # Summary
    summary = (
        "✅ **DONE!**\n"
        f"Source X photos processed: {processed_x}\n\n"
        f"**Target 1:** sent={total_sent[1]}  no_match={total_not_found[1]}\n"
        f"**Target 2:** sent={total_sent[2]}  no_match={total_not_found[2]}\n"
    )
    await progress.edit(summary)


if __name__ == "__main__":
    app.run()
