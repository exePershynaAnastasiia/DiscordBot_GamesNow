import asyncio
import csv
import json
import logging
import os
import re
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CATEGORY_ID = int(os.getenv("CATEGORY_ID", "0"))
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "teams.json"
CSV_FILE = DATA_DIR / "team_members.csv"
FIRST_TEAM_NUMBER = 6
MAX_CHANNELS_IN_CATEGORY = 50
JAMMER_ROLE_NAME = "JammerGNGJ18 2026"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")


def load_data():
    if not DATA_FILE.exists():
        return {"guilds": {}}
    with DATA_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_data():
    temp = DATA_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4, ensure_ascii=False)
    os.replace(temp, DATA_FILE)


data = load_data()


def get_guild_data(guild_id):
    key = str(guild_id)
    if key not in data["guilds"]:
        data["guilds"][key] = {
            "last_team_number": FIRST_TEAM_NUMBER - 1,
            "teams": {}
        }
    return data["guilds"][key]


def get_team(guild_id, number):
    return get_guild_data(guild_id)["teams"].get(str(number))


def member_id_of(entry):
    return int(entry["id"] if isinstance(entry, dict) else entry)


def team_member_ids(team):
    return [member_id_of(entry) for entry in team["members"]]


def next_team_number(guild, guild_data):
    numbers = [FIRST_TEAM_NUMBER - 1, int(guild_data["last_team_number"])]
    numbers.extend(int(key) for key in guild_data["teams"] if key.isdecimal())

    # Avoid reusing existing Project N roles or team-N channels if the JSON was reset
    for role in guild.roles:
        match = re.fullmatch(r"Project (\d+)", role.name, flags=re.IGNORECASE)
        if match:
            numbers.append(int(match.group(1)))
    for channel in guild.channels:
        if isinstance(channel, discord.TextChannel):
            match = re.fullmatch(r"team-(\d+)", channel.name)
            if match:
                numbers.append(int(match.group(1)))

    return max(numbers) + 1


def get_base_category(guild):
    category = guild.get_channel(CATEGORY_ID)
    return category if isinstance(category, discord.CategoryChannel) else None


async def find_or_create_category(guild):
    base = get_base_category(guild)

    if base is None:
        raise ValueError("Base category not found; check CATEGORY_ID")

    # Remove the trailing number from the base category
    match = re.fullmatch(r"(.+?)\s+(\d+)", base.name)

    if match:
        base_name = match.group(1)
        base_number = int(match.group(2))
    else:
        base_name = base.name
        base_number = 1

    # Find all numbered team categories
    candidates = []

    pattern = re.compile(
        rf"{re.escape(base_name)}\s+(\d+)$",
        re.IGNORECASE
    )

    for category in guild.categories:
        category_match = pattern.fullmatch(category.name)

        if category_match:
            category_number = int(category_match.group(1))

            if category_number >= base_number:
                candidates.append(
                    (category_number, category)
                )

    if not any(cat.id == base.id for _, cat in candidates):
        candidates.append((base_number, base))

    # Use the newest category first
    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    for _, category in candidates:
        if len(category.channels) < MAX_CHANNELS_IN_CATEGORY:
            return category

    # Create the next category when all existing ones are full
    next_index = max(
        number for number, _ in candidates
    ) + 1

    category = await guild.create_category(
        name=f"{base_name} {next_index}",
        overwrites=base.overwrites,
        reason="Project team category has reached its channel limit"
    )

    logger.info(
        "Created overflow category %s (%s)",
        category.name,
        category.id
    )

    return category


def get_interaction_member(interaction):
    return interaction.user if isinstance(interaction.user, discord.Member) else None


async def assign_jammer_role(member):
    role = discord.utils.get(member.guild.roles, name=JAMMER_ROLE_NAME)
    if role is None:
        logger.warning("Role %s not found", JAMMER_ROLE_NAME)
        return
    if role not in member.roles:
        try:
            await member.add_roles(role, reason="Used team bot")
        except discord.HTTPException:
            logger.exception("Cannot assign %s to %s", JAMMER_ROLE_NAME, member.id)


async def send_welcome(channel, member):
    await channel.send(
        f"New user, welcome to the team! {member.mention}",
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
    )


async def export_teams_csv(guild):
    guild_data = get_guild_data(guild.id)
    rows = []
    for team_number, team in guild_data["teams"].items():
        for entry in team["members"]:
            member_id = member_id_of(entry)
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.HTTPException:
                    member = None

            rows.append([
                int(team_number),
                member.name if member else "Unknown",
                member.display_name if member else "Unknown",
                str(member_id)
            ])

    rows.sort(key=lambda item: (item[0], item[1].lower()))
    temp = CSV_FILE.with_suffix(".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Team Number", "Discord Username", "Display Name", "Discord ID"])
        writer.writerows(rows)
    os.replace(temp, CSV_FILE)
    logger.info("Exported %s memberships for server %s", len(rows), guild.id)


async def save_and_export(guild):
    save_data()
    try:
        await export_teams_csv(guild)
    except (OSError, discord.HTTPException):
        logger.exception("JSON saved but CSV export failed")


class TeamBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.team_lock = asyncio.Lock()

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
        else:
            commands = await self.tree.sync()
        logger.info("Synced %s slash commands", len(commands))


bot = TeamBot()


@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)
    for guild in bot.guilds:
        async with bot.team_lock:
            try:
                await export_teams_csv(guild)
            except (OSError, discord.HTTPException):
                logger.exception("Failed CSV export on startup")


@bot.tree.command(name="create_new_team", description="Create a new project team")
@app_commands.guild_only()
async def create_new_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = get_interaction_member(interaction)
    if guild is None or member is None:
        await interaction.followup.send("Use this command in the server", ephemeral=True)
        return

    await assign_jammer_role(member)

    async with bot.team_lock:
        if get_base_category(guild) is None:
            await interaction.followup.send("Team category not found. Check CATEGORY_ID", ephemeral=True)
            return

        guild_data = get_guild_data(guild.id)
        number = next_team_number(guild, guild_data)
        role = None
        channel = None
        try:
            category = await find_or_create_category(guild)
            role = await guild.create_role(
                name=f"Project {number}",
                permissions=discord.Permissions.none(),
                hoist=True,
                mentionable=True,
                reason=f"Create project team {number}"
            )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )
            }
            channel = await guild.create_text_channel(
                name=f"team-{number}",
                category=category,
                overwrites=overwrites,
                topic=f"Public discussion channel for Project {number}",
                reason=f"Create project team {number}"
            )
            await member.add_roles(role, reason=f"Created team {number}")
        except (discord.HTTPException, ValueError):
            logger.exception("Failed to create team %s", number)
            if channel is not None:
                try:
                    await channel.delete(reason="Rollback incomplete team")
                except discord.HTTPException:
                    logger.exception("Could not roll back channel %s", channel.id)
            if role is not None:
                try:
                    await role.delete(reason="Rollback incomplete team")
                except discord.HTTPException:
                    logger.exception("Could not roll back role %s", role.id)
            await interaction.followup.send(
                "Could not create the team. Check channel limit and bot permissions",
                ephemeral=True
            )
            return

        guild_data["last_team_number"] = number
        guild_data["teams"][str(number)] = {
            "role_id": role.id,
            "channel_id": channel.id,
            "members": [member.id]
        }
        await save_and_export(guild)
        await interaction.followup.send(
            f"Now you're in team number {number}!\n"
            f"Role: {role.mention}\nChannel: {channel.mention}",
            ephemeral=True
        )
        try:
            await send_welcome(channel, member)
        except discord.HTTPException:
            logger.exception("Could not welcome team creator")



@bot.tree.command(name="join_team", description="Join an existing project team")
@app_commands.describe(number="Team number")
@app_commands.guild_only()
async def join_team(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = get_interaction_member(interaction)

    if guild is None or member is None:
        await interaction.followup.send(
            "Use this command in the server",
            ephemeral=True
        )
        return

    await assign_jammer_role(member)

    async with bot.team_lock:

        # Teams 1-4 were created manually
        if 1 <= number <= 4:
            role = discord.utils.get(
                guild.roles,
                name=f"Project{number}"
            )

            channel = discord.utils.get(
                guild.text_channels,
                name=f"team-{number}"
            )

            if role is None or channel is None:
                await interaction.followup.send(
                    f"Role or channel for team {number} was not found",
                    ephemeral=True
                )
                return

            if role in member.roles:
                await interaction.followup.send(
                    f"You are already in team {number}",
                    ephemeral=True
                )
                return

            try:
                await member.add_roles(
                    role,
                    reason=f"Joined team {number}"
                )
            except discord.HTTPException:
                logger.exception(
                    "Failed to add member to team %s",
                    number
                )
                await interaction.followup.send(
                    "Cannot assign team role",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                f"You have joined team {number}!\n"
                f"Channel: {channel.mention}",
                ephemeral=True
            )

            try:
                await send_welcome(channel, member)
            except discord.HTTPException:
                logger.exception(
                    "Could not welcome new team member"
                )

            return

        # Teams 5+ are managed through JSON
        team = get_team(guild.id, number)

        if team is None:
            await interaction.followup.send(
                f"Team {number} does not exist",
                ephemeral=True
            )
            return

        role = guild.get_role(int(team["role_id"]))
        channel = guild.get_channel(int(team["channel_id"]))

        if role is None or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Team role or channel is missing",
                ephemeral=True
            )
            return

        if member.id in team_member_ids(team) and role in member.roles:
            await interaction.followup.send(
                f"You are already in team {number}",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(
                role,
                reason=f"Joined team {number}"
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to add member to team %s",
                number
            )
            await interaction.followup.send(
                "Cannot assign team role",
                ephemeral=True
            )
            return

        if member.id not in team_member_ids(team):
            team["members"].append(member.id)

        await save_and_export(guild)

        await interaction.followup.send(
            f"You have joined team {number}!\n"
            f"Channel: {channel.mention}",
            ephemeral=True
        )

        try:
            await send_welcome(channel, member)
        except discord.HTTPException:
            logger.exception(
                "Could not welcome new team member"
            )


@bot.tree.command(
    name="remove_from_team",
    description="Leave a project team"
)
@app_commands.describe(number="Team number")
@app_commands.guild_only()
async def remove_from_team(
    interaction: discord.Interaction,
    number: int
):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = get_interaction_member(interaction)

    if guild is None or member is None:
        await interaction.followup.send(
            "Use this command in the server",
            ephemeral=True
        )
        return

    async with bot.team_lock:
        role = discord.utils.get(
            guild.roles,
            name=f"Project{number}"
        )

        if role is None:
            await interaction.followup.send(
                f"Team {number} does not exist",
                ephemeral=True
            )
            return

        if role not in member.roles:
            await interaction.followup.send(
                f"You are not in team {number}",
                ephemeral=True
            )
            return

        try:
            await member.remove_roles(
                role,
                reason=f"Left team {number}"
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to remove member from team %s",
                number
            )
            await interaction.followup.send(
                "Cannot remove team role",
                ephemeral=True
            )
            return

        team = get_team(guild.id, number)

        if team is not None:
            team["members"] = [
                entry
                for entry in team["members"]
                if member_id_of(entry) != member.id
            ]

            await save_and_export(guild)

        await interaction.followup.send(
            f"You have left team {number}",
            ephemeral=True
        )

@bot.tree.command(name="export_teams", description="Download team membership CSV")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def export_teams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    async with bot.team_lock:
        try:
            await export_teams_csv(guild)
            await interaction.followup.send(
                "Here is the current list of teams and members:",
                file=discord.File(CSV_FILE, filename="team_members.csv"),
                ephemeral=True
            )
        except (OSError, discord.HTTPException):
            logger.exception("Failed to export teams")
            await interaction.followup.send("CSV export failed. Check Railway logs", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing in environment variables")
    if not CATEGORY_ID:
        raise RuntimeError("CATEGORY_ID is missing in environment variables")
    bot.run(TOKEN)
