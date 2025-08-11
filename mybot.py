import os
import discord
import wavelink
import urllib.parse
import asyncio
import random
import sqlite3
import uvicorn
import datetime as dt
import hashlib
import edge_tts
import re


from contextlib import closing
from typing import Optional
from pathlib import Path
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from openai import OpenAI
from fastapi import FastAPI, Request
from collections import defaultdict


# ======================== 기본 설정 ========================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# 기존 랭킹(점수) 시스템 DB (그대로 유지)
DB_PATH = "scores.db"

TTS_CACHE_DIR = Path("/tmp/tts_cache")
TTS_CACHE_DIR.mkdir(exist_ok=True)

# 길드별 보이스 작업 락(동시 요청 충돌 방지)
voice_locks = defaultdict(asyncio.Lock)


# *** 중요: 토큰/키는 환경변수 사용 (반드시 재발급 후 세팅!) ***
MY_DISCORD_TOKEN_KEY = os.getenv("DISCORD_TOKEN")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "sk-REPLACE_ME"))

TRIGGER_CHANNEL_NAMES = ["칼바람 방 생성", "솔랭 방 생성", "방 생성"]

TEAM_TRIGGER_NAME = "팀 생성"

TEAM_PARENT_CATEGORIES = {"칼바내전", "협곡내전"}

temp_channels = {}
games = {}

POSITION_ROLES = ["탑", "정글", "미드", "원딜", "서폿"]
MEMBER_ROLES  = ["멤버", "지인"]

ENTER_QUIT = "입장-퇴장"

# 만들어진 팀 세트 추적
team_groups: dict[int, list[int]] = {}    # group_key(대표 채널ID) -> [채널ID...]
channel_to_group: dict[int, int] = {}     # 채널ID -> group_key
category_group: dict[int, int] = {}       # 카테고리ID -> group_key (중복 생성 방지)

print(wavelink.__version__)

# ================= 포인트/상점/AFK (신규) =================
# 새 파일명으로 사용하여 기존 points.db와 충돌 방지
POINTS_DB_PATH  = os.getenv("POINTS_DB_PATH", "points_v2.db")
POINTS_PER_BLOCK = 5
BLOCK_SECONDS    = 30 * 60  # 30분
AFK_SECONDS      = 60 * 60  # 60분
MUTE_GRACE_SECONDS = 60 * 60  # 뮤트/이어폰 2분 지속 시에만 AFK 이동

# 뮤트/이어폰 시작 시각 캐시(메모리)
mute_since = {}  # key: (guild_id, user_id) -> unix ts


def get_db():
    conn = sqlite3.connect(POINTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_points_db():
    with closing(get_db()) as db, db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            points     INTEGER NOT NULL DEFAULT 0,
            carry_sec  INTEGER NOT NULL DEFAULT 0,
            last_join  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            afk_channel_id INTEGER,
            log_channel_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS shop (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name     TEXT NOT NULL,
            price    INTEGER NOT NULL,
            stock    INTEGER
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            item_id  INTEGER NOT NULL,
            ts       INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS afk_watch (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            last_active INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

def ensure_user(db, guild_id: int, user_id: int):
    db.execute("INSERT OR IGNORE INTO users(guild_id, user_id) VALUES (?,?)", (guild_id, user_id))
    db.execute("INSERT OR IGNORE INTO afk_watch(guild_id, user_id, last_active) VALUES(?,?,?)",
               (guild_id, user_id, int(dt.datetime.utcnow().timestamp())))

def get_afk_channel_id(db, guild_id: int) -> Optional[int]:
    row = db.execute("SELECT afk_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row["afk_channel_id"] if row and row["afk_channel_id"] else None

def set_afk_channel_id(db, guild_id: int, channel_id: Optional[int]):
    db.execute(
        "INSERT INTO guild_settings(guild_id, afk_channel_id) VALUES(?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET afk_channel_id=excluded.afk_channel_id",
        (guild_id, channel_id)
    )

def mark_active(db, guild_id: int, user_id: int, now: Optional[int] = None):
    now = now or int(dt.datetime.utcnow().timestamp())
    db.execute("INSERT OR REPLACE INTO afk_watch(guild_id, user_id, last_active) VALUES(?,?,?)",
               (guild_id, user_id, now))

def grant_points_for_session(db, guild_id: int, user_id: int, extra_sec: int) -> tuple[int, int]:
    """
    포인트 지급 처리만 수행.
    return: (이번에 지급된 포인트, 지급 후 사용자 총 포인트)
    """
    ensure_user(db, guild_id, user_id)
    row = db.execute("SELECT carry_sec FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    carry = row["carry_sec"] if row else 0
    total = carry + max(0, extra_sec)
    blocks = total // BLOCK_SECONDS
    remainder = total % BLOCK_SECONDS

    awarded = 0
    if blocks > 0:
        awarded = blocks * POINTS_PER_BLOCK
        db.execute(
            "UPDATE users SET points=points+?, carry_sec=? WHERE guild_id=? AND user_id=?",
            (awarded, remainder, guild_id, user_id),
        )
    else:
        db.execute(
            "UPDATE users SET carry_sec=? WHERE guild_id=? AND user_id=?",
            (total, guild_id, user_id),
        )

    # 총 포인트 조회
    row = db.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    new_total = row["points"] if row else 0
    return awarded, new_total

def get_log_channel_obj(guild) -> discord.TextChannel | None:
    with closing(get_db()) as db:
        row = db.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id=?", (guild.id,)).fetchone()
    if not row or not row[0]:
        return None
    return guild.get_channel(row[0])

@bot.tree.command(name="set_log_channel", description="포인트 로그 채널 설정/해제")
@app_commands.default_permissions(administrator=True)
async def set_log_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None):
    with closing(get_db()) as db, db:
        db.execute(
            "INSERT INTO guild_settings(guild_id, log_channel_id) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
            (interaction.guild.id, channel.id if channel else None),
        )
    await interaction.response.send_message(
        f"📜 로그 채널: {channel.mention}" if channel else "📜 로그 채널 해제됨."
    )

    
# ===관리자포인트====

@bot.tree.command(name="points_add", description="(관리자) 해당 유저에게 포인트를 추가합니다.")
@app_commands.default_permissions(administrator=True)
async def points_add_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("추가할 포인트는 1 이상이어야 합니다.", ephemeral=True)
        return
    with closing(get_db()) as db, db:
        ensure_user(db, interaction.guild.id, member.id)
        db.execute("UPDATE users SET points = points + ? WHERE guild_id=? AND user_id=?",
                   (amount, interaction.guild.id, member.id))
        row = db.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?",
                         (interaction.guild.id, member.id)).fetchone()
    await interaction.response.send_message(f"✅ {member.display_name} 님에게 **+{amount}p** 추가 (현재 {row['points']}p)")

@bot.tree.command(name="points_remove", description="(관리자) 해당 유저의 포인트를 차감합니다.")
@app_commands.default_permissions(administrator=True)
async def points_remove_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("차감할 포인트는 1 이상이어야 합니다.", ephemeral=True)
        return
    with closing(get_db()) as db, db:
        ensure_user(db, interaction.guild.id, member.id)
        db.execute("UPDATE users SET points = MAX(points - ?, 0) WHERE guild_id=? AND user_id=?",
                   (amount, interaction.guild.id, member.id))
        row = db.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?",
                         (interaction.guild.id, member.id)).fetchone()
    await interaction.response.send_message(f"✅ {member.display_name} 님에게 **-{amount}p** 차감 (현재 {row['points']}p)")

@bot.tree.command(name="points_set", description="(관리자) 해당 유저의 포인트를 특정 값으로 설정합니다.")
@app_commands.default_permissions(administrator=True)
async def points_set_cmd(interaction: discord.Interaction, member: discord.Member, value: int):
    if value < 0:
        await interaction.response.send_message("설정 값은 0 이상이어야 합니다.", ephemeral=True)
        return
    with closing(get_db()) as db, db:
        ensure_user(db, interaction.guild.id, member.id)
        db.execute("UPDATE users SET points = ? WHERE guild_id=? AND user_id=?",
                   (value, interaction.guild.id, member.id))
    await interaction.response.send_message(f"✅ {member.display_name} 님의 포인트를 **{value}p** 로 설정했습니다.")


    
# ==============================================

async def tts_synthesize_to_file(text: str,
                                 voice: str = "ko-KR-SunHiNeural") -> str:
    """
    edge-tts로 텍스트를 mp3로 합성하고, 캐시 파일 경로를 반환.
    같은 텍스트는 캐시 히트로 즉시 재생.
    """
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    out = TTS_CACHE_DIR / f"{key}.mp3"
    if not out.exists():
        # 필요하면 rate="+10%", volume="+0%" 같은 파라미터도 전달 가능
        await edge_tts.Communicate(text, voice=voice).save(str(out))
    return str(out)

# ================= 기존 커맨드들 =================
@bot.command()
async def rps(ctx, opponent: discord.Member):
    if ctx.author.id == opponent.id:
        await ctx.send("자기 자신과는 할 수 없습니다.")
        return
    choices = ["가위", "바위", "보"]
    user_choice = random.choice(choices)
    bot_choice  = random.choice(choices)
    wins = {"가위": "보", "바위": "가위", "보": "바위"}
    if user_choice == bot_choice:
        result = "비겼습니다!"
    elif wins[user_choice] == bot_choice:
        result = f"{ctx.author.mention} 님이 이겼습니다!"
    else:
        result = f"{opponent.mention} 님이 이겼습니다!"
    await ctx.send(f"{ctx.author.mention} 님: {user_choice}\n{opponent.mention} 님: {bot_choice}\n결과: {result}")

# ================= on_ready (병합) =================
@bot.event
async def on_ready():
    init_points_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="기본 커맨드 : $? 　　　　　"
    ))
    # 보조 루프 스타트
    accrual_loop.start()
    afk_guard.start()
    print(f"{bot.user} 작동 중")

# ================ 활동 기록 (텍스트 치면 비활동 해제) ================
@bot.event
async def on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        with closing(get_db()) as db, db:
            ensure_user(db, message.guild.id, message.author.id)
            mark_active(db, message.guild.id, message.author.id)
    await bot.process_commands(message)

# ================ 음성 상태 업데이트 (병합) ================

def normalize_name(name: str) -> str:
    # 띄어쓰기/이모지/특수문자 제거 → '칼 바 내 전'도 '칼바내전'으로 인식
    return re.sub(r"\s+|[^\w가-힣]", "", name)

@bot.event
async def on_voice_state_update(member, before, after):
    gid, uid = member.guild.id, member.id
    now = int(dt.datetime.utcnow().timestamp())

    # --- (팀 생성 트리거) after가 '팀 생성' 이고 카테고리가 칼바/협곡이면 1~4팀 생성 ---
    if after.channel and after.channel.name == TEAM_TRIGGER_NAME:
        parent = after.channel.category
        if parent and normalize_name(parent.name) in {"칼바내전", "협곡내전"}:
            await create_team_set(parent, member, trigger_ch=after.channel)  # 트리거 채널 지우려면 이 인자 필수

    # --- (팀 세트 자동 정리) 누가 나가거나 이동할 때마다, 이전/이후 채널 모두 확인 ---
    if before.channel:
        await maybe_cleanup_team_set(member.guild, before.channel.id)
    if after.channel:
        await maybe_cleanup_team_set(member.guild, after.channel.id)

    # ----- 아래는 네 기존 '방 생성/정리' 및 포인트/AFK 로직 유지 -----
    # --- 방 생성/정리 (기존 기능 그대로) ---
    if after.channel and after.channel.name in TRIGGER_CHANNEL_NAMES:
        category = after.channel.category
        new_channel = await category.create_voice_channel(f"방장: {member.display_name}")
        temp_channels[new_channel.id] = new_channel
        await member.move_to(new_channel)

    if before.channel and before.channel.id in temp_channels:
        if len(before.channel.members) == 0:
            await temp_channels[before.channel.id].delete()
            del temp_channels[before.channel.id]

    # --- 포인트 정산/세션 관리 (신규) ---
    awarded = 0
    new_total = 0
    with closing(get_db()) as db, db:
        ensure_user(db, gid, uid)
        afk_id = get_afk_channel_id(db, gid)

        if before.channel and (not afk_id or before.channel.id != afk_id):
            row = db.execute("SELECT last_join FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
            if row and row["last_join"]:
                elapsed = now - int(row["last_join"])
                awarded, new_total = grant_points_for_session(db, gid, uid, elapsed)
                db.execute("UPDATE users SET last_join=NULL WHERE guild_id=? AND user_id=?", (gid, uid))

        if after.channel and (not afk_id or after.channel.id != afk_id):
            row = db.execute("SELECT last_join FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
            if not row or row["last_join"] is None:
                db.execute("UPDATE users SET last_join=? WHERE guild_id=? AND user_id=?", (now, gid, uid))

        if after.channel and (not after.self_mute and not after.self_deaf):
            mark_active(db, gid, uid, now)

        if awarded > 0:
            log_ch = get_log_channel_obj(member.guild)
            if log_ch:
                try:
                    blocks = awarded // POINTS_PER_BLOCK
                    minutes = blocks * (BLOCK_SECONDS // 60)
                    await log_ch.send(
                        f"{member.name} 님이 **{minutes}분 활동**으로 **{awarded}p** 획득! (총 {new_total}p)"
                    )
                except Exception:
                    pass

#============================================================

async def create_team_set(category: discord.CategoryChannel,
                          owner: discord.Member,
                          trigger_ch: discord.VoiceChannel | None = None) -> int:
    """카테고리에 1~4팀 생성. 이미 있으면 재사용. 생성 후 트리거 채널은 삭제."""
    # 이미 이 카테고리에 팀 세트가 있으면 첫 채널로 이동만
    if category.id in category_group:
        group_key = category_group[category.id]
        first_id = team_groups.get(group_key, [None])[0]
        first_ch = category.guild.get_channel(first_id)
        if isinstance(first_ch, discord.VoiceChannel):
            try:
                await owner.move_to(first_ch)
            except Exception:
                pass
        # 트리거 채널이 남아있다면 지워주기(중복 클릭 방지)
        if trigger_ch:
            try: await trigger_ch.delete()
            except Exception: pass
        return group_key

    # 1~4팀 생성
    names = ["1팀", "2팀", "3팀", "4팀"]
    created = [await category.create_voice_channel(n) for n in names]

    group_key = created[0].id
    team_groups[group_key] = [c.id for c in created]
    category_group[category.id] = group_key
    for cid in team_groups[group_key]:
        channel_to_group[cid] = group_key

    # ✅ 1) 먼저 유저를 1팀으로 이동
    try:
        await owner.move_to(created[0])
    except Exception:
        pass

    # 살짝 텀 주면 안정적(이벤트 순서/권한 레이스 대비)
    await asyncio.sleep(0.1)

    # ✅ 2) 그 다음 트리거 채널 삭제
    if trigger_ch:
        try:
            await trigger_ch.delete()
        except Exception:
            pass

    return group_key
    


async def maybe_cleanup_team_set(guild: discord.Guild, channel_id: int):
    """세트가 전부 비면 1~4팀 삭제하고 카테고리에 '팀 생성' 채널 복구."""
    if channel_id not in channel_to_group:
        return
    group_key = channel_to_group[channel_id]
    ch_ids = team_groups.get(group_key, [])

    # 채널 객체 수집
    channels: list[discord.VoiceChannel] = []
    for cid in ch_ids:
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.VoiceChannel):
            channels.append(ch)

    # 모두 비었는지 확인
    all_empty = channels and all(len(ch.members) == 0 for ch in channels)
    if not all_empty:
        return

    # 보이스 클라 붙어있으면 끊기
    try:
        vc = guild.voice_client
        if vc and vc.channel and vc.channel.id in ch_ids:
            await vc.disconnect(force=True)
    except Exception:
        pass

    # 채널 삭제
    for ch in channels:
        try: await ch.delete()
        except Exception: pass

    # 매핑 해제
    for cid in ch_ids:
        channel_to_group.pop(cid, None)
    team_groups.pop(group_key, None)

    # 카테고리 기준으로 '팀 생성' 복구
    cat = channels[0].category if channels else None
    if cat:
        category_group.pop(cat.id, None)  # 세트 없음으로 표시
        # 이미 있으면 중복 생성 방지
        already = any(vch.name == TEAM_TRIGGER_NAME for vch in cat.voice_channels)
        if not already:
            try:
                await cat.create_voice_channel(TEAM_TRIGGER_NAME)
            except Exception:
                pass


# ================ 포인트 보조 루프 (5분 간격) ================
@tasks.loop(minutes=5)
async def accrual_loop():
    now = int(dt.datetime.utcnow().timestamp())
    awarded_msgs = []

    with closing(get_db()) as db, db:
        for guild in bot.guilds:
            afk_id = get_afk_channel_id(db, guild.id)
            in_voice = set()
            for vc in guild.voice_channels:
                if afk_id and vc.id == afk_id:
                    continue
                for m in vc.members:
                    if m.bot:
                        continue
                    in_voice.add(m.id)

                    # ✅ 여기가 핵심: 음성채널에 있고 뮤트/이어폰 아니면 활동으로 갱신
                    try:
                        vs = m.voice  # discord.VoiceState
                        if vs and not vs.self_mute and not vs.self_deaf:
                            mark_active(db, guild.id, m.id, now)
                    except Exception:
                        pass

            rows = db.execute("""
                SELECT user_id, last_join FROM users
                WHERE guild_id=? AND last_join IS NOT NULL
            """, (guild.id,)).fetchall()

            for r in rows:
                uid = r["user_id"]
                elapsed = now - int(r["last_join"])
                awarded, new_total = grant_points_for_session(db, guild.id, uid, elapsed)

                if uid not in in_voice:
                    db.execute("UPDATE users SET last_join=NULL WHERE guild_id=? AND user_id=?", (guild.id, uid))
                else:
                    db.execute("UPDATE users SET last_join=? WHERE guild_id=? AND user_id=?", (now, guild.id, uid))

                if awarded > 0:
                    awarded_msgs.append((guild, uid, awarded, new_total))

    for guild, uid, awarded, new_total in awarded_msgs:
        ch = get_log_channel_obj(guild)
        if not ch:
            continue
        member = guild.get_member(uid)
        if not member:
            continue
        blocks  = awarded // POINTS_PER_BLOCK
        minutes = blocks * (BLOCK_SECONDS // 60)
        await ch.send(
            f"{str(member)} 님이 **{minutes}분 활동**으로 **{awarded}p** 획득! (총 {new_total}p)"
        )

# ================ AFK 감시 루프 (1분 간격) ================
@tasks.loop(minutes=1)
async def afk_guard():
    now = int(dt.datetime.utcnow().timestamp())
    with closing(get_db()) as db, db:
        for guild in bot.guilds:
            afk_id = get_afk_channel_id(db, guild.id)
            if not afk_id:
                continue

            afk_channel = guild.get_channel(afk_id)
            if not isinstance(afk_channel, discord.VoiceChannel):
                continue

            for vc in guild.voice_channels:
                if vc.id == afk_id:
                    continue

                for m in vc.members:
                    if m.bot:
                        continue

                    # 최근 활동시각
                    row = db.execute(
                        "SELECT last_active FROM afk_watch WHERE guild_id=? AND user_id=?",
                        (guild.id, m.id)
                    ).fetchone()
                    last_active = row["last_active"] if row else now
                    inactive = (now - last_active) >= AFK_SECONDS

                    # 뮤트/이어폰 상태 지속 시간 디바운스
                    key = (guild.id, m.id)
                    currently_muted = (m.voice.self_mute or m.voice.self_deaf)

                    if currently_muted:
                        # 시작 기록 없으면 지금부터 카운트
                        if key not in mute_since:
                            mute_since[key] = now
                        muted_long = (now - mute_since[key]) >= MUTE_GRACE_SECONDS
                    else:
                        # 뮤트 해제되면 카운터 제거
                        if key in mute_since:
                            mute_since.pop(key, None)
                        muted_long = False

                    # 이동 조건: 오랜 비활동 or (뮤트/이어폰이 일정 시간 이상 지속)
                    if inactive or muted_long:
                        try:
                            await m.move_to(afk_channel, reason="비활동/뮤트 지속으로 AFK 이동")
                            # 이동했으면 뮤트 타이머도 초기화
                            mute_since.pop(key, None)
                        except discord.Forbidden:
                            pass
                        except Exception:
                            pass

# ================ Slash 명령 (포인트/상점/AFK) ================
@bot.command(name="포인트")
async def points_prefix_kr(ctx, member: discord.Member = None):
    member = member or ctx.author
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT points FROM users WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id)
        ).fetchone()
    pts = row["points"] if row else 0
    await ctx.send(f"💰 {member.display_name} 님의 포인트: **{pts}p**")

@bot.command(name="points")
async def points_prefix_en(ctx, member: discord.Member = None):
    await points_prefix_kr(ctx, member)

@bot.tree.command(name="leaderboard", description="포인트 리더보드 Top 10")
async def leaderboard_cmd(interaction: discord.Interaction):
    with closing(get_db()) as db:
        rows = db.execute("SELECT user_id, points FROM users WHERE guild_id=? ORDER BY points DESC LIMIT 10",
                          (interaction.guild.id,)).fetchall()
    if not rows:
        await interaction.response.send_message("아직 포인트가 없습니다.")
        return
    lines = []
    for i, r in enumerate(rows, start=1):
        mem = interaction.guild.get_member(r["user_id"])
        name = mem.display_name if mem else str(r["user_id"])
        lines.append(f"{i}. {name} — {r['points']}p")
    await interaction.response.send_message("🏆 **리더보드**\n" + "\n".join(lines))

@bot.tree.command(name="set_afk_channel", description="잠수방(포인트 제외 & 자동이동) 설정/해제")
@app_commands.default_permissions(administrator=True)
async def set_afk_channel_cmd(interaction: discord.Interaction, channel: Optional[discord.VoiceChannel]):
    with closing(get_db()) as db, db:
        set_afk_channel_id(db, interaction.guild.id, channel.id if channel else None)
    await interaction.response.send_message(
        f"⛔ 잠수방: **{channel.name}**" if channel else "잠수방 설정이 해제되었습니다."
    )

@bot.tree.command(name="shop_add", description="상점 아이템 추가 (관리자)")
@app_commands.default_permissions(administrator=True)
async def shop_add_cmd(interaction: discord.Interaction, name: str, price: int, stock: Optional[int] = None):
    if price < 0:
        await interaction.response.send_message("가격은 0 이상이어야 합니다.", ephemeral=True)
        return
    with closing(get_db()) as db, db:
        db.execute("INSERT INTO shop(guild_id, name, price, stock) VALUES(?, ?, ?, ?)",
                   (interaction.guild.id, name, price, stock))
    await interaction.response.send_message(f"🛒 추가: [{name}] — {price}p (재고: {'무제한' if stock is None else stock})")

@bot.tree.command(name="shop", description="상점 목록 보기")
async def shop_cmd(interaction: discord.Interaction):
    with closing(get_db()) as db:
        rows = db.execute("SELECT id, name, price, stock FROM shop WHERE guild_id=? ORDER BY id ASC",
                          (interaction.guild.id,)).fetchall()
    if not rows:
        await interaction.response.send_message("상점에 등록된 아이템이 없습니다.")
        return
    lines = [f"[{r['id']}] {r['name']} — {r['price']}p (재고: {'무제한' if r['stock'] is None else r['stock']})" for r in rows]
    await interaction.response.send_message("**상점 목록**\n" + "\n".join(lines))

@bot.tree.command(name="buy", description="상점 아이템 구매")
async def buy_cmd(interaction: discord.Interaction, item_id: int):
    uid, gid = interaction.user.id, interaction.guild.id
    with closing(get_db()) as db, db:
        ensure_user(db, gid, uid)
        item = db.execute("SELECT id, name, price, stock FROM shop WHERE guild_id=? AND id=?",
                          (gid, item_id)).fetchone()
        if not item:
            await interaction.response.send_message("해당 ID의 아이템이 없습니다.", ephemeral=True)
            return
        user = db.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
        pts = user["points"] if user else 0
        if pts < item["price"]:
            await interaction.response.send_message(f"포인트가 부족합니다. (보유 {pts}p / 필요 {item['price']}p)", ephemeral=True)
            return
        if item["stock"] is not None and item["stock"] <= 0:
            await interaction.response.send_message("해당 아이템은 품절입니다.", ephemeral=True)
            return
        db.execute("UPDATE users SET points = points - ? WHERE guild_id=? AND user_id=?",
                   (item["price"], gid, uid))
        if item["stock"] is not None:
            db.execute("UPDATE shop SET stock = stock - 1 WHERE id=?", (item["id"],))
        db.execute("INSERT INTO purchases(guild_id, user_id, item_id, ts) VALUES(?, ?, ?, ?)",
                   (gid, uid, item["id"], int(dt.datetime.utcnow().timestamp())))
    await interaction.response.send_message(f"✅ 구매 완료: **{item['name']}** — {item['price']}p 차감")

@bot.tree.command(name="give", description="특정 유저에게 포인트 지급/차감 (관리자)")
@app_commands.default_permissions(administrator=True)
async def give_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    with closing(get_db()) as db, db:
        ensure_user(db, interaction.guild.id, member.id)
        db.execute("UPDATE users SET points=points+? WHERE guild_id=? AND user_id=?",
                   (amount, interaction.guild.id, member.id))
    await interaction.response.send_message(f"{member.display_name} 님에게 {amount:+}p 적용됨")

# ================== 너의 기존 명령들 그대로 유지 ==================
# 도움말/입퇴장/닉변경/clear/역할/질문/등록/점수/랭킹/팀짜기/로그/a/투표/음악/TTS/정지 등
# ---- 아래는 네가 올린 기존 코드 그대로 ----

@bot.command(name="?")
async def question_help(ctx):
    help_message = """
**📜 명령어 목록**
`$role`,`$역할` - 역할 버튼 표시 / 롤 전용 채널 사용
`$a`,`$질문` - AI에게 질문
`$s` - TTS 말하기 기능
`$sstop` - TTS 강제종료
`$rps @대결상대` -  심플 가위바위보
`$?` - 도움말 출력
"""
    await ctx.send(help_message)

#===== 로그채널 설정 ======

@bot.command()
async def setlog(ctx, channel: discord.TextChannel):
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO guild_settings(guild_id, log_channel_id) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
            (ctx.guild.id, channel.id)
        )
    await ctx.send(f"📜 로그 채널이 {channel.mention} 로 설정되었습니다!")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_afk(ctx, channel: discord.VoiceChannel):
    """AFK 채널 ID를 DB에 저장"""
    with closing(get_db()) as db, db:
        db.execute("""
            INSERT INTO guild_settings (guild_id, afk_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET afk_channel_id=excluded.afk_channel_id
        """, (ctx.guild.id, channel.id))
    await ctx.send(f"✅ AFK 채널이 `{channel.name}`(ID: {channel.id})로 설정되었습니다.")


@bot.event 
async def on_member_join(member):
    channel = discord.utils.get(member.guild.text_channels, name=ENTER_QUIT)
    if channel:
        await channel.send(f"{member.display_name}님이 서버에 입장했습니다!")

@bot.event
async def on_member_remove(member):
    channel = discord.utils.get(member.guild.text_channels, name=ENTER_QUIT)
    if channel:
        await channel.send(f"{member.display_name}님이 서버에서 퇴장했습니다!")

@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        channel = discord.utils.get(after.guild.text_channels, name="닉네임변경")
        if channel:
            old_nick = before.nick if before.nick else before.name
            new_nick = after.nick if after.nick else after.name
            await channel.send(f"**{old_nick}** 님이 닉네임을 **{new_nick}**(으)로 변경했습니다.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, *args):
    if not args:
        await ctx.send("사용법: `$clear all`, `$clear 100`, `$clear from @User`", delete_after=5)
        return
    if args[0] == "all":
        await ctx.channel.purge(limit=None)
        await ctx.send("모든 메시지가 삭제되었습니다.", delete_after=3)
    elif args[0].isdigit():
        count = int(args[0])
        await ctx.channel.purge(limit=count + 1)
        await ctx.send(f"최근 {count}개 메시지 삭제 완료", delete_after=3)
    elif args[0] == "from" and len(ctx.message.mentions) > 0:
        member = ctx.message.mentions[0]
        deleted = await ctx.channel.purge(limit=1000, check=lambda m: m.author == member)
        await ctx.send(f"{member.display_name}님의 메시지 {len(deleted)}개 삭제 완료", delete_after=3)
    else:
        await ctx.send("사용법: `$clear all`, `$clear 100`, `$clear from @User`", delete_after=5)

@clear.error
async def clear_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ 메시지 관리 권한이 없습니다.", delete_after=5)
    else:
        raise error
class RoleView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleButton(label="멤버", role_name="멤버", style=discord.ButtonStyle.green))
        self.add_item(RoleButton(label="지인", role_name="지인", style=discord.ButtonStyle.grey))
        self.add_item(RoleButton(label="탑", role_name="탑"))
        self.add_item(RoleButton(label="정글", role_name="정글"))
        self.add_item(RoleButton(label="미드", role_name="미드"))
        self.add_item(RoleButton(label="원딜", role_name="원딜"))
        self.add_item(RoleButton(label="서폿", role_name="서폿"))

class RoleButton(Button):
    def __init__(self, label, role_name, style=discord.ButtonStyle.blurple):
        super().__init__(label=label, style=style, custom_id=f"role_{role_name}")
        self.role_name = role_name
    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        role = discord.utils.get(guild.roles, name=self.role_name)
        if self.role_name in MEMBER_ROLES:
            for r_name in MEMBER_ROLES:
                role_to_remove = discord.utils.get(guild.roles, name=r_name)
                if role_to_remove and role_to_remove in interaction.user.roles:
                    await interaction.user.remove_roles(role_to_remove)
        if self.role_name in POSITION_ROLES:
            for r_name in POSITION_ROLES:
                role_to_remove = discord.utils.get(guild.roles, name=r_name)
                if role_to_remove and role_to_remove in interaction.user.roles:
                    await interaction.user.remove_roles(role_to_remove)
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            msg = await interaction.channel.send(f"{interaction.user.mention} `{role.name}` 역할을 제거했습니다.")
        else:
            await interaction.user.add_roles(role)
            msg = await interaction.channel.send(f"{interaction.user.mention} `{role.name}` 역할을 부여했습니다.")
        await msg.delete(delay=3)
        await interaction.response.defer()

@bot.command()
async def 역할(ctx):
    embed = discord.Embed(
        title="역할 선택",
        description="🟢 **멤버 / 지인** 중 하나를 고르세요 (중복 불가)\n🔵 **포지션 (탑/정글/미드/원딜/서폿)** 중 하나만 유지됩니다.\n\n다시 누르면 해제됩니다.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=RoleView())

@bot.command()
async def role(ctx):
    embed = discord.Embed(
        title="역할 선택",
        description="🟢 **멤버 / 지인** 중 하나를 고르세요 (중복 불가)\n🔵 **포지션 (탑/정글/미드/원딜/서폿)** 중 하나만 유지됩니다.\n\n다시 누르면 해제됩니다.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=RoleView())

@bot.command()
async def 질문(ctx, *, question):
    thinking = await ctx.send("🤔 강철봇이 생각 중...")
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": question}]
    )
    answer = response.choices[0].message.content.strip()
    embed = discord.Embed(title="🤖 강철봇의 답변", description=answer, color=discord.Color.blue())
    embed.set_footer(text=f"질문자: {ctx.author.display_name}")
    await thinking.delete()
    await ctx.send(embed=embed)

# ----- 랭킹(기존) -----
@bot.command()
async def 등록(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    guild_id = str(ctx.guild.id)
    user_id = str(member.id)
    username = member.name
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM scores WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    if cursor.fetchone():
        await ctx.send(f"{username} 님은 이미 등록되어 있습니다.")
    else:
        cursor.execute("INSERT INTO scores (guild_id, user_id, username, score) VALUES (?, ?, ?, 1000)",
                       (guild_id, user_id, username))
        conn.commit()
        await ctx.send(f"{username} 님을 1000점으로 등록했습니다! ✅")
    conn.close()

@bot.command()
async def 점수(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    guild_id = str(ctx.guild.id); user_id = str(member.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT score FROM scores WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cursor.fetchone(); conn.close()
    if row: await ctx.send(f"{member.name}님의 현재 점수는 {row[0]}점입니다.")
    else:   await ctx.send(f"{member.name}님은 아직 등록되지 않았습니다.")

@bot.command()
async def 랭킹(ctx):
    guild = ctx.guild; guild_id = str(guild.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT user_id, score FROM scores WHERE guild_id=? ORDER BY score DESC LIMIT 10", (guild_id,))
    rows = cursor.fetchall(); conn.close()
    if not rows:
        await ctx.send("등록된 유저가 없습니다."); return
    msg = "🏆 **서버 내 랭킹 TOP 10** 🏆\n"
    for i, (user_id, score) in enumerate(rows, start=1):
        member = guild.get_member(int(user_id))
        name = f"@{member.display_name}" if member else f"`탈퇴 또는 미확인 유저 ({user_id})`"
        msg += f"{i}. {name} - {score}점\n"
    await ctx.send(msg)

@bot.command()
async def 팀짜기(ctx, *members: discord.Member):
    guild_id = str(ctx.guild.id)
    if len(members) < 2:
        await ctx.send("최소 2명 이상의 멤버를 멘션해주세요."); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    team_pool = []; missing = []
    for member in members:
        uid = str(member.id)
        cursor.execute("SELECT username, score FROM scores WHERE guild_id=? AND user_id=?", (guild_id, uid))
        row = cursor.fetchone()
        (team_pool.append(row) if row else missing.append(member.mention))
    conn.close()
    if missing:
        await ctx.send(f"등록되지 않은 유저: {', '.join(missing)}"); return
    random.shuffle(team_pool)
    team1, team2, sum1, sum2 = [], [], 0, 0
    for name, score in team_pool:
        if sum1 <= sum2: team1.append((name, score)); sum1 += score
        else:            team2.append((name, score)); sum2 += score
    def fmt(team, total): return "\n".join([f"{n} - {s}" for n, s in team]) + f"\n총합: {total}"
    result = f"""📊 멘션한 유저로 팀 편성 완료

🔵 **Team A**
{fmt(team1, sum1)}

🔴 **Team B**
{fmt(team2, sum2)}
"""
    await ctx.send(result)

@bot.command()
async def 로그(ctx):
    guild_id = str(ctx.guild.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT timestamp, winner_ids, loser_ids, note FROM match_logs WHERE guild_id=? ORDER BY id DESC LIMIT 5",
                   (guild_id,))
    logs = cursor.fetchall(); conn.close()
    if logs:
        msg = "📜 **최근 경기 기록**\n"
        for i, (time, winners, losers, note) in enumerate(logs, start=1):
            msg += f"{i}. [{time}]\n승리: {winners}\n패배: {losers}\n비고: {note}\n\n"
        await ctx.send(msg)
    else:
        await ctx.send("아직 기록된 경기가 없습니다.")

@bot.command()
async def a(ctx, *, question):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": question}]
    )
    await ctx.send(response.choices[0].message.content.strip())

@bot.command(name='투표')
async def poll(ctx, *, 질문):
    message = await ctx.send(f"📊 **{질문}**\n\n👍 가능\n❌ 불가능\n🤔 미정")
    await message.add_reaction("👍"); await message.add_reaction("❌"); await message.add_reaction("🤔")

@bot.command()
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send("먼저 음성 채널에 들어가 있어야 해요."); return
    if not ctx.voice_client:
        vc: wavelink.Player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
    else:
        vc: wavelink.Player = ctx.voice_client
    tracks = await wavelink.Playable.search(f"ytmsearch:{query}")
    await ctx.send("Youtube Music에서 검색중 ..."); await ctx.send(f"쿼리: {query}"); await ctx.send(f"{tracks}")
    if not tracks:
        tracks = await wavelink.Playable.search(f"ytsearch:{query}")
        await ctx.send("Youtube 에서 검색중 ..."); await ctx.send(f"{tracks}")
    if not tracks:
        await ctx.send("트랙을 찾을 수 없습니다."); await ctx.send("해당 국가는 Youtube API를 이용하실 수 없습니다."); return
    track = tracks[0]; await vc.play(track); await ctx.send(f"🎵 현재 재생 중: {track.title}")

@bot.command()
async def resume(ctx):
    vc: wavelink.Player = ctx.voice_client
    await vc.resume(); await ctx.send("▶️ 다시 재생!")

@bot.command()
async def skip(ctx):
    vc: wavelink.Player = ctx.voice_client
    await vc.stop(); await ctx.send("⏭️ 스킵!")

@bot.command()
async def stop(ctx):
    vc: wavelink.Player = ctx.voice_client
    if vc:
        await vc.stop(); await vc.disconnect(); await ctx.send("🛑 노래 중지 및 음성채널에서 나갔습니다.")
    else:
        await ctx.send("이미 음성채널에 없습니다.")

@bot.command()
async def s(ctx, *, text):
    # 커맨드 흔적 지우기(선택)
    try:
        await ctx.message.delete()
    except Exception:
        pass

    # 보이스 관련 동시성 제어: 한 길드당 한 번씩만 처리
    async with voice_locks[ctx.guild.id]:
        # 발화자 음성 채널 체크
        if not ctx.author.voice:
            await ctx.send("먼저 음성채널에 들어가 있어야 해요.", delete_after=3)
            return

        # 연결/재사용
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            try:
                vc = await ctx.author.voice.channel.connect(timeout=15, reconnect=True)
            except asyncio.TimeoutError:
                await ctx.send("보이스 연결 타임아웃… 잠시 후 다시 시도해줘요.", delete_after=4)
                return
            except discord.ClientException:
                vc = ctx.voice_client  # 이미 연결 중/완료 상태

        # edge-tts 합성 (캐시 사용)
        mp3_path = await tts_synthesize_to_file(text)

        # 재생 중이면 정지 후 새로
        if vc.is_playing():
            vc.stop()

        # ffmpeg로 재생
        source = discord.FFmpegPCMAudio(mp3_path, before_options="-nostdin", options="-vn")
        vc.play(source)

        # 끝날 때까지 짧게 폴링
        while vc.is_playing():
            await asyncio.sleep(0.2)


@bot.command()
async def sstop(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("TTS를 중지했습니다.", delete_after=3)
    else:
        await ctx.send("현재 TTS가 재생 중이 아닙니다.", delete_after=3)

# ================= 실행 =================
def main():
    if not MY_DISCORD_TOKEN_KEY:
        print("환경변수 DISCORD_TOKEN이 비어있습니다. 토큰을 설정하세요.")
        return
    bot.run(MY_DISCORD_TOKEN_KEY)

if __name__ == "__main__":
    main()
