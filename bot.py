import json
import logging
import discord
from discord.ext import commands
from discord import app_commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('karateli-promotion')

with open('config.json', 'r', encoding='utf-8') as f:
    CFG = json.load(f)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

RANKS = CFG['rank_roles']  # {"Приближённый": ROLE_ID, ...}
EMPLOYEE_ROLE_ID = int(CFG['employee_role_id'])
TRAINEE_DEPT_ROLE_ID = int(CFG['trainee_department_role_id'])
SUBMISSIONS_CHANNEL_ID = int(CFG['submissions_channel_id'])
SENIOR_ROLE_IDS = {int(x) for x in CFG['senior_role_ids']}


def get_role(guild: discord.Guild, role_id: int):
    return guild.get_role(int(role_id))


def member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == int(role_id) for r in member.roles)


def current_rank(member: discord.Member):
    for name, role_id in RANKS.items():
        if member_has_role(member, int(role_id)):
            return name
    return 'Не определено'


def rank_role_id(name: str):
    return int(RANKS[name])


class ReasonModal(discord.ui.Modal, title='Причина повышения'):
    def __init__(self, target_id: int, rank_name: str, author_id: int):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.rank_name = rank_name
        self.author_id = author_id
        self.reason = discord.ui.TextInput(
            label='Причина повышения',
            placeholder='Опишите заслуги и причины повышения',
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=1000,
            required=True,
        )
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
            return await interaction.response.send_message('Канал заявок не найден. Проверьте submissions_channel_id.', ephemeral=True)
        rank = get_role(guild, rank_role_id(self.rank_name))
        if rank is None:
            return await interaction.response.send_message('Роль выбранного звания не найдена. Проверьте rank_roles.', ephemeral=True)
        embed = discord.Embed(title='💀 КАРАТЕЛИ | Рапорт на повышение', color=discord.Color.dark_red())
        embed.add_field(name='Участник', value=target.mention, inline=True)
        embed.add_field(name='Кто отписал', value=interaction.user.mention, inline=True)
        embed.add_field(name='Текущее звание', value=current_rank(target), inline=False)
        embed.add_field(name='На какое звание', value=rank.mention, inline=False)
        embed.add_field(name='Причина', value=str(self.reason.value), inline=False)
        embed.set_footer(text='Ожидает решения старшего состава')
        view = DecisionView(target.id, rank.id, interaction.user.id)
        msg = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(f'Рапорт отправлен на рассмотрение: {msg.jump_url}', ephemeral=True)


class RankSelect(discord.ui.Select):
    def __init__(self, target_id: int, author_id: int):
        options = [discord.SelectOption(label=name, value=name) for name in RANKS.keys()]
        super().__init__(placeholder='Выберите звание, на которое представляют', min_values=1, max_values=1, options=options)
        self.target_id = target_id
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message('Эта форма открыта не для вас.', ephemeral=True)
        await interaction.response.send_modal(ReasonModal(self.target_id, self.values[0], self.author_id))


class RankSelectView(discord.ui.View):
    def __init__(self, target_id: int, author_id: int):
        super().__init__(timeout=180)
        self.add_item(RankSelect(target_id, author_id))


class TargetSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder='Выберите участника, которого отписывают', min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if not member_has_role(interaction.user, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('Подать рапорт можно только с ролью «Сотрудник семьи».', ephemeral=True)
        target = interaction.guild.get_member(self.values[0].id)
        if target is None or not member_has_role(target, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('У выбранного участника должна быть роль «Сотрудник семьи».', ephemeral=True)
        if target.bot:
            return await interaction.response.send_message('Нельзя подать рапорт на бота.', ephemeral=True)
        await interaction.response.send_message('Выберите звание, на которое представляют участника:', view=RankSelectView(target.id, interaction.user.id), ephemeral=True)


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
        await interaction.response.send_message('Выберите участника, которого отписывают на повышение:', view=TargetSelectView(), ephemeral=True)


class DecisionView(discord.ui.View):
    def __init__(self, target_id: int, rank_id: int, author_id: int):
        super().__init__(timeout=None)
        self.target_id = target_id
        self.rank_id = rank_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not any(member_has_role(interaction.user, rid) for rid in SENIOR_ROLE_IDS):
            await interaction.response.send_message('Рассматривать рапорты может только старший состав.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Одобрить', emoji='✅', style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        target = guild.get_member(self.target_id)
        new_role = guild.get_role(self.rank_id)
        if target is None or new_role is None:
            return await interaction.response.send_message('Участник или роль звания не найдены.', ephemeral=True)
        if not member_has_role(target, EMPLOYEE_ROLE_ID):
            return await interaction.response.send_message('У участника больше нет роли «Сотрудник семьи». Повышение отменено.', ephemeral=True)
        old_rank_roles = [get_role(guild, int(rid)) for rid in RANKS.values()]
        remove_roles = [r for r in old_rank_roles if r and r in target.roles and r.id != new_role.id]
        add_roles = [] if new_role in target.roles else [new_role]
        trainee_role = guild.get_role(TRAINEE_DEPT_ROLE_ID)
        trainee_names = CFG.get('trainee_department_until_ranks', ['Стажёр', 'Приближённый'])
        # Keep the trainee-department role only for the configured early ranks.
        if trainee_role:
            if new_role.name in trainee_names and trainee_role not in target.roles:
                add_roles.append(trainee_role)
            elif new_role.name not in trainee_names and trainee_role in target.roles:
                remove_roles.append(trainee_role)
        try:
            if remove_roles:
                await target.remove_roles(*remove_roles, reason=f'Повышение одобрено: {interaction.user}')
            if add_roles:
                await target.add_roles(*add_roles, reason=f'Повышение одобрено: {interaction.user}')
        except discord.Forbidden:
            return await interaction.response.send_message('Не хватает прав или роль бота ниже выдаваемой роли.', ephemeral=True)
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name='Решение', value=f'✅ Одобрено: {interaction.user.mention}', inline=False)
        embed.set_footer(text='Рапорт одобрен')
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        try:
            await target.send(f'💀 Ваш рапорт на повышение одобрен. Новое звание: **{new_role.name}**.')
        except discord.Forbidden:
            pass
        author = guild.get_member(self.author_id)
        if author and author.id != target.id:
            try:
                await author.send(f'Ваш рапорт на участника {target} одобрен. Новое звание: **{new_role.name}**.')
            except discord.Forbidden:
                pass

    @discord.ui.button(label='Отклонить', emoji='❌', style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectModal(self))


class RejectModal(discord.ui.Modal, title='Отклонение рапорта'):
    def __init__(self, decision_view: DecisionView):
        super().__init__(timeout=300)
        self.decision_view = decision_view
        self.reason = discord.ui.TextInput(label='Причина отказа', style=discord.TextStyle.paragraph, min_length=2, max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        view = self.decision_view
        guild = interaction.guild
        target = guild.get_member(view.target_id)
        author = guild.get_member(view.author_id)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        # Modal submission has no source message in discord.py; update the original request via stored message id is required.
        if target:
            try:
                await target.send(f'Ваш рапорт на повышение отклонён. Причина: {self.reason.value}')
            except discord.Forbidden:
                pass
        if author and author.id != (target.id if target else 0):
            try:
                await author.send(f'Ваш рапорт на повышение отклонён. Причина: {self.reason.value}')
            except discord.Forbidden:
                pass
        await interaction.response.send_message('Рапорт отклонён. Уведомления отправлены в ЛС, если они открыты.', ephemeral=True)


@bot.event
async def on_ready():
    bot.add_view(PromotionPanel())
    log.info('Logged in as %s', bot.user)
    try:
        await bot.tree.sync()
    except Exception:
        log.exception('Slash command sync failed')


@bot.tree.command(name='setup_promotion', description='Опубликовать панель списков на повышение')
async def setup_promotion(interaction: discord.Interaction):
    if not any(member_has_role(interaction.user, rid) for rid in SENIOR_ROLE_IDS):
        return await interaction.response.send_message('Настроить панель может только старший состав.', ephemeral=True)
    embed = discord.Embed(
        title='💀 КАРАТЕЛИ | СИСТЕМА ПОВЫШЕНИЯ',
        description='Нажмите **«Отписать»**, выберите участника, желаемое звание и укажите причину. Рапорт поступит в канал списков на повышение.',
        color=discord.Color.dark_red(),
    )
    embed.add_field(name='📋 Порядок подачи', value='1. Выберите участника с ролью «Сотрудник семьи».\n2. Выберите звание, на которое его представляют.\n3. Укажите причину повышения.', inline=False)
    await interaction.response.send_message(embed=embed, view=PromotionPanel())


if __name__ == '__main__':
    bot.run(CFG['bot_token'])
