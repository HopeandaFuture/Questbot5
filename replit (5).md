# Discord Quest Bot

## Overview
A comprehensive Discord bot that manages an XP-based quest system with automatic role tracking and leaderboards. The bot provides a gamified experience for Discord communities by allowing users to complete quests, earn experience points, and progress through a 10-level system. It features both prefix commands and modern slash commands for flexible user interaction.

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture

### Core Architecture
- **Framework**: Built on discord.py library for Discord API integration
- **Command System**: Hybrid approach supporting both traditional prefix commands (-) and modern slash commands (/)
- **Database**: SQLite for local data persistence with simple file-based storage
- **Web Server**: Flask integration for health monitoring and keep-alive functionality

### Bot Structure
- **Class-based Design**: QuestBot class encapsulates core functionality and database operations
- **Intent Configuration**: Carefully configured Discord intents for guild messages, reactions, and member data access
- **Privileged Intents**: Requires Message Content Intent and Members Intent to be enabled in Discord Developer Portal

### XP and Level System
- **10-Level Progression**: Exponential XP requirements from 0 to 11,700 XP
- **Multiple XP Sources**: Quest completion (50 XP), badge roles (5 XP), streak roles (5 XP)
- **Role-based Automation**: Automatic XP assignment when users receive specific roles
- **Manual Override**: Admin controls for XP management and adjustments

### Quest Management
- **Embed-based Quests**: Rich Discord embeds for quest presentation
- **Channel-specific**: Dedicated quest channel configuration
- **Role Notifications**: Configurable ping role for quest announcements
- **Lifecycle Management**: Create, remove, and bulk delete quest functionality

### Data Storage
- **SQLite Database**: Local file-based database for simplicity and reliability
- **User Tracking**: XP amounts, levels, and quest participation
- **Role Configuration**: Stored XP values for different role types
- **Channel Settings**: Persistent storage of quest channel and ping role configurations

### Permission System
- **Staff-only Commands**: XP manipulation and role assignment restricted to authorized users
- **Role-based Access**: Different command access levels based on Discord permissions
- **Safety Features**: Confirmation prompts for destructive operations like bulk deletions

## External Dependencies

### Discord Integration
- **discord.py**: Primary library for Discord bot functionality and API interactions
- **Discord Developer Portal**: Required for bot token, privileged intents configuration

### Web Services
- **Flask**: Lightweight web framework for health check endpoints
- **Port Configuration**: Environment-based port configuration for deployment flexibility

### Development Tools
- **python-dotenv**: Environment variable management for secure token handling
- **requests**: HTTP client library for potential external API integrations

### Runtime Environment
- **Threading**: Multi-threaded architecture for concurrent web server and bot operations
- **Environment Variables**: Secure configuration management for sensitive data like bot tokens
- **SQLite3**: Built-in Python database interface, no external database server required