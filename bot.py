
import asyncio
import json
import logging
import os
import csv
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CATEGORY_ID = int(os.getenv("CATEGORY_ID", "0"))

DATA_FILE = Path(__file__).with_name("teams.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")

intents = discord.Intents.default()


class TeamBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.team_lock = asyncio.Lock()

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()

        logger.info("Synced %s slash commands", len(synced))


bot = TeamBot()


def load_data():
    if not DATA_FILE.exists():
        return {"guilds": {}}

    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_data(data):
    temp_file = DATA_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)

    os.replace(temp_file, DATA_FILE)

async def export_teams_csv(guild):
    csv_file = Path(__file__).with_name("team_members.csv")

    guild_data = get_guild_data(guild.id)

    rows = []

    for team_number, team in guild_data["teams"].items():
        for member_id in team["members"]:
            member = guild.get_member(member_id)

            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except (discord.NotFound, discord.Forbidden):
                    member = None

            if member:
                username = member.name
                display_name = member.display_name
            else:
                username = "Unknown"
                display_name = "Unknown"

            rows.append([
                team_number,
                username,
                display_name,
                str(member_id)
            ])

    rows.sort(key=lambda row: (int(row[0]), row[1].lower()))

    temp_file = csv_file.with_suffix(".tmp")

    with temp_file.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        writer = csv.writer(file)

        writer.writerow([
            "Team Number",
            "Discord Username",
            "Display Name",
            "Discord ID"
        ])

        writer.writerows(rows)

    os.replace(temp_file, csv_file)

data = load_data()


def get_guild_data(guild_id):
    guild_id = str(guild_id)

    if guild_id not in data["guilds"]:
        data["guilds"][guild_id] = {
            "last_team_number": 0,
            "teams": {}
        }

    return data["guilds"][guild_id]


def get_team(guild_id, number):
    guild_data = get_guild_data(guild_id)
    return guild_data["teams"].get(str(number))


def get_category(guild):
    category = guild.get_channel(CATEGORY_ID)

    if not isinstance(category, discord.CategoryChannel):
        return None

    return category


def get_member(interaction):
    if isinstance(interaction.user, discord.Member):
        return interaction.user

    return None

async def assign_jammer_role(member):
    role = discord.utils.get(
        member.guild.roles,
        name="jammers2026"
    )

    if role is None:
        logger.warning("Role jammers2026 not found")
        return False

    if role not in member.roles:
        try:
            await member.add_roles(
                role,
                reason="User interacted with the team bot"
            )
        except discord.HTTPException:
            logger.exception("Failed to assign jammers2026 role")
            return False

    return True


async def send_welcome(channel, member):
    await channel.send(
        f"New user, welcome to the team! {member.mention}",
        allowed_mentions=discord.AllowedMentions(
            users=True,
            roles=False,
            everyone=False
        )
    )


@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)

    for guild in bot.guilds:
        await export_teams_csv(guild)

    logger.info("Team members CSV updated")


@bot.tree.command(
    name="create_new_team",
    description="Create a new project team"
)
@app_commands.guild_only()
async def create_new_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = get_member(interaction)

    if guild is None or member is None:
        await interaction.followup.send(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return
    await assign_jammer_role(member)

    category = get_category(guild)

    if category is None:
        await interaction.followup.send(
            "Team category not found. Contact an administrator.",
            ephemeral=True
        )
        return

    async with bot.team_lock:
        guild_data = get_guild_data(guild.id)

        number = guild_data["last_team_number"] + 1

        role = None
        channel = None

        try:
            role = await guild.create_role(
                name=f"Project {number}",
                permissions=discord.Permissions.none(),
                hoist=True,
                mentionable=True,
                reason=f"Creating project team {number}"
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
                reason=f"Creating project team {number}"
            )

            await member.add_roles(
                role,
                reason=f"Created project team {number}"
            )

        except discord.HTTPException:
            logger.exception("Failed to create team %s", number)

            if channel is not None:
                try:
                    await channel.delete()
                except discord.HTTPException:
                    logger.exception("Failed to clean up channel")

            if role is not None:
                try:
                    await role.delete()
                except discord.HTTPException:
                    logger.exception("Failed to clean up role")

            await interaction.followup.send(
                "Failed to create the team. Check the bot permissions.",
                ephemeral=True
            )
            return

        guild_data["last_team_number"] = number

        guild_data["teams"][str(number)] = {
            "role_id": role.id,
            "channel_id": channel.id,
            "members": [member.id]
        }

        save_data(data)
        await export_teams_csv(guild)

        await interaction.followup.send(
            f"Now you're in team number {number}!\n"
            f"Role: {role.mention}\n"
            f"Channel: {channel.mention}",
            ephemeral=True
        )

        try:
            await send_welcome(channel, member)
        except discord.HTTPException:
            logger.exception(
                "Could not send welcome message for team %s",
                number
            )


@bot.tree.command(
    name="add_to_team",
    description="Join an existing project team"
)
@app_commands.describe(number="The number of the team to join")
@app_commands.guild_only()
async def add_to_team(
    interaction: discord.Interaction,
    number: int
):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = get_member(interaction)

    if guild is None or member is None:
        await interaction.followup.send(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return
    await assign_jammer_role(member)

    async with bot.team_lock:
        team = get_team(guild.id, number)

        if team is None:
            await interaction.followup.send(
                f"Team {number} does not exist.",
                ephemeral=True
            )
            return

        role = guild.get_role(team["role_id"])
        channel = guild.get_channel(team["channel_id"])

        if role is None:
            await interaction.followup.send(
                f"The role for team {number} no longer exists.",
                ephemeral=True
            )
            return

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"The channel for team {number} no longer exists.",
                ephemeral=True
            )
            return

        if member.id in team["members"] and role in member.roles:
            await interaction.followup.send(
                f"You are already in team {number}.",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(
                role,
                reason=f"Joined project team {number}"
            )

        except discord.HTTPException:
            logger.exception("Failed to add member to team")

            await interaction.followup.send(
                "Could not assign the team role.",
                ephemeral=True
            )
            return

        if member.id not in team["members"]:
            team["members"].append(member.id)

        save_data(data)
        await export_teams_csv(guild)

        await interaction.followup.send(
            f"You have joined team {number}!\n"
            f"Channel: {channel.mention}",
            ephemeral=True
        )

        try:
            await send_welcome(channel, member)
        except discord.HTTPException:
            logger.exception(
                "Could not send welcome message in team %s",
                number
            )


@bot.tree.command(
    name="remove_from_team",
    description="Leave a project team"
)
@app_commands.describe(number="The number of the team to leave")
@app_commands.guild_only()
async def remove_from_team(
    interaction: discord.Interaction,
    number: int
):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = get_member(interaction)

    if guild is None or member is None:
        await interaction.followup.send(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return
    await assign_jammer_role(member)

    async with bot.team_lock:
        team = get_team(guild.id, number)

        if team is None:
            await interaction.followup.send(
                f"Team {number} does not exist.",
                ephemeral=True
            )
            return

        role = guild.get_role(team["role_id"])

        if role is None:
            await interaction.followup.send(
                f"The role for team {number} no longer exists.",
                ephemeral=True
            )
            return

        if member.id not in team["members"] and role not in member.roles:
            await interaction.followup.send(
                f"You are not a member of team {number}.",
                ephemeral=True
            )
            return

        try:
            if role in member.roles:
                await member.remove_roles(
                    role,
                    reason=f"Left project team {number}"
                )

        except discord.HTTPException:
            logger.exception("Failed to remove member from team")

            await interaction.followup.send(
                "Could not remove the team role.",
                ephemeral=True
            )
            return

        if member.id in team["members"]:
            team["members"].remove(member.id)

        save_data(data)
        await export_teams_csv(guild)

        await interaction.followup.send(
            f"You have left team {number}.",
            ephemeral=True
        )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from .env")

    if not CATEGORY_ID:
        raise RuntimeError("CATEGORY_ID is missing from .env")

    bot.run(TOKEN)
