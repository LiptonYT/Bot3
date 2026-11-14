import os
import json
import sqlite3
import logging
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.json'
DB_PATH = BASE_DIR / 'promotions.sqlite3'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('karateli-promotion')

if not CONFIG_PATH.exists():
    raise SystemExit('Не найден config.json. Создайте его по образцу config.example.json.')
try:
    with CONFIG_PATH.open('r', encoding='utf-8') as f:
        CFG = json.load(f)
except json.JSONDecodeError as exc:
    raise SystemExit(f'Ошибка config.json: {exc}. Проверьте JSON и заполните файл.')

TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not TOKEN:
    raise SystemExit('Не задан BOT_TOKEN в переменных окружения хостинга.')

RANKS = CFG.get('rank_roles', {})
if not isinstance(RANKS, dict) or not RANKS:
    raise SystemExit('В config.json заполните rank_roles: названия званий и ID ролей.')

try:
    EMPLOYEE_ROLE_ID = int(CFG['employee_role_id'])
    TRAINEE_DEPT_ROLE_ID = int(CFG['trainee_department_role_id'])
    SUBMISSIONS_CHANNEL_ID = int(CFG['submissions_channel_id'])
    SENIOR_ROLE_IDS = {int(x) for x in CFG['senior_role_ids']}
    RANK_ROLE_IDS = {str(name): int(role_id) for name, role_id in RANKS.items()}
except (KeyError, TypeError, ValueError) as exc:
    raise SystemExit(f'В config.json отсутствует обязательный параметр или неверный ID: {exc}')

TRAINEE_UNTIL = set(CFG.get('trainee_department_until_ranks', ['Стажёр', 'Приближённый']))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            current_rank TEXT NOT NULL,
            new_rank TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id INTEGER,
            decision_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')


def get_role(guild: discord.Guild, role_id: int):
    return guild.get_role(int(role_id))


def member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == int(role_id) for r in member.roles)


def current_rank(member: discord.Member) -> str:
    # First match configured role IDs; this is reliable even if Discord role names were changed.
    for name, role_id in RANK_ROLE_IDS.items():
        if member_has_role(member, role_id):
            return name
    # Fallback: match role names for configurations where an ID was accidentally omitted.
    rank_names = set(RANK_ROLE_IDS)
    for role in member.roles:
        if role.name in rank_names:
            return role.name
    return 'Не определено'


def is_senior(member: discord.Member) -> bool:
    return any(member_has_role(member, rid) for rid in SENIOR_ROLE_IDS)


def build_request_embed(row, status='pending', reviewer=None, decision_reason=None):
    colors = {
        'pending': discord.Color.from_rgb(130, 20, 35),
        'approved': discord.Color.from_rgb(35, 145, 85),
        'rejected': discord.Color.from_rgb(190, 45, 55),
    }
    embed = discord.Embed(
        title='💀 НОВЫЙ СПИСОК НА ПОВЫШЕНИЕ',
        description='**КАРАТЕЛИ · КАДРОВОЕ ПРЕДСТАВЛЕНИЕ**\nРапорт на рассмотрение старшего состава.',
        color=colors.get(status, discord.Color.dark_red()),
    )
    embed.add_field(name='👤 Кого представляют', value=f'<@{row["target_id"]}>', inline=True)
    embed.add_field(name='📝 Кто отписал', value=f'<@{row["author_id"]}>', inline=True)
    current_role_id = RANK_ROLE_IDS.get(row['current_rank'])
    current_rank_display = f'<@&{current_role_id}>' if current_role_id else row['current_rank']
    embed.add_field(name='🎖️ Текущее звание', value=current_rank_display, inline=True)
    embed.add_field(name='⬆️ Представляют на звание', value=f'<@&{RANK_ROLE_IDS[row["new_rank"]]}>', inline=False)
    embed.add_field(name='📄 Причина повышения', value=row['reason'][:1024], inline=False)
    if status == 'pending':
        embed.add_field(name='⏳ Статус', value='Ожидает решения старшего состава', inline=False)
        embed.set_footer(text=f'КАРАТЕЛИ · Система повышения · Заявка №{row["id"]}')
    elif status == 'approved':
        embed.add_field(name='✅ Решение', value=f'**ОДОБРЕНО**\nПроверяющий: {reviewer.mention if reviewer else "Неизвестно"}', inline=False)
        embed.add_field(name='Итог', value=f'<@{row["target_id"]}> получил звание <@&{RANK_ROLE_IDS[row["new_rank"]]}>.', inline=False)
        embed.set_footer(text=f'КАРАТЕЛИ · Повышение одобрено · Заявка №{row["id"]}')
    else:
        embed.add_field(name='❌ Решение', value=f'**ОТКЛОНЕНО**\nПроверяющий: {reviewer.mention if reviewer else "Неизвестно"}', inline=False)
        embed.add_field(name='Причина отказа', value=(decision_reason or 'Не указана')[:1024], inline=False)
        embed.set_footer(text=f'КАРАТЕЛИ · Повышение отклонено · Заявка №{row["id"]}')
    return embed


class ReasonModal(discord.ui.Modal, title='Причина повышения'):
    def __init__(self, target_id: int, new_rank: str, author_id: int):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.new_rank = new_rank
        self.author_id = author_id
        self.reason = discord.ui.TextInput(
            label='Причина повышения', placeholder='Опишите заслуги и причины повышения',
            style=discord.TextStyle.paragraph, min_length=3, max_length=1000, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        target = guild.get_member(self.target_id) if guild else None
        if target is None:
            return await interaction.response.send_message('Участник не найден на сервере.', ephemeral=True)
        if not member_has_role(interaction.user, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('Подать рапорт можно только с ролью «Сотрудник семьи».', ephemeral=True)
        if not member_has_role(target, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('У выбранного участника нет роли «Сотрудник семьи».', ephemeral=True)
        channel = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(SUBMISSIONS_CHANNEL_ID)
            except discord.HTTPException:
                return await interaction.response.send_message('Канал списков на повышение не найден. Проверьте submissions_channel_id.', ephemeral=True)
        new_role = get_role(guild, RANK_ROLE_IDS[self.new_rank])
        if new_role is None:
            return await interaction.response.send_message('Роль выбранного звания не найдена. Проверьте rank_roles.', ephemeral=True)
        cur_rank = current_rank(target)
        temp = {
            'target_id': target.id, 'author_id': interaction.user.id,
            'current_rank': cur_rank, 'new_rank': self.new_rank,
            'reason': str(self.reason.value), 'id': '—'
        }
        # Send first, then persist the real message ID and update the footer with request number.
        placeholder = discord.Embed(title='💀 НОВЫЙ СПИСОК НА ПОВЫШЕНИЕ', description='Подготовка рапорта…', color=discord.Color.dark_red())
        msg = await channel.send(embed=placeholder, allowed_mentions=discord.AllowedMentions.none())
        with db_connect() as con:
            cur = con.execute('''INSERT INTO promotions (guild_id, channel_id, message_id, target_id, author_id, current_rank, new_rank, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (guild.id, channel.id, msg.id, target.id, interaction.user.id, cur_rank, self.new_rank, str(self.reason.value)))
            request_id = cur.lastrowid
            row = con.execute('SELECT * FROM promotions WHERE id=?', (request_id,)).fetchone()
        view = DecisionView(request_id)
        await msg.edit(embed=build_request_embed(row), view=view)
        await interaction.response.send_message(f'✅ Рапорт №{request_id} отправлен в список на повышение: {msg.jump_url}', ephemeral=True)


class RankSelect(discord.ui.Select):
    def __init__(self, target_id: int, author_id: int):
        options = [discord.SelectOption(label=name, value=name) for name in RANK_ROLE_IDS.keys()][:25]
        super().__init__(placeholder='Выберите новое звание', min_values=1, max_values=1, options=options)
        self.target_id = target_id
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message('Эта форма открыта не для вас.', ephemeral=True)
        target = interaction.guild.get_member(self.target_id)
        if target is None:
            return await interaction.response.send_message('Участник не найден.', ephemeral=True)
        if self.values[0] == current_rank(target):
            return await interaction.response.send_message('У участника уже есть выбранное звание.', ephemeral=True)
        await interaction.response.send_modal(ReasonModal(self.target_id, self.values[0], self.author_id))


class RankSelectView(discord.ui.View):
    def __init__(self, target_id: int, author_id: int):
        super().__init__(timeout=180)
        self.add_item(RankSelect(target_id, author_id))


class TargetSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder='Выберите участника, которого представляют', min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if not member_has_role(interaction.user, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('Подать рапорт можно только с ролью «Сотрудник семьи».', ephemeral=True)
        target = interaction.guild.get_member(self.values[0].id)
        if target is None or not member_has_role(target, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('У выбранного участника должна быть роль «Сотрудник семьи».', ephemeral=True)
        if target.bot:
            return await interaction.response.send_message('Нельзя подать рапорт на бота.', ephemeral=True)
        if current_rank(target) == 'Не определено':
            return await interaction.response.send_message('Не удалось определить текущее звание участника. Проверьте, что ID его звания есть в rank_roles.', ephemeral=True)
        await interaction.response.send_message(
            f'Участник: {target.mention}\nТекущее звание: **{current_rank(target)}**\nВыберите звание, на которое его представляют:',
            view=RankSelectView(target.id, interaction.user.id), ephemeral=True)


class TargetSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(TargetSelect())


class PromotionPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Отписать', emoji='📝', style=discord.ButtonStyle.danger, custom_id='karateli_promotion:submit')
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not member_has_role(interaction.user, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('Подать рапорт можно только с ролью «Сотрудник семьи».', ephemeral=True)
        await interaction.response.send_message('Выберите участника, которого представляют на повышение:', view=TargetSelectView(), ephemeral=True)


class RejectModal(discord.ui.Modal, title='Отклонение рапорта'):
    def __init__(self, request_id: int):
        super().__init__(timeout=300)
        self.request_id = request_id
        self.reason = discord.ui.TextInput(label='Причина отказа', placeholder='Укажите причину отклонения', style=discord.TextStyle.paragraph, min_length=2, max_length=500, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not is_senior(interaction.user):
            return await interaction.response.send_message('Отклонять рапорты может только старший состав.', ephemeral=True)
        with db_connect() as con:
            row = con.execute('SELECT * FROM promotions WHERE id=?', (self.request_id,)).fetchone()
            if not row or row['status'] != 'pending':
                return await interaction.response.send_message('Этот рапорт уже рассмотрен или не найден.', ephemeral=True)
            con.execute("UPDATE promotions SET status='rejected', reviewer_id=?, decision_reason=? WHERE id=?",
                        (interaction.user.id, str(self.reason.value), self.request_id))
            updated = con.execute('SELECT * FROM promotions WHERE id=?', (self.request_id,)).fetchone()
        channel = bot.get_channel(row['channel_id']) or await bot.fetch_channel(row['channel_id'])
        try:
            message = await channel.fetch_message(row['message_id'])
            await message.edit(embed=build_request_embed(updated, 'rejected', interaction.user, str(self.reason.value)), view=ClosedDecisionView())
        except discord.HTTPException:
            log.exception('Could not update rejected promotion message %s', self.request_id)
        await interaction.response.send_message('❌ Рапорт отмечен как отклонённый. Карточка обновлена.', ephemeral=True)
        for uid, text in ((row['target_id'], f'Ваше представление на повышение отклонено. Причина: {self.reason.value}'),
                          (row['author_id'], f'Ваш рапорт на участника <@{row["target_id"]}> отклонён. Причина: {self.reason.value}')):
            try:
                user = await bot.fetch_user(uid)
                await user.send(text)
            except discord.HTTPException:
                pass


class DecisionView(discord.ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        approve = discord.ui.Button(label='Одобрить', emoji='✅', style=discord.ButtonStyle.success, custom_id=f'karateli_promotion:approve:{request_id}')
        reject = discord.ui.Button(label='Отклонить', emoji='❌', style=discord.ButtonStyle.danger, custom_id=f'karateli_promotion:reject:{request_id}')
        approve.callback = self.approve_callback
        reject.callback = self.reject_callback
        self.add_item(approve)
        self.add_item(reject)

    async def check_pending(self, interaction):
        if not interaction.guild or not is_senior(interaction.user):
            await interaction.response.send_message('Рассматривать рапорты может только старший состав.', ephemeral=True)
            return False
        with db_connect() as con:
            row = con.execute('SELECT * FROM promotions WHERE id=?', (self.request_id,)).fetchone()
        if not row or row['status'] != 'pending':
            await interaction.response.send_message('Этот рапорт уже рассмотрен.', ephemeral=True)
            return False
        return row

    async def approve_callback(self, interaction: discord.Interaction):
        row = await self.check_pending(interaction)
        if not row:
            return
        guild = interaction.guild
        target = guild.get_member(row['target_id'])
        if target is None or not member_has_role(target, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('Участник не найден или больше не имеет роли «Сотрудник семьи».', ephemeral=True)
        new_role = get_role(guild, RANK_ROLE_IDS[row['new_rank']])
        if new_role is None:
            return await interaction.response.send_message('Роль нового звания не найдена. Проверьте rank_roles.', ephemeral=True)
        remove_roles = [r for rid in RANK_ROLE_IDS.values() if (r := guild.get_role(rid)) and r in target.roles and r.id != new_role.id]
        add_roles = [new_role] if new_role not in target.roles else []
        trainee_role = guild.get_role(TRAINEE_DEPT_ROLE_ID)
        if trainee_role:
            if row['new_rank'] in TRAINEE_UNTIL and trainee_role not in target.roles:
                add_roles.append(trainee_role)
            elif row['new_rank'] not in TRAINEE_UNTIL and trainee_role in target.roles:
                remove_roles.append(trainee_role)
        try:
            if remove_roles:
                await target.remove_roles(*remove_roles, reason=f'Повышение одобрено: {interaction.user}')
            if add_roles:
                await target.add_roles(*add_roles, reason=f'Повышение одобрено: {interaction.user}')
        except discord.Forbidden:
            return await interaction.response.send_message('Не хватает прав или роль бота ниже выдаваемой роли.', ephemeral=True)
        with db_connect() as con:
            con.execute("UPDATE promotions SET status='approved', reviewer_id=? WHERE id=? AND status='pending'", (interaction.user.id, self.request_id))
            updated = con.execute('SELECT * FROM promotions WHERE id=?', (self.request_id,)).fetchone()
        await interaction.response.edit_message(embed=build_request_embed(updated, 'approved', interaction.user), view=ClosedDecisionView())
        for uid, text in ((row['target_id'], f'💀 Ваш рапорт одобрен. Новое звание: {row["new_rank"]}.'),
                          (row['author_id'], f'💀 Ваш рапорт на участника <@{row["target_id"]}> одобрен. Новое звание: {row["new_rank"]}.')):
            try:
                user = await bot.fetch_user(uid)
                await user.send(text)
            except discord.HTTPException:
                pass

    async def reject_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not is_senior(interaction.user):
            return await interaction.response.send_message('Отклонять рапорты может только старший состав.', ephemeral=True)
        with db_connect() as con:
            row = con.execute('SELECT status FROM promotions WHERE id=?', (self.request_id,)).fetchone()
        if not row or row['status'] != 'pending':
            return await interaction.response.send_message('Этот рапорт уже рассмотрен.', ephemeral=True)
        await interaction.response.send_modal(RejectModal(self.request_id))


class ClosedDecisionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='Рассмотрено', emoji='🔒', style=discord.ButtonStyle.secondary, disabled=True))


@bot.event
async def on_ready():
    init_db()
    bot.add_view(PromotionPanel())
    # Re-register controls for all pending requests after a restart.
    with db_connect() as con:
        pending = con.execute("SELECT id, message_id FROM promotions WHERE status='pending'").fetchall()
    for row in pending:
        bot.add_view(DecisionView(row['id']), message_id=row['message_id'])
    try:
        await bot.tree.sync()
    except Exception:
        log.exception('Slash command sync failed')
    log.info('Logged in as %s; restored %s pending promotion views', bot.user, len(pending))


@bot.tree.command(name='setup_promotion', description='Опубликовать панель списков на повышение')
async def setup_promotion(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_senior(interaction.user):
        return await interaction.response.send_message('Настроить панель может только старший состав.', ephemeral=True)
    embed = discord.Embed(
        title='💀 КАРАТЕЛИ | СИСТЕМА ПОВЫШЕНИЯ',
        description='Подача и рассмотрение представлений на повышение. Заполните данные участника, выберите новое звание и укажите причину.',
        color=discord.Color.from_rgb(130, 20, 35),
    )
    embed.add_field(name='📝 Подача рапорта', value='Нажмите **«Отписать»** и заполните форму. Подать рапорт можно только при наличии роли **«Сотрудник семьи»**.', inline=False)
    embed.add_field(name='⚖️ Рассмотрение', value='Старший состав принимает решение прямо в канале списков на повышение.', inline=False)
    embed.set_footer(text='КАРАТЕЛИ · Кадровая система')
    await interaction.channel.send(embed=embed, view=PromotionPanel())
    await interaction.response.send_message('✅ Панель системы повышения опубликована.', ephemeral=True)


if __name__ == '__main__':
    init_db()
    bot.run(TOKEN)
