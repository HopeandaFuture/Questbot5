import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import asyncio
from typing import Optional, Dict, List
import json
import requests
import os
import webserver

# Bot configuration
TOKEN = None  # Set this through environment variables
PREFIX = '-'

# XP Level thresholds
LEVEL_THRESHOLDS = {
    1: 0,
    2: 100,
    3: 500,
    4: 1200,
    5: 2200,
    6: 3500,
    7: 5100,
    8: 7000,
    9: 9200,
    10: 11700
}

# Bot setup - With message content intent for full functionality
# NOTE: Requires "Message Content Intent" enabled in Discord Developer Portal
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True
intents.message_content = True  # Privileged intent - enable in Discord Developer Portal
intents.members = True  # Privileged intent - enable in Discord Developer Portal to read member roles

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

class QuestBot:
    def __init__(self):
        self.db_connection = None
        self.quest_ping_role_id = None
        self.quest_channel_id = None
        self.role_xp_assignments = {}
        self.optin_message_id = None
        self.optin_channel_id = None
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database for storing user XP and quest data"""
        self.db_connection = sqlite3.connect('quest_bot.db')
        cursor = self.db_connection.cursor()
        
        # Create users table for XP tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                UNIQUE(user_id, guild_id)
            )
        ''')
        
        # Create quests table for active quests
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quests (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                title TEXT,
                content TEXT,
                completed_users TEXT DEFAULT '[]',
                xp_reward INTEGER DEFAULT 50
            )
        ''')
        
        # Add xp_reward column to existing tables if it doesn't exist
        try:
            cursor.execute('ALTER TABLE quests ADD COLUMN xp_reward INTEGER DEFAULT 50')
        except sqlite3.OperationalError:
            # Column already exists, ignore error
            pass
        
        # Backfill old quests without xp_reward to use default 50 XP
        cursor.execute('UPDATE quests SET xp_reward = 50 WHERE xp_reward IS NULL')
        
        # Create whitelisted_channels table for channel restrictions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS whitelisted_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT,
                UNIQUE(guild_id, channel_id)
            )
        ''')
        
        self.db_connection.commit()
        
        # Create settings table for bot configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                quest_ping_role_id INTEGER,
                quest_channel_id INTEGER,
                role_xp_assignments TEXT DEFAULT '{}'
            )
        ''')
        
        # Migrate settings table to add new columns if they don't exist
        try:
            # Check if optin_message_id column exists
            cursor.execute("PRAGMA table_info(settings)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'optin_message_id' not in columns:
                cursor.execute('ALTER TABLE settings ADD COLUMN optin_message_id INTEGER')
                print("Added optin_message_id column to settings table")
                
            if 'optin_channel_id' not in columns:
                cursor.execute('ALTER TABLE settings ADD COLUMN optin_channel_id INTEGER')
                print("Added optin_channel_id column to settings table")
                
        except Exception as e:
            print(f"Database migration warning: {e}")
        
        # Create streak_role_gains table for tracking streak role accumulation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS streak_role_gains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                role_id INTEGER,
                role_name TEXT,
                xp_awarded INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.db_connection.commit()
    
    def get_user_data(self, user_id: int, guild_id: int):
        """Get user XP and level data"""
        if not self.db_connection:
            return {'xp': 0, 'level': 1}
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT xp, level FROM users WHERE user_id = ? AND guild_id = ?', (user_id, guild_id))
        result = cursor.fetchone()
        if result:
            return {'xp': result[0], 'level': result[1]}
        else:
            # Create new user entry
            cursor.execute('INSERT INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', (user_id, guild_id))
            self.db_connection.commit()
            return {'xp': 0, 'level': 1}
    
    def update_user_xp(self, user_id: int, guild_id: int, xp_change: int):
        """Update user base XP and recalculate level based on total XP"""
        if not self.db_connection:
            return 0, 1
        cursor = self.db_connection.cursor()
        current_data = self.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        new_base_xp = max(0, current_data['xp'] + xp_change)
        
        # Update base XP in database first
        cursor.execute('UPDATE users SET xp = ? WHERE user_id = ? AND guild_id = ?', 
                      (new_base_xp, user_id, guild_id))
        self.db_connection.commit()
        
        # Calculate level based on TOTAL XP (including roles), not just base XP
        total_xp = self.calculate_total_user_xp(user_id, guild_id)
        new_level = self.calculate_level(total_xp)
        
        # Update level in database if changed
        if old_level != new_level:
            cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                          (new_level, user_id, guild_id))
            self.db_connection.commit()
            asyncio.create_task(self.update_user_level_role(user_id, guild_id, old_level, new_level))
        
        return total_xp, new_level
    
    async def create_level_roles(self, guild):
        """Create level roles if they don't exist"""
        try:
            for level in range(1, 11):
                role_name = f"Level {level}"
                # Check if role already exists
                existing_role = discord.utils.get(guild.roles, name=role_name)
                if not existing_role:
                    # Create role with a color gradient from blue to gold
                    color_value = int(0x0099ff + (0xffd700 - 0x0099ff) * (level - 1) / 9)
                    await guild.create_role(
                        name=role_name,
                        color=discord.Color(color_value),
                        reason=f"Auto-created level role for Level {level}"
                    )
                    print(f"Created role: {role_name}")
        except discord.Forbidden:
            print("Bot lacks permission to create roles")
        except Exception as e:
            print(f"Error creating level roles: {e}")
    
    async def update_user_level_role(self, user_id: int, guild_id: int, old_level: int, new_level: int):
        """Update user's level role when they level up/down"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                return
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                return
            
            # Remove ALL existing level roles (not just the old one)
            removed_roles = []
            for role in member.roles:
                if role.name.startswith("Level ") and role.name != f"Level {new_level}":
                    removed_roles.append(role.name)
                    await member.remove_roles(role, reason="Level changed - removing old level role")
            
            # Add new level role
            new_role_name = f"Level {new_level}"
            new_role = discord.utils.get(guild.roles, name=new_role_name)
            if new_role:
                if new_role not in member.roles:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Removed {removed_roles} → Added {new_role_name}")
                else:
                    print(f"ℹ️ {member.display_name}: Already has {new_role_name}, removed {removed_roles}")
            else:
                # Create the role if it doesn't exist
                print(f"Creating missing level roles...")
                await self.create_level_roles(guild)
                new_role = discord.utils.get(guild.roles, name=new_role_name)
                if new_role:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Created and added {new_role_name}")
                else:
                    print(f"❌ Failed to create {new_role_name}")
        except discord.Forbidden as e:
            print(f"❌ Bot lacks permission to manage roles: {e}")
            print(f"   Make sure bot role is higher than Level roles in server settings!")
        except Exception as e:
            print(f"❌ Error updating user level role: {e}")
    
    def calculate_level(self, xp: int) -> int:
        """Calculate level based on XP"""
        for level in range(10, 0, -1):
            if xp >= LEVEL_THRESHOLDS[level]:
                return level
        return 1
    
    def calculate_total_user_xp(self, user_id: int, guild_id: int) -> int:
        """Calculate total XP including quest XP + role-based XP"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            # Get base XP from database (quest completions and manual additions)
            user_data = self.get_user_data(user_id, guild_id)
            base_xp = user_data.get('xp', 0)
            
            # Add XP from custom assigned roles (excluding streak roles which use accumulated system)
            custom_role_xp = 0
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation to avoid circular dependency
                if role.name.startswith("Level "):
                    continue
                    
                role_xp_data = self.get_role_xp_and_type(guild_id, str(role.id))
                if role_xp_data:
                    xp_amount, role_type = role_xp_data
                    # Skip streak roles since they now use accumulated XP system
                    if role_type != "streak":
                        custom_role_xp += xp_amount
            
            # Add XP from accumulated streak roles (historical gains)
            accumulated_streak_xp = self.get_accumulated_streak_xp(user_id, guild_id)
            
            # Add XP from current badge roles (only for unassigned roles that have "badge" in name)
            auto_role_xp = 0
            badge_roles_found = []
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation
                if role.name.startswith("Level "):
                    continue
                    
                role_name_lower = role.name.lower()
                role_id_str = str(role.id)
                role_xp_data = self.get_role_xp_and_type(guild_id, role_id_str)
                # Only apply auto-detection fallback if role doesn't have explicit assignment
                if not role_xp_data:
                    # Badge roles give 5 XP each (fallback for unassigned roles)
                    if "badge" in role_name_lower:
                        auto_role_xp += 5
                        badge_roles_found.append(role.name)
                    # Note: Streak roles now use accumulated XP instead of current roles
            
            # Calculate total XP - sum of base XP + all role bonuses + accumulated streak XP
            # NO level role XP to avoid circular dependency in level calculation
            total_xp = base_xp + custom_role_xp + auto_role_xp + accumulated_streak_xp
            
            # Log badge roles found for debugging
            if badge_roles_found:
                print(f"Role XP for {member.display_name}: Badge roles: {badge_roles_found}, Auto XP: {auto_role_xp}")
            if accumulated_streak_xp > 0:
                print(f"Accumulated Streak XP for {member.display_name}: {accumulated_streak_xp}")
            
            return total_xp
            
        except Exception as e:
            print(f"Error calculating total XP for user {user_id}: {e}")
            import traceback
            traceback.print_exc()
            # Fall back to database XP
            user_data = self.get_user_data(user_id, guild_id)
            return user_data.get('xp', 0)
    
    def get_leaderboard(self, guild_id: int, limit: int = 10):
        """Get top users for leaderboard with total XP including roles (opted-in users only)"""
        if not self.db_connection:
            return []
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT user_id, xp, level FROM users WHERE guild_id = ? ORDER BY xp DESC', (guild_id,))
        all_users = cursor.fetchall()
        
        # Calculate total XP for each user (including role bonuses) and filter for opted-in users only
        users_with_total_xp = []
        for user_id, base_xp, level in all_users:
            # Only include users who are opted into the bot
            if self.is_user_opted_in(user_id, guild_id):
                total_xp = self.calculate_total_user_xp(user_id, guild_id)
                new_level = self.calculate_level(total_xp)
                users_with_total_xp.append((user_id, total_xp, new_level))
        
        # Sort by total XP and limit results
        users_with_total_xp.sort(key=lambda x: x[1], reverse=True)
        return users_with_total_xp[:limit]
    
    def save_settings(self, guild_id: int):
        """Save bot settings to database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        role_xp_json = json.dumps(self.role_xp_assignments.get(guild_id, {}))
        cursor.execute('''
            INSERT OR REPLACE INTO settings 
            (guild_id, quest_ping_role_id, quest_channel_id, role_xp_assignments, optin_message_id, optin_channel_id) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (guild_id, self.quest_ping_role_id, self.quest_channel_id, role_xp_json, self.optin_message_id, self.optin_channel_id))
        self.db_connection.commit()
    
    def load_settings(self, guild_id: int):
        """Load bot settings from database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT quest_ping_role_id, quest_channel_id, role_xp_assignments, optin_message_id, optin_channel_id FROM settings WHERE guild_id = ?', (guild_id,))
        result = cursor.fetchone()
        if result:
            self.quest_ping_role_id = result[0]
            self.quest_channel_id = result[1]
            loaded_assignments = json.loads(result[2])
            self.optin_message_id = result[3] if len(result) > 3 else None
            self.optin_channel_id = result[4] if len(result) > 4 else None
            
            # Migrate old format to new format if needed
            migrated_assignments = {}
            for role_id, data in loaded_assignments.items():
                if isinstance(data, int):
                    # Old format: role_id -> xp_amount
                    # Migrate to new format: role_id -> {"xp": xp_amount, "type": "badge"}
                    # Default to "badge" for backward compatibility
                    migrated_assignments[role_id] = {"xp": data, "type": "badge"}
                else:
                    # New format: role_id -> {"xp": xp_amount, "type": "streak"|"badge"}
                    migrated_assignments[role_id] = data
            
            self.role_xp_assignments[guild_id] = migrated_assignments
    
    def record_streak_role_gain(self, user_id: int, guild_id: int, role_id: int, role_name: str, xp_awarded: int):
        """Record when a user gains a streak role for accumulation tracking"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('''
            INSERT INTO streak_role_gains (user_id, guild_id, role_id, role_name, xp_awarded)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, guild_id, role_id, role_name, xp_awarded))
        self.db_connection.commit()
        print(f"Recorded streak role gain: {role_name} (+{xp_awarded} XP) for user {user_id}")
    
    def get_accumulated_streak_xp(self, user_id: int, guild_id: int) -> int:
        """Get total accumulated streak XP from all historical role gains"""
        if not self.db_connection:
            return 0
        cursor = self.db_connection.cursor()
        cursor.execute('''
            SELECT SUM(xp_awarded) FROM streak_role_gains 
            WHERE user_id = ? AND guild_id = ?
        ''', (user_id, guild_id))
        result = cursor.fetchone()
        return result[0] if result[0] else 0
    
    def get_role_xp_and_type(self, guild_id: int, role_id: str):
        """Get XP amount and type for a role, returns (xp, type) or None if not assigned"""
        if guild_id not in self.role_xp_assignments:
            return None
        role_data = self.role_xp_assignments[guild_id].get(role_id)
        if role_data:
            return role_data["xp"], role_data["type"]
        return None
    
    def assign_role_xp(self, guild_id: int, role_id: str, xp_amount: int, role_type: str):
        """Assign XP and type to a role"""
        if guild_id not in self.role_xp_assignments:
            self.role_xp_assignments[guild_id] = {}
        self.role_xp_assignments[guild_id][role_id] = {"xp": xp_amount, "type": role_type}
    
    def unassign_role_xp(self, guild_id: int, role_id: str):
        """Remove XP assignment from a role"""
        if guild_id in self.role_xp_assignments and role_id in self.role_xp_assignments[guild_id]:
            del self.role_xp_assignments[guild_id][role_id]
    
    def is_user_opted_in(self, user_id: int, guild_id: int) -> bool:
        """Check if user is opted into the bot (has Level 1+ role)"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                return False
            
            member = guild.get_member(user_id)
            if not member:
                return False
            
            # Check if user has any Level role (Level 1, Level 2, etc.)
            for role in member.roles:
                if role.name.startswith("Level "):
                    return True
            return False
        except Exception as e:
            print(f"Error checking opt-in status for user {user_id}: {e}")
            return False
    
    def add_whitelisted_channel(self, guild_id: int, channel_id: int, channel_name: str):
        """Add a channel to the whitelist"""
        try:
            if self.db_connection:
                cursor = self.db_connection.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO whitelisted_channels (guild_id, channel_id, channel_name)
                    VALUES (?, ?, ?)
                ''', (guild_id, channel_id, channel_name))
                self.db_connection.commit()
                return True
        except Exception as e:
            print(f"Error adding whitelisted channel: {e}")
        return False
    
    def remove_whitelisted_channel(self, guild_id: int, channel_id: int):
        """Remove a channel from the whitelist"""
        try:
            if self.db_connection:
                cursor = self.db_connection.cursor()
                cursor.execute('''
                    DELETE FROM whitelisted_channels WHERE guild_id = ? AND channel_id = ?
                ''', (guild_id, channel_id))
                self.db_connection.commit()
                return cursor.rowcount > 0
        except Exception as e:
            print(f"Error removing whitelisted channel: {e}")
        return False
    
    def get_whitelisted_channels(self, guild_id: int):
        """Get all whitelisted channels for a guild"""
        try:
            if self.db_connection:
                cursor = self.db_connection.cursor()
                cursor.execute('''
                    SELECT channel_id, channel_name FROM whitelisted_channels WHERE guild_id = ?
                ''', (guild_id,))
                return cursor.fetchall()
        except Exception as e:
            print(f"Error getting whitelisted channels: {e}")
        return []
    
    def is_channel_whitelisted(self, guild_id: int, channel_id: int):
        """Check if a channel is whitelisted"""
        try:
            if self.db_connection:
                cursor = self.db_connection.cursor()
                cursor.execute('''
                    SELECT 1 FROM whitelisted_channels WHERE guild_id = ? AND channel_id = ?
                ''', (guild_id, channel_id))
                return cursor.fetchone() is not None
        except Exception as e:
            print(f"Error checking if channel is whitelisted: {e}")
        return True  # Default to allowing if error occurs
    
    def clear_whitelisted_channels(self, guild_id: int):
        """Clear all whitelisted channels for a guild"""
        try:
            if self.db_connection:
                cursor = self.db_connection.cursor()
                cursor.execute('DELETE FROM whitelisted_channels WHERE guild_id = ?', (guild_id,))
                self.db_connection.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"Error clearing whitelisted channels: {e}")
        return 0

quest_bot = QuestBot()

@bot.event
async def on_ready():
    print(f'{bot.user} has logged in to Discord!')
    for guild in bot.guilds:
        quest_bot.load_settings(guild.id)
        # Create level roles on startup
        await quest_bot.create_level_roles(guild)
        # Cache members to improve role reading
        try:
            await guild.chunk()
            print(f"Cached {guild.member_count} members for {guild.name}")
        except Exception as e:
            print(f"Failed to cache members for {guild.name}: {e}")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

async def is_channel_whitelisted_check(ctx):
    """Global check to enforce channel whitelist for commands"""
    # Always allow DMs
    if ctx.guild is None:
        return True
    
    # Always allow users with manage_guild permissions (staff/admin)
    if ctx.author.guild_permissions.manage_guild:
        return True
    
    # Always allow the whitelist command itself for staff
    if ctx.command and ctx.command.name == "whitelist":
        return True
    
    # Get whitelisted channels for this guild
    whitelisted_channels = quest_bot.get_whitelisted_channels(ctx.guild.id)
    
    # If no channels are whitelisted, allow all channels
    if not whitelisted_channels:
        return True
    
    # Check if current channel is whitelisted
    whitelisted_channel_ids = [channel_id for channel_id, _ in whitelisted_channels]
    if ctx.channel.id not in whitelisted_channel_ids:
        # Silently block command execution
        return False
    
    return True

# Add the global check to all commands
bot.add_check(is_channel_whitelisted_check)

async def send_whitelisted_message(channel, **kwargs):
    """Send a message only if the channel is whitelisted or no whitelist is active"""
    # Always allow DMs
    if not hasattr(channel, 'guild') or channel.guild is None:
        return await channel.send(**kwargs)
    
    # Get whitelisted channels for this guild
    whitelisted_channels = quest_bot.get_whitelisted_channels(channel.guild.id)
    
    # If no channels are whitelisted, allow all channels
    if not whitelisted_channels:
        return await channel.send(**kwargs)
    
    # Check if current channel is whitelisted
    whitelisted_channel_ids = [channel_id for channel_id, _ in whitelisted_channels]
    if channel.id in whitelisted_channel_ids:
        return await channel.send(**kwargs)
    
    # Silently skip sending to non-whitelisted channels
    return None

@bot.event
async def on_reaction_add(reaction, user):
    """Handle quest completion reactions and opt-in reactions"""
    if user.bot:
        return
    
    # Check if it's a quest completion (✅ emoji)
    if str(reaction.emoji) == '✅':
        # First check if this is an opt-in message (by message ID)
        if quest_bot.optin_message_id and reaction.message.id == quest_bot.optin_message_id:
            try:
                # This is an opt-in reaction
                guild = reaction.message.guild
                member = guild.get_member(user.id)
                
                if not member:
                    print(f"Could not find member {user.name} in guild {guild.name}")
                    return
                
                # Check if user already has a level role
                has_level_role = any(role.name.startswith("Level ") for role in member.roles)
                
                if not has_level_role:
                    print(f"Processing opt-in for user {user.name} (ID: {user.id})")
                    
                    # Assign Level 1 role
                    level_1_role = discord.utils.get(guild.roles, name="Level 1")
                    if level_1_role:
                        try:
                            await member.add_roles(level_1_role, reason="Opted into QuestBot system")
                            print(f"Successfully assigned Level 1 role to {user.name}")
                            
                            # Initialize user in database
                            if quest_bot.db_connection:
                                db_cursor = quest_bot.db_connection.cursor()
                                db_cursor.execute('INSERT OR IGNORE INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', 
                                             (user.id, guild.id))
                                quest_bot.db_connection.commit()
                                # Check if insert actually happened
                                if db_cursor.rowcount > 0:
                                    print(f"Successfully initialized {user.name} in database")
                                else:
                                    print(f"User {user.name} already exists in database")
                            else:
                                print("Warning: Database connection is None - user not initialized in database")
                            
                            # Send confirmation message
                            confirmation_embed = discord.Embed(
                                title="✅ Welcome to QuestBot!",
                                description=f"{user.mention} has opted into the QuestBot system!\nYou can now earn XP, complete quests, and appear on the leaderboard.",
                                color=0x00ff00
                            )
                            await send_whitelisted_message(reaction.message.channel, embed=confirmation_embed, delete_after=10)
                            print(f"User {user.name} successfully opted into QuestBot system")
                        except discord.Forbidden:
                            print(f"Failed to assign Level 1 role to {user.name} - insufficient permissions")
                        except Exception as e:
                            print(f"Error assigning Level 1 role to {user.name}: {e}")
                    else:
                        print("Level 1 role not found - creating level roles")
                        try:
                            await quest_bot.create_level_roles(guild)
                            level_1_role = discord.utils.get(guild.roles, name="Level 1")
                            if level_1_role:
                                await member.add_roles(level_1_role, reason="Opted into QuestBot system")
                                print(f"Successfully assigned newly created Level 1 role to {user.name}")
                                
                                if quest_bot.db_connection:
                                    db_cursor = quest_bot.db_connection.cursor()
                                    db_cursor.execute('INSERT OR IGNORE INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', 
                                                 (user.id, guild.id))
                                    quest_bot.db_connection.commit()
                                    # Check if insert actually happened
                                    if db_cursor.rowcount > 0:
                                        print(f"Successfully initialized {user.name} in database after role creation")
                                    else:
                                        print(f"User {user.name} already exists in database")
                                else:
                                    print("Warning: Database connection is None - user not initialized in database")
                                
                                # Send confirmation message
                                confirmation_embed = discord.Embed(
                                    title="✅ Welcome to QuestBot!",
                                    description=f"{user.mention} has opted into the QuestBot system!\nYou can now earn XP, complete quests, and appear on the leaderboard.",
                                    color=0x00ff00
                                )
                                await send_whitelisted_message(reaction.message.channel, embed=confirmation_embed, delete_after=10)
                                print(f"User {user.name} successfully opted into QuestBot system with new roles")
                            else:
                                print(f"Failed to create Level 1 role for {user.name}")
                        except Exception as e:
                            print(f"Error creating level roles for {user.name}: {e}")
                else:
                    print(f"User {user.name} already has a level role - skipping opt-in")
            except Exception as e:
                print(f"Error processing opt-in reaction for user {user.name}: {e}")
                import traceback
                print(f"Traceback: {traceback.format_exc()}")
            return
        
        # Check if it's a quest completion
        if not quest_bot.db_connection:
            return
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT title, completed_users, xp_reward FROM quests WHERE message_id = ?', (reaction.message.id,))
        quest_data = cursor.fetchone()
        
        if quest_data:
            # Only allow opted-in users to complete quests
            if not quest_bot.is_user_opted_in(user.id, reaction.message.guild.id):
                return
            
            title, completed_users_json, xp_reward = quest_data
            # Handle cases where xp_reward might be None for old quests
            if xp_reward is None:
                xp_reward = 50
                
            completed_users = json.loads(completed_users_json)
            
            if user.id not in completed_users:
                # Award XP based on quest's stored reward amount
                new_xp, new_level = quest_bot.update_user_xp(user.id, reaction.message.guild.id, xp_reward)
                completed_users.append(user.id)
                
                # Update quest completion list
                cursor.execute('UPDATE quests SET completed_users = ? WHERE message_id = ?', 
                              (json.dumps(completed_users), reaction.message.id))
                if quest_bot.db_connection:
                    quest_bot.db_connection.commit()
                
                # Send confirmation message
                embed = discord.Embed(
                    title="Quest Completed!",
                    description=f"{user.mention} completed: **{title}**\n+{xp_reward} XP (Total: {new_xp} XP, Level {new_level})",
                    color=0x00ff00
                )
                await send_whitelisted_message(reaction.message.channel, embed=embed, delete_after=10)

async def check_and_update_level_roles(user_id: int, guild_id: int, reason: str = "XP change"):
    """Comprehensive level role check and update function"""
    try:
        # Get current level in database
        current_data = quest_bot.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        
        # Calculate actual total XP and new level
        current_total_xp = quest_bot.calculate_total_user_xp(user_id, guild_id)
        new_level = quest_bot.calculate_level(current_total_xp)
        
        # Update level in database if changed and trigger level role assignment
        if old_level != new_level:
            if quest_bot.db_connection:
                cursor = quest_bot.db_connection.cursor()
                cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                              (new_level, user_id, guild_id))
                quest_bot.db_connection.commit()
                asyncio.create_task(quest_bot.update_user_level_role(user_id, guild_id, old_level, new_level))
            return old_level, new_level, current_total_xp
        
        return old_level, old_level, current_total_xp
    except Exception as e:
        print(f"Error in check_and_update_level_roles: {e}")
        return 1, 1, 0

@bot.event
async def on_member_update(before, after):
    """Handle role changes for automatic XP assignment"""
    guild_id = after.guild.id
    
    # Check for role changes (additions OR removals)
    added_roles = set(after.roles) - set(before.roles)
    removed_roles = set(before.roles) - set(after.roles)
    
    # Handle specific role additions (only for opted-in users)
    for role in added_roles:
        # Skip XP assignment for users who haven't opted in
        if not quest_bot.is_user_opted_in(after.id, guild_id):
            continue
            
        role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
        if role_xp_data:
            xp_reward, role_type = role_xp_data
            
            # Handle streak roles differently - accumulate each time they're gained
            if role_type == "streak":
                quest_bot.record_streak_role_gain(after.id, guild_id, role.id, role.name, xp_reward)
                
                # Check for level changes after streak accumulation
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "streak role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for streak role gain
                embed = discord.Embed(
                    title="🔥 Streak Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} Streak XP accumulated (Total: {total_xp} XP){level_text}",
                    color=0xff6600
                )
            else:
                # Check for level changes after badge role gain
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for badge role gain
                embed = discord.Embed(
                    title="🏅 Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} XP (Total: {total_xp} XP){level_text}",
                    color=0x0099ff
                )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break
        elif "badge" in role.name.lower():
            # Handle unassigned badge roles (fallback +5 XP) - only for opted-in users
            old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
            level_text = f" → Level {new_level}!" if old_level != new_level else ""
            
            embed = discord.Embed(
                title="🏅 Role Gained!",
                description=f"{after.mention} gained **{role.name}** role!\n+5 XP (Total: {total_xp} XP){level_text}",
                color=0x0099ff
            )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break

@bot.command(name='questbotoptin')
@commands.has_permissions(manage_roles=True)
async def questbot_optin(ctx, channel: Optional[discord.TextChannel] = None):
    """Create an opt-in embed message for users to join the QuestBot system (admin only)"""
    target_channel = channel if channel is not None else ctx.channel
    
    # Create opt-in embed
    embed = discord.Embed(
        title="🤖 QuestBot Opt-In",
        description="React with ✅ to join the QuestBot system and start earning XP!\n\n"
                   "**What you get by joining:**\n"
                   "• Earn XP by completing quests (50 XP each)\n"
                   "• Gain XP from badge and streak roles\n"
                   "• Appear on the server leaderboard\n"
                   "• Automatic level progression and role assignment\n"
                   "• Track your progress with detailed XP breakdown\n\n"
                   "**Note:** Only opted-in users will earn XP and appear on leaderboards.",
        color=0x0099ff
    )
    embed.add_field(name="📊 Level System", value="Level 1: 0 XP → Level 10: 11,700 XP", inline=False)
    embed.set_footer(text="React with ✅ below to opt in • This is required to use the bot")
    
    try:
        # Send embed to target channel
        optin_message = await target_channel.send(embed=embed)
        await optin_message.add_reaction('✅')
        
        # Store the opt-in message details
        quest_bot.optin_message_id = optin_message.id
        quest_bot.optin_channel_id = target_channel.id
        quest_bot.save_settings(ctx.guild.id)
        
        # Confirm to admin
        admin_embed = discord.Embed(
            title="✅ Opt-In Message Created",
            description=f"QuestBot opt-in message posted in {target_channel.mention}\n"
                       f"Users can now react with ✅ to join the system.",
            color=0x00ff00
        )
        await ctx.send(embed=admin_embed, delete_after=15)
        
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to send messages or add reactions in that channel!", delete_after=10)
    except Exception as e:
        await ctx.send(f"❌ Error creating opt-in message: {str(e)[:100]}", delete_after=10)

@bot.command(name='whitelist')
@commands.has_permissions(kick_members=True)
async def whitelist_channels(ctx, action: str = None, *channels):
    """Manage whitelisted channels where the bot can send messages and respond to commands (staff only)"""
    
    if not action:
        # Show current whitelisted channels
        whitelisted = quest_bot.get_whitelisted_channels(ctx.guild.id)
        
        if not whitelisted:
            embed = discord.Embed(
                title="📋 Channel Whitelist",
                description="No channels are currently whitelisted.\nThe bot can send messages in all channels.\n\n"
                           "**Usage:**\n"
                           "`-whitelist add <#channel1> <#channel2>...` - Add channels to whitelist\n"
                           "`-whitelist remove <#channel1> <#channel2>...` - Remove channels from whitelist\n"
                           "`-whitelist clear` - Clear all whitelisted channels\n"
                           "`-whitelist list` - Show current whitelist",
                color=0x0099ff
            )
        else:
            channel_list = []
            for channel_id, channel_name in whitelisted:
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    channel_list.append(f"• {channel.mention} ({channel.name})")
                else:
                    channel_list.append(f"• #{channel_name} (deleted)")
            
            embed = discord.Embed(
                title="📋 Channel Whitelist",
                description=f"Bot can only send messages and respond to commands in these channels:\n\n"
                           f"{chr(10).join(channel_list)}\n\n"
                           "**Usage:**\n"
                           "`-whitelist add <#channel1> <#channel2>...` - Add channels to whitelist\n"
                           "`-whitelist remove <#channel1> <#channel2>...` - Remove channels from whitelist\n"
                           "`-whitelist clear` - Clear all whitelisted channels",
                color=0x00ff00
            )
        
        await ctx.send(embed=embed)
        return
    
    action = action.lower()
    
    if action == "add":
        if not channels:
            await ctx.send("❌ Please specify channels to add to the whitelist!\n**Usage:** `-whitelist add <#channel1> <#channel2>...`", delete_after=10)
            return
        
        added_channels = []
        failed_channels = []
        
        for channel_mention in channels:
            # Extract channel ID from mention or use as-is if it's a number
            try:
                if channel_mention.startswith('<#') and channel_mention.endswith('>'):
                    channel_id = int(channel_mention[2:-1])
                else:
                    channel_id = int(channel_mention)
                
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    if quest_bot.add_whitelisted_channel(ctx.guild.id, channel.id, channel.name):
                        added_channels.append(channel.mention)
                    else:
                        failed_channels.append(channel_mention)
                else:
                    failed_channels.append(channel_mention)
            except ValueError:
                failed_channels.append(channel_mention)
        
        result_parts = []
        if added_channels:
            result_parts.append(f"✅ **Added to whitelist:** {', '.join(added_channels)}")
        if failed_channels:
            result_parts.append(f"❌ **Failed to add:** {', '.join(failed_channels)}")
        
        embed = discord.Embed(
            title="📋 Whitelist Updated",
            description="\n".join(result_parts),
            color=0x00ff00 if not failed_channels else 0xffaa00
        )
        await ctx.send(embed=embed, delete_after=15)
    
    elif action == "remove":
        if not channels:
            await ctx.send("❌ Please specify channels to remove from the whitelist!\n**Usage:** `-whitelist remove <#channel1> <#channel2>...`", delete_after=10)
            return
        
        removed_channels = []
        failed_channels = []
        
        for channel_mention in channels:
            try:
                if channel_mention.startswith('<#') and channel_mention.endswith('>'):
                    channel_id = int(channel_mention[2:-1])
                else:
                    channel_id = int(channel_mention)
                
                channel = ctx.guild.get_channel(channel_id)
                channel_name = channel.mention if channel else f"Channel ID: {channel_id}"
                
                if quest_bot.remove_whitelisted_channel(ctx.guild.id, channel_id):
                    removed_channels.append(channel_name)
                else:
                    failed_channels.append(channel_mention)
            except ValueError:
                failed_channels.append(channel_mention)
        
        result_parts = []
        if removed_channels:
            result_parts.append(f"✅ **Removed from whitelist:** {', '.join(removed_channels)}")
        if failed_channels:
            result_parts.append(f"❌ **Failed to remove:** {', '.join(failed_channels)}")
        
        embed = discord.Embed(
            title="📋 Whitelist Updated",
            description="\n".join(result_parts),
            color=0x00ff00 if not failed_channels else 0xffaa00
        )
        await ctx.send(embed=embed, delete_after=15)
    
    elif action == "clear":
        cleared_count = quest_bot.clear_whitelisted_channels(ctx.guild.id)
        embed = discord.Embed(
            title="📋 Whitelist Cleared",
            description=f"✅ Cleared {cleared_count} channel(s) from the whitelist.\nThe bot can now send messages in all channels.",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=15)
    
    elif action == "list":
        # Same as default behavior when no action is provided
        await whitelist_channels(ctx)
    
    else:
        await ctx.send("❌ Invalid action! Use: `add`, `remove`, `clear`, or `list`\n**Usage:** `-whitelist <action> [channels...]`", delete_after=10)


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Display the XP leaderboard (opted-in users only)"""
    try:
        leaderboard_data = quest_bot.get_leaderboard(ctx.guild.id, 10)
        
        if not leaderboard_data:
            embed = discord.Embed(
                title="📊 XP Leaderboard",
                description="No opted-in users found yet!\nUse `-questbotoptin` to create an opt-in message, or complete some quests to get on the leaderboard!",
                color=0xffd700
            )
            # Still show level requirements
            level_info = "**Level Requirements:**\n"
            for level, xp in LEVEL_THRESHOLDS.items():
                level_info += f"Level {level}: {xp:,} XP\n"
            embed.add_field(name="Level System", value=level_info, inline=False)
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title="🏆 XP Leaderboard",
            description="Top 10 Opted-In Quest Completers",
            color=0xffd700
        )
        
        medals = ["🥇", "🥈", "🥉"]
        users_added = 0
        
        for i, (user_id, xp, level) in enumerate(leaderboard_data):
            medal = medals[i] if i < 3 else f"#{i+1}"
            
            # Try multiple methods to get user info
            user = ctx.guild.get_member(user_id)
            if not user:
                user = bot.get_user(user_id)
            
            # Calculate total XP including role-based XP
            total_xp = quest_bot.calculate_total_user_xp(user_id, ctx.guild.id)
            
            if user:
                # Format username without pinging - use @ but escape it
                username = f"@{user.name}"
                display_name = getattr(user, 'display_name', user.name)
                if display_name != user.name:
                    username = f"@{user.name} ({display_name})"
                
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"{username}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
            else:
                # Last resort - show user ID
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"@User{str(user_id)[-4:]}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
        
        if users_added == 0:
            embed.add_field(
                name="No Active Users", 
                value="Opted-in users with XP may have left the server", 
                inline=False
            )
        
        # Add level requirements info
        level_info = "**Level Requirements:**\n"
        for level, xp_req in LEVEL_THRESHOLDS.items():
            level_info += f"Level {level}: {xp_req:,} XP\n"
        
        embed.add_field(name="Level System", value=level_info, inline=False)
        embed.set_footer(text="Only opted-in users appear on this leaderboard")
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"Error in leaderboard command: {e}")
        await ctx.send("❌ Could not retrieve leaderboard data. Please try again later.", delete_after=5)

@bot.command(name='checkXP')
async def check_xp(ctx):
    """Check your current XP and level progress (opted-in users only)"""
    try:
        # Regular users can only check their own XP
        target_member = ctx.author
        
        # Check if target user is opted in
        if not quest_bot.is_user_opted_in(target_member.id, ctx.guild.id):
            if target_member == ctx.author:
                embed = discord.Embed(
                    title="❌ Not Opted In",
                    description="You haven't opted into the QuestBot system yet!\n\n"
                               "Ask an admin to use `-questbotoptin` to create an opt-in message, "
                               "then react with ✅ to join the system and start earning XP!",
                    color=0xff0000
                )
            else:
                embed = discord.Embed(
                    title="❌ User Not Opted In",
                    description=f"{target_member.mention} hasn't opted into the QuestBot system yet.\n\n"
                               "Only users who have opted in can have their XP checked.",
                    color=0xff0000
                )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Get XP breakdown for detailed display
        guild = ctx.guild
        guild_id = guild.id
        
        # Calculate total XP and individual components
        current_xp = quest_bot.calculate_total_user_xp(target_member.id, guild_id)
        current_level = quest_bot.calculate_level(current_xp)
        
        # Get Quest XP (base XP from database - from completing quests)
        user_data = quest_bot.get_user_data(target_member.id, guild_id)
        quest_xp = user_data.get('xp', 0)
        
        # Get Streak XP (accumulated from streak roles)
        streak_xp = quest_bot.get_accumulated_streak_xp(target_member.id, guild_id)
        
        # Calculate Badge XP (from current badge roles)
        badge_xp = 0
        auto_badge_xp = 0
        
        for role in target_member.roles:
            if role.name.startswith("Level "):
                continue
                
            role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
            if role_xp_data:
                xp_amount, role_type = role_xp_data
                if role_type == "badge":
                    badge_xp += xp_amount
            else:
                # Auto-detection fallback for unassigned badge roles
                if "badge" in role.name.lower():
                    auto_badge_xp += 5
        
        # Total badge XP includes both assigned and auto-detected
        total_badge_xp = badge_xp + auto_badge_xp
        
        # Calculate XP needed for next level
        next_level = min(current_level + 1, 10)  # Cap at level 10
        next_level_xp = LEVEL_THRESHOLDS.get(next_level, LEVEL_THRESHOLDS[10])
        xp_needed = max(0, next_level_xp - current_xp)
        
        # Calculate progress percentage safely
        if current_level < 10:
            current_level_xp = LEVEL_THRESHOLDS.get(current_level, 0)
            xp_range = next_level_xp - current_level_xp
            if xp_range > 0:
                progress_percentage = min(100, max(0, ((current_xp - current_level_xp) / xp_range) * 100))
            else:
                progress_percentage = 100
        else:
            progress_percentage = 100
        
        embed = discord.Embed(
            title=f"📊 {target_member.display_name}'s XP Stats",
            color=0x00ff00
        )
        
        # Main stats row
        embed.add_field(name="💰 Total XP", value=f"{current_xp:,} XP", inline=True)
        embed.add_field(name="⭐ Current Level", value=f"Level {current_level}", inline=True)
        embed.add_field(name="📈 Progress", value=f"{progress_percentage:.1f}%" if current_level < 10 else "MAX", inline=True)
        
        # XP breakdown section
        embed.add_field(name="🏆 Quest XP", value=f"{quest_xp:,} XP", inline=True)
        embed.add_field(name="🔥 Streak XP", value=f"{streak_xp:,} XP", inline=True) 
        embed.add_field(name="🎖️ Badge XP", value=f"{total_badge_xp:,} XP", inline=True)
        
        if current_level < 10:
            embed.add_field(name="🎯 XP to Next Level", value=f"{xp_needed:,} XP needed", inline=True)
            
            # Progress bar with safe calculation
            progress_bar_length = 20
            filled_length = int(progress_bar_length * progress_percentage / 100)
            filled_length = max(0, min(filled_length, progress_bar_length))  # Clamp values
            bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
            embed.add_field(
                name="📈 Level Progress", 
                value=f"`{bar}` {progress_percentage:.1f}%", 
                inline=False
            )
        else:
            embed.add_field(name="🏆 Status", value="**MAX LEVEL REACHED!**", inline=True)
            embed.add_field(name="📈 Level Progress", value="`████████████████████` 100%", inline=False)
        
        # Safe avatar handling
        try:
            if target_member.avatar:
                embed.set_thumbnail(url=target_member.avatar.url)
            else:
                embed.set_thumbnail(url=target_member.default_avatar.url)
        except:
            pass  # Skip thumbnail if there are issues
        
        embed.set_footer(text="Quest XP: Complete quests (50 each) • Streak XP: Gain streak roles • Badge XP: Current badge roles")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        import traceback
        print(f"Error in checkXP command: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        await ctx.send(f"❌ Could not retrieve XP data. Error: {str(e)[:100]}...", delete_after=10)

@bot.command(name='checkmemberXP')
@commands.has_permissions(kick_members=True)
async def check_member_xp(ctx, member: discord.Member):
    """Check another member's XP and level progress (staff only)"""
    try:
        target_member = member
        
        # Check if target user is opted in
        if not quest_bot.is_user_opted_in(target_member.id, ctx.guild.id):
            embed = discord.Embed(
                title="❌ User Not Opted In",
                description=f"{target_member.mention} hasn't opted into the QuestBot system yet.\n\n"
                           "Only users who have opted in can have their XP checked.",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return

        # Get XP breakdown for detailed display
        guild = ctx.guild
        guild_id = guild.id
        
        # Calculate total XP and individual components
        current_xp = quest_bot.calculate_total_user_xp(target_member.id, guild_id)
        current_level = quest_bot.calculate_level(current_xp)
        
        # Get Quest XP (base XP from database - from completing quests)
        user_data = quest_bot.get_user_data(target_member.id, guild_id)
        quest_xp = user_data.get('xp', 0)
        
        # Get Streak XP (accumulated from streak roles)
        streak_xp = quest_bot.get_accumulated_streak_xp(target_member.id, guild_id)
        
        # Calculate Badge XP (from current badge roles)
        badge_xp = 0
        auto_badge_xp = 0
        
        for role in target_member.roles:
            if role.name.startswith("Level "):
                continue
                
            role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
            if role_xp_data:
                xp_amount, role_type = role_xp_data
                if role_type == "badge":
                    badge_xp += xp_amount
            else:
                # Auto-detection fallback for unassigned badge roles
                if "badge" in role.name.lower():
                    auto_badge_xp += 5
        
        # Total badge XP includes both assigned and auto-detected
        total_badge_xp = badge_xp + auto_badge_xp
        
        # Calculate XP needed for next level
        next_level = min(current_level + 1, 10)  # Cap at level 10
        next_level_xp = LEVEL_THRESHOLDS.get(next_level, LEVEL_THRESHOLDS[10])
        xp_needed = max(0, next_level_xp - current_xp)
        
        # Calculate progress percentage safely
        if current_level < 10:
            current_level_xp = LEVEL_THRESHOLDS.get(current_level, 0)
            xp_range = next_level_xp - current_level_xp
            if xp_range > 0:
                progress_percentage = min(100, max(0, ((current_xp - current_level_xp) / xp_range) * 100))
            else:
                progress_percentage = 100
        else:
            progress_percentage = 100
        
        embed = discord.Embed(
            title=f"📊 {target_member.display_name}'s XP Stats",
            color=0x00ff00
        )
        
        # Main stats row
        embed.add_field(name="💰 Total XP", value=f"{current_xp:,} XP", inline=True)
        embed.add_field(name="⭐ Current Level", value=f"Level {current_level}", inline=True)
        
        if current_level < 10:
            embed.add_field(name="🎯 Next Level", value=f"{xp_needed:,} XP needed", inline=True)
        else:
            embed.add_field(name="🏆 Max Level", value="Reached Level 10!", inline=True)
        
        # XP Breakdown
        xp_breakdown = []
        if quest_xp > 0:
            xp_breakdown.append(f"🏆 Quest XP: {quest_xp:,}")
        if streak_xp > 0:
            xp_breakdown.append(f"⚡ Streak XP: {streak_xp:,}")
        if total_badge_xp > 0:
            if badge_xp > 0 and auto_badge_xp > 0:
                xp_breakdown.append(f"🎖️ Badge XP: {badge_xp:,} + {auto_badge_xp:,} auto = {total_badge_xp:,}")
            elif badge_xp > 0:
                xp_breakdown.append(f"🎖️ Badge XP: {badge_xp:,}")
            else:
                xp_breakdown.append(f"🎖️ Badge XP: {auto_badge_xp:,} auto")
        
        if xp_breakdown:
            embed.add_field(
                name="📈 XP Breakdown",
                value="\n".join(xp_breakdown),
                inline=False
            )
        
        # Progress bar
        if current_level < 10:
            progress_bar_length = 20
            filled_length = int(progress_bar_length * (progress_percentage / 100))
            progress_bar = '█' * filled_length + '▒' * (progress_bar_length - filled_length)
            embed.add_field(
                name="📊 Level Progress",
                value=f"`{progress_bar}` {progress_percentage:.1f}%",
                inline=False
            )
        
        embed.set_footer(text="Quest XP: Complete quests (50 each) • Streak XP: Gain streak roles • Badge XP: Current badge roles")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        import traceback
        print(f"Error in checkmemberXP command: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        await ctx.send(f"❌ Could not retrieve XP data. Error: {str(e)[:100]}...", delete_after=10)

@bot.command(name='addXP')
@commands.has_permissions(manage_roles=True)
async def add_xp_command(ctx, amount: int, member: discord.Member):
    """Add XP to a user (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can receive XP. Ask them to react to the opt-in message first.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Update user XP
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, amount)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Added",
            description=f"Added {amount:,} XP to {member.mention}\n"
                       f"**New Total:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error adding XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='removeXP')
@commands.has_permissions(manage_roles=True)
async def remove_xp_command(ctx, amount: int, member: discord.Member):
    """Remove XP from a user (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can have XP modified.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Remove user XP (negative amount)
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, -amount)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Removed",
            description=f"Removed {amount:,} XP from {member.mention}\n"
                       f"**New Total:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error removing XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='setXP')
@commands.has_permissions(manage_roles=True)
async def set_xp_command(ctx, amount: int, member: discord.Member):
    """Set a user's XP to a specific amount (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can have XP modified.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Get current XP to calculate the difference
        current_data = quest_bot.get_user_data(member.id, ctx.guild.id)
        current_base_xp = current_data.get('xp', 0)
        xp_change = amount - current_base_xp
        
        # Update user XP to the target amount
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, xp_change)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Set",
            description=f"Set {member.mention}'s base XP to {amount:,} XP\n"
                       f"**Total XP:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error setting XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='questbot')
async def questbot_ping(ctx):
    """Ping the bot to check if it's online"""
    await ctx.send("online")

@bot.command(name='commands')
async def commands_list(ctx):
    """Display a comprehensive list of all available commands organized by permissions"""
    embed = discord.Embed(
        title="🤖 QuestBot Commands",
        description="",
        color=0x0099ff
    )
    
    # User Commands (Anyone can use)
    user_commands = [
        "`-allquests` - List all current quests by name",
        "`-leaderboard` - Display XP rankings", 
        "`-checkXP` - Check your XP and level progress",
        "`-questbot` - Ping bot to check if online",
        "`-commands` - Display this help message"
    ]
    
    embed.add_field(
        name="👥 User Commands (Everyone)",
        value="\n".join(user_commands),
        inline=False
    )
    
    
    # XP System Information
    xp_info = [
        "**Quest XP:** 50 XP (default) per completed quest (one-time per quest)",
        "**Streak XP:** Earned each time you gain a streak role (accumulates)",
        "**Badge XP:** Added to total while you have badge roles (non-duplicating)",
        "",
        "**Total XP = Quest XP + Streak XP + Badge XP**"
    ]
    
    embed.add_field(
        name="📊 XP System Types",
        value="\n".join(xp_info),
        inline=False
    )
    
    
    embed.set_footer(text="💡 Tip: React with ✅ to opt-in messages to start earning XP!")
    
    await ctx.send(embed=embed)

@bot.command(name='staffcommands')
@commands.has_permissions(manage_roles=True)
async def staff_commands_list(ctx):
    """Display staff and admin commands (staff only)"""
    embed = discord.Embed(
        title="🛡️ QuestBot Staff Commands",
        description="Staff and Admin commands for managing the bot:",
        color=0xff6600
    )
    
    # Staff Commands (Staff permissions required)
    staff_commands = [
        "`-addXP <amount> <member>` - Add XP to user",
        "`-removeXP <amount> <member>` - Remove XP from user",
        "`-setXP <amount> <member>` - Set user's XP to specific amount",
        "`-checkmemberXP <member>` - Check another member's XP and level progress",
        "`-addquest <title> <content> <amount>` - Create new quest embed (defaults to 50 XP)",
        "`-removequest <message_id>` - Delete quest by message ID",
        "`-checkroleXP <role_name_or_id>` - Check XP assigned to role (no pings!)",
        "`-whitelist <action> [channels...]` - Manage bot channel restrictions",
        "`-staffcommands` - Display this staff command list",
        "",
        "**Role XP Management:**",
        "`-assignroleXP <amount> <role> <type>` - Assign XP to single role",
        "`-assignstreakXP <amount> <role1> <role2>...` - Assign streak XP to multiple roles",
        "`-assignbadgeXP <amount> <role1> <role2>...` - Assign badge XP to multiple roles",
        "`-unassignroleXP <role1> <role2>...` - Remove XP assignment from roles"
    ]
    
    embed.add_field(
        name="👮 Staff Only Commands",
        value="\n".join(staff_commands),
        inline=False
    )
    
    # Admin Commands (Manage roles permission required)
    admin_commands = [
        "**Quest Management:**",
        "`-deleteallquests` - Delete all current quests",
        "",
        "**Bot Configuration:**", 
        "`-questping <role_id_or_name>` - Set quest ping role",
        "`-questchannel <channel_id_or_name>` - Set quest channel",
        "`-questbotoptin <channel>` - Create opt-in message"
    ]
    
    embed.add_field(
        name="🛡️ Admin Only Commands (Manage Roles)",
        value="\n".join(admin_commands),
        inline=False
    )
    
    embed.set_footer(text="💡 All staff commands require appropriate permissions to use.")
    
    await ctx.send(embed=embed)

@bot.command(name='questping')
@commands.has_permissions(manage_roles=True)
async def quest_ping(ctx, *, role_input: str):
    """Set quest ping role with @ mention or role ID (admin only)"""
    try:
        role = None
        
        # Try to parse as a role mention first
        if role_input.startswith('<@&') and role_input.endswith('>'):
            # Extract role ID from mention <@&123456789>
            role_id = int(role_input[3:-1])
            role = ctx.guild.get_role(role_id)
        else:
            # Try to parse as raw role ID
            try:
                role_id = int(role_input)
                role = ctx.guild.get_role(role_id)
            except ValueError:
                # Try to find role by name if not a valid ID
                role = discord.utils.get(ctx.guild.roles, name=role_input)
        
        if not role:
            embed = discord.Embed(
                title="❌ Role Not Found",
                description=f"Could not find a role matching: `{role_input}`\n\n"
                           "**Usage:**\n"
                           "• `-questping @RoleName` (mention the role)\n"
                           "• `-questping 123456789` (use role ID)\n"
                           "• `-questping RoleName` (use role name)",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Save the quest ping role
        quest_bot.quest_ping_role_id = role.id
        quest_bot.save_settings(ctx.guild.id)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ Quest Ping Role Set",
            description=f"Quest ping role has been set to: {role.mention}\n"
                       f"**Role:** {role.name}\n"
                       f"**ID:** {role.id}\n\n"
                       "This role will be pinged when new quests are created.",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=15)
        
    except ValueError:
        embed = discord.Embed(
            title="❌ Invalid Input",
            description="Please provide a valid role mention, role ID, or role name.\n\n"
                       "**Examples:**\n"
                       "• `-questping @Questors`\n"
                       "• `-questping 123456789`\n"
                       "• `-questping Questors`",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=15)
    except Exception as e:
        await ctx.send(f"❌ Error setting quest ping role: {str(e)[:100]}", delete_after=10)

@bot.command(name='questchannel')
@commands.has_permissions(manage_roles=True)
async def quest_channel(ctx, *, channel_input: str):
    """Set quest channel with # mention or channel ID (admin only)"""
    try:
        channel = None
        
        # Try to parse as a channel mention first
        if channel_input.startswith('<#') and channel_input.endswith('>'):
            # Extract channel ID from mention <#123456789>
            channel_id = int(channel_input[2:-1])
            channel = ctx.guild.get_channel(channel_id)
        else:
            # Try to parse as raw channel ID
            try:
                channel_id = int(channel_input)
                channel = ctx.guild.get_channel(channel_id)
            except ValueError:
                # Try to find channel by name if not a valid ID
                channel = discord.utils.get(ctx.guild.text_channels, name=channel_input)
        
        if not channel:
            embed = discord.Embed(
                title="❌ Channel Not Found",
                description=f"Could not find a text channel matching: `{channel_input}`\n\n"
                           "**Usage:**\n"
                           "• `-questchannel #channel-name` (mention the channel)\n"
                           "• `-questchannel 123456789` (use channel ID)\n"
                           "• `-questchannel channel-name` (use channel name)",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Ensure it's a text channel
        if not isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="❌ Invalid Channel Type",
                description="Quest channel must be a text channel, not a voice or category channel.",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=10)
            return
        
        # Save the quest channel
        quest_bot.quest_channel_id = channel.id
        quest_bot.save_settings(ctx.guild.id)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ Quest Channel Set",
            description=f"Quest channel has been set to: {channel.mention}\n"
                       f"**Channel:** #{channel.name}\n"
                       f"**ID:** {channel.id}\n\n"
                       "New quests will be posted in this channel.",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=15)
        
    except ValueError:
        embed = discord.Embed(
            title="❌ Invalid Input",
            description="Please provide a valid channel mention, channel ID, or channel name.\n\n"
                       "**Examples:**\n"
                       "• `-questchannel #general`\n"
                       "• `-questchannel 123456789`\n"
                       "• `-questchannel general`",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=15)
    except Exception as e:
        await ctx.send(f"❌ Error setting quest channel: {str(e)[:100]}", delete_after=10)

@bot.command(name='addquest')
@commands.has_permissions(kick_members=True)
async def add_quest(ctx, title: str, *args):
    """Create new quest embed with custom XP (staff only)"""
    try:
        # Parse arguments - last argument might be XP amount if it's a number
        if not args:
            await ctx.send("❌ Please provide quest content!\n**Usage:** `-addquest <title> <content> [amount]`", delete_after=10)
            return
            
        # Check if last argument is a number (XP amount)
        xp = 50  # Default XP
        content_parts = list(args)
        
        if len(args) > 1 and args[-1].isdigit():
            xp = int(args[-1])
            content_parts = args[:-1]  # Remove XP from content
        
        # Join remaining arguments as content
        content = " ".join(content_parts)
        
        # Validate XP amount
        if xp < 0:
            await ctx.send("❌ XP amount cannot be negative!", delete_after=10)
            return
        if xp > 10000:
            await ctx.send("❌ XP amount cannot exceed 10,000!", delete_after=10)
            return
        # Get quest channel if set
        target_channel = ctx.channel
        if quest_bot.quest_channel_id:
            quest_channel = ctx.guild.get_channel(quest_bot.quest_channel_id)
            if quest_channel:
                target_channel = quest_channel
        
        # Create quest embed
        embed = discord.Embed(
            title=f"🏆 {title}",
            description=content,
            color=0x0099ff
        )
        embed.add_field(name="💰 Reward", value=f"{xp} XP", inline=True)
        embed.add_field(name="📝 How to Complete", value="React with ✅ below", inline=True)
        embed.set_footer(text="React with ✅ to complete this quest • Must be opted-in to earn XP")
        
        # Prepare quest role ping if set
        ping_text = ""
        if quest_bot.quest_ping_role_id:
            ping_role = ctx.guild.get_role(quest_bot.quest_ping_role_id)
            if ping_role:
                ping_text = f"🔔 {ping_role.mention} - New quest available!\n\n"
        
        # Send quest message with ping before embed
        quest_message = await target_channel.send(content=ping_text, embed=embed)
        await quest_message.add_reaction('✅')
        
        # Store quest in database
        if quest_bot.db_connection:
            cursor = quest_bot.db_connection.cursor()
            cursor.execute('''
                INSERT INTO quests (message_id, guild_id, channel_id, title, content, completed_users, xp_reward)
                VALUES (?, ?, ?, ?, ?, '[]', ?)
            ''', (quest_message.id, ctx.guild.id, target_channel.id, title, content, xp))
            quest_bot.db_connection.commit()
        
        # Confirmation message
        embed = discord.Embed(
            title="✅ Quest Created",
            description=f"Quest **{title}** has been created in {target_channel.mention}",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error creating quest: {str(e)[:100]}", delete_after=10)

@bot.command(name='removequest')
@commands.has_permissions(kick_members=True)
async def remove_quest(ctx, message_id: int):
    """Delete quest by message ID (staff only)"""
    try:
        if not quest_bot.db_connection:
            await ctx.send("❌ Database not available", delete_after=10)
            return
        
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT channel_id, title FROM quests WHERE message_id = ?', (message_id,))
        quest_data = cursor.fetchone()
        
        if not quest_data:
            embed = discord.Embed(
                title="❌ Quest Not Found",
                description=f"No quest found with message ID: `{message_id}`\n\n"
                           "**Tip:** Right-click on a quest message and select 'Copy Message ID' to get the ID",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        channel_id, title = quest_data
        
        # Try to delete the actual message
        try:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                message = await channel.fetch_message(message_id)
                await message.delete()
        except discord.NotFound:
            pass  # Message already deleted
        except Exception as e:
            print(f"Could not delete quest message: {e}")
        
        # Remove from database
        cursor.execute('DELETE FROM quests WHERE message_id = ?', (message_id,))
        quest_bot.db_connection.commit()
        
        embed = discord.Embed(
            title="✅ Quest Removed",
            description=f"Quest **{title}** has been removed from the database and deleted from the channel.",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error removing quest: {str(e)[:100]}", delete_after=10)

@bot.command(name='deleteallquests')
@commands.has_permissions(manage_roles=True)
async def delete_all_quests(ctx):
    """Delete all current quests (admin only)"""
    try:
        if not quest_bot.db_connection:
            await ctx.send("❌ Database not available", delete_after=10)
            return
        
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT message_id, channel_id, title FROM quests WHERE guild_id = ?', (ctx.guild.id,))
        quests = cursor.fetchall()
        
        if not quests:
            embed = discord.Embed(
                title="❌ No Quests Found",
                description="There are no active quests to delete.",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=10)
            return
        
        # Safety confirmation prompt
        embed = discord.Embed(
            title="⚠️ Delete All Quests Confirmation",
            description=f"**WARNING:** You are about to delete **{len(quests)} quest(s)** from this server.\n\n"
                       f"**This action will:**\n"
                       f"• Delete all quest messages from channels\n"
                       f"• Remove all quest records from the database\n"
                       f"• **Cannot be undone**\n\n"
                       f"React with ✅ to confirm or ❌ to cancel.",
            color=0xff6600
        )
        
        confirmation_msg = await ctx.send(embed=embed)
        await confirmation_msg.add_reaction('✅')
        await confirmation_msg.add_reaction('❌')
        
        def check(reaction, user):
            return (user == ctx.author and 
                   str(reaction.emoji) in ['✅', '❌'] and 
                   reaction.message.id == confirmation_msg.id)
        
        try:
            reaction, user = await bot.wait_for('reaction_add', timeout=30.0, check=check)
            
            if str(reaction.emoji) == '❌':
                embed = discord.Embed(
                    title="❌ Operation Cancelled",
                    description="Quest deletion has been cancelled.",
                    color=0xff0000
                )
                await confirmation_msg.edit(embed=embed)
                await confirmation_msg.clear_reactions()
                return
            
            # User confirmed with ✅, proceed with deletion
            deleted_count = 0
            failed_deletes = []
            
            # Delete Discord messages
            for message_id, channel_id, title in quests:
                try:
                    channel = ctx.guild.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                        deleted_count += 1
                except discord.NotFound:
                    # Message already deleted, still count as success
                    deleted_count += 1
                except Exception as e:
                    failed_deletes.append(f"{title} (ID: {message_id})")
                    print(f"Could not delete quest message {message_id}: {e}")
            
            # Delete all quest records from database
            cursor.execute('DELETE FROM quests WHERE guild_id = ?', (ctx.guild.id,))
            quest_bot.db_connection.commit()
            
            # Success message
            embed = discord.Embed(
                title="✅ All Quests Deleted",
                description=f"**Successfully deleted {len(quests)} quest(s)**\n"
                           f"• Discord messages deleted: {deleted_count}/{len(quests)}\n"
                           f"• Database records removed: {len(quests)}\n\n"
                           f"{'**Note:** Some Discord messages could not be deleted (likely already removed)' if failed_deletes else 'All quest data has been completely removed.'}",
                color=0x00ff00
            )
            
            await confirmation_msg.edit(embed=embed)
            await confirmation_msg.clear_reactions()
            
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Confirmation Timeout",
                description="No response received within 30 seconds. Quest deletion cancelled.",
                color=0xff0000
            )
            await confirmation_msg.edit(embed=embed)
            await confirmation_msg.clear_reactions()
        
    except Exception as e:
        await ctx.send(f"❌ Error deleting all quests: {str(e)[:100]}", delete_after=10)

@bot.command(name='allquests')
async def all_quests(ctx):
    """List all current quests by name with clickable links"""
    try:
        if not quest_bot.db_connection:
            await ctx.send("❌ Database not available", delete_after=10)
            return
        
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT message_id, channel_id, title FROM quests WHERE guild_id = ?', (ctx.guild.id,))
        quests = cursor.fetchall()
        
        if not quests:
            embed = discord.Embed(
                title="📋 Current Quests",
                description="No active quests found.\n\nAdmins can create new quests using `-addquest <title> <description>`",
                color=0x0099ff
            )
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title="📋 Current Active Quests",
            description=f"Found {len(quests)} active quest(s). Click the links below to jump to each quest:",
            color=0x0099ff
        )
        
        quest_links = []
        for message_id, channel_id, title in quests:
            # Create clickable link to the quest message
            quest_url = f"https://discord.com/channels/{ctx.guild.id}/{channel_id}/{message_id}"
            quest_links.append(f"🏆 [{title}]({quest_url})")
        
        # Split into chunks if too many quests
        for i in range(0, len(quest_links), 10):
            chunk = quest_links[i:i+10]
            field_name = f"Quests {i+1}-{min(i+10, len(quest_links))}" if len(quest_links) > 10 else "Active Quests"
            embed.add_field(
                name=field_name,
                value="\n".join(chunk),
                inline=False
            )
        
        embed.set_footer(text="💡 Click quest titles to jump directly to them • React with ✅ to complete")
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error fetching quests: {str(e)[:100]}", delete_after=10)

@bot.command(name='assignroleXP')
@commands.has_permissions(kick_members=True)
async def assign_role_xp(ctx, xp_amount: int, role: discord.Role, role_type: str = "badge"):
    """Assign XP value to role (staff only)"""
    try:
        if role_type.lower() not in ["badge", "streak"]:
            embed = discord.Embed(
                title="❌ Invalid Role Type",
                description="Role type must be either `badge` or `streak`\n\n"
                           "**Examples:**\n"
                           "• `-assignroleXP @BadgeRole 5 badge`\n"
                           "• `-assignroleXP @StreakRole 10 streak`",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Assign XP to role
        quest_bot.assign_role_xp(ctx.guild.id, str(role.id), xp_amount, role_type.lower())
        quest_bot.save_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="✅ Role XP Assigned",
            description=f"**Role:** {role.mention} ({role.name})\n"
                       f"**XP Amount:** {xp_amount:,} XP\n"
                       f"**Type:** {role_type.title()}\n\n"
                       f"{'Users will earn this XP each time they gain this role (accumulates)' if role_type.lower() == 'streak' else 'Users with this role will have this XP added to their total'}",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=15)
        
    except Exception as e:
        await ctx.send(f"❌ Error assigning role XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='assignstreakXP')
@commands.has_permissions(kick_members=True)
async def assign_streak_xp_multi(ctx, xp_amount: int, *roles: discord.Role):
    """Assign streak XP to multiple roles at once (staff only)"""
    try:
        if not roles:
            embed = discord.Embed(
                title="❌ No Roles Specified",
                description="Please mention one or more roles to assign streak XP to.\n\n"
                           "**Example:** `-assignstreakXP 10 @Role1 @Role2 @Role3`",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        if xp_amount <= 0:
            embed = discord.Embed(
                title="❌ Invalid XP Amount", 
                description="XP amount must be a positive number.",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=10)
            return
        
        # Assign streak XP to all specified roles
        assigned_roles = []
        for role in roles:
            quest_bot.assign_role_xp(ctx.guild.id, str(role.id), xp_amount, "streak")
            assigned_roles.append(role.mention)
        
        quest_bot.save_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="✅ Streak XP Assigned",
            description=f"**XP Amount:** {xp_amount:,} Streak XP\n"
                       f"**Assigned to {len(roles)} role(s):**\n{', '.join(assigned_roles)}\n\n"
                       f"**Behavior:** Users will earn {xp_amount:,} XP each time they gain any of these streak roles (accumulates over time)",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=20)
        
    except Exception as e:
        await ctx.send(f"❌ Error assigning streak XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='assignbadgeXP')
@commands.has_permissions(kick_members=True) 
async def assign_badge_xp_multi(ctx, xp_amount: int, *roles: discord.Role):
    """Assign badge XP to multiple roles at once (staff only)"""
    try:
        if not roles:
            embed = discord.Embed(
                title="❌ No Roles Specified",
                description="Please mention one or more roles to assign badge XP to.\n\n"
                           "**Example:** `-assignbadgeXP 5 @Role1 @Role2 @Role3`",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        if xp_amount <= 0:
            embed = discord.Embed(
                title="❌ Invalid XP Amount",
                description="XP amount must be a positive number.",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=10)
            return
        
        # Assign badge XP to all specified roles
        assigned_roles = []
        for role in roles:
            quest_bot.assign_role_xp(ctx.guild.id, str(role.id), xp_amount, "badge")
            assigned_roles.append(role.mention)
        
        quest_bot.save_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="✅ Badge XP Assigned",
            description=f"**XP Amount:** {xp_amount:,} Badge XP\n"
                       f"**Assigned to {len(roles)} role(s):**\n{', '.join(assigned_roles)}\n\n"
                       f"**Behavior:** Users with any of these badge roles will have {xp_amount:,} XP added to their total (does not duplicate)",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=20)
        
    except Exception as e:
        await ctx.send(f"❌ Error assigning badge XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='unassignroleXP')
@commands.has_permissions(kick_members=True)
async def unassign_role_xp(ctx, *roles: discord.Role):
    """Remove XP assignments from multiple roles (staff only)"""
    try:
        if not roles:
            embed = discord.Embed(
                title="❌ No Roles Specified",
                description="Please mention one or more roles to remove XP assignments from.\n\n"
                           "**Example:** `-unassignroleXP @Role1 @Role2 @Role3`",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Remove XP assignments from all specified roles
        removed_roles = []
        for role in roles:
            quest_bot.unassign_role_xp(ctx.guild.id, str(role.id))
            removed_roles.append(role.mention)
        
        quest_bot.save_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="✅ Role XP Assignments Removed",
            description=f"**Removed XP assignments from {len(roles)} role(s):**\n{', '.join(removed_roles)}\n\n"
                       f"**Result:** These roles no longer provide any custom XP bonuses.",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=15)
        
    except Exception as e:
        await ctx.send(f"❌ Error removing role XP assignments: {str(e)[:100]}", delete_after=10)

@bot.command(name='checkroleXP')
@commands.has_permissions(kick_members=True)
async def check_role_xp(ctx, *, role_input: str):
    """Display XP amount assigned to a role"""
    try:
        # Parse role from string input (name, ID, or mention)
        role = None
        
        # Try to parse as a role mention first
        if role_input.startswith('<@&') and role_input.endswith('>'):
            # Extract role ID from mention <@&123456789>
            role_id = int(role_input[3:-1])
            role = ctx.guild.get_role(role_id)
        else:
            # Try to parse as raw role ID
            try:
                role_id = int(role_input)
                role = ctx.guild.get_role(role_id)
            except ValueError:
                # Try to find role by name if not a valid ID
                role = discord.utils.get(ctx.guild.roles, name=role_input)
        
        if not role:
            embed = discord.Embed(
                title="❌ Role Not Found",
                description=f"Could not find role: `{role_input}`\n\n"
                           f"**Usage:** `-checkroleXP <role_name_or_id>`\n"
                           f"**Examples:**\n"
                           f"• `-checkroleXP Helper`\n"
                           f"• `-checkroleXP 123456789`\n\n"
                           f"💡 **Tip:** Use role name or ID to avoid pinging everyone!",
                color=0xff0000
            )
            await ctx.send(embed=embed, delete_after=15)
            return
        role_xp_data = quest_bot.get_role_xp_and_type(ctx.guild.id, str(role.id))
        
        if not role_xp_data:
            embed = discord.Embed(
                title="📊 Role XP Information",
                description=f"**Role:** @{role.name}\n"
                           f"**XP Assignment:** No XP assigned\n\n"
                           f"This role currently has no custom XP assignment.",
                color=0x0099ff
            )
            await ctx.send(embed=embed)
            return
        
        xp_amount, role_type = role_xp_data
        
        # Different descriptions based on role type
        if role_type == "streak":
            behavior = f"Users earn {xp_amount:,} Streak XP each time they gain this role (accumulates over time)"
        elif role_type == "badge":
            behavior = f"Users with this role have {xp_amount:,} Badge XP added to their total (does not duplicate)"
        else:
            behavior = f"Users with this role have {xp_amount:,} XP added to their total"
        
        embed = discord.Embed(
            title="📊 Role XP Information",
            description=f"**Role:** @{role.name}\n"
                       f"**XP Amount:** {xp_amount:,} {role_type.title()} XP\n"
                       f"**Type:** {role_type.title()}\n\n"
                       f"**Behavior:** {behavior}",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error checking role XP: {str(e)[:100]}", delete_after=10)

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
    elif isinstance(error, commands.MissingRole):
        await ctx.send("❌ You need the @staff role to use this command!", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument provided!", delete_after=5)
    else:
        await ctx.send("❌ An error occurred while processing the command!", delete_after=5)

if __name__ == "__main__":
    import os
    
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN environment variable not set!")
        print("Please set your Discord bot token as an environment variable.")
        exit(1)

    webserver.keep_alive()
    
    # Run the bot
    bot.run(TOKEN)
