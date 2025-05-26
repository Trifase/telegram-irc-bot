import asyncio
import logging
from typing import Optional
import config
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes



class IRCClient:
    def __init__(self, server: str, port: int, nick: str, channel: str, bridge):
        self.server = server
        self.port = port
        self.nick = nick
        self.channel = channel
        self.bridge = bridge
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.joined = False
    
    async def connect(self):
        """Connect to IRC server"""
        try:
            self.reader, self.writer = await asyncio.open_connection(self.server, self.port)
            self.connected = True
            print(f"Connected to IRC server: {self.server}:{self.port}")

            # Send registration
            await self.send_raw(f"NICK {self.nick}")
            await self.send_raw(f"USER {self.nick} 0 * :{self.nick}")
            
            # Start message handling
            asyncio.create_task(self.handle_messages())
            
        except Exception as e:
            print(f"Failed to connect to IRC: {e}")
            self.connected = False
    
    async def send_raw(self, message: str):
        """Send raw IRC message"""
        if self.writer and not self.writer.is_closing():
            self.writer.write(f"{message}\r\n".encode())
            await self.writer.drain()
            print(f"IRC >> {message}")
    
    async def join_channel(self):
        """Join the specified channel"""
        if self.connected and not self.joined:
            await self.send_raw(f"JOIN {self.channel}")
    
    async def send_privmsg(self, target: str, message: str):
        """Send PRIVMSG to channel or user"""
        await self.send_raw(f"PRIVMSG {target} :{message}")
    
    async def handle_messages(self):
        """Handle incoming IRC messages"""
        try:
            while self.connected and self.reader:
                data = await self.reader.readline()
                if not data:
                    break
                
                message = data.decode('utf-8', errors='ignore').strip()
                if not message:
                    continue
                
                print(f"IRC << {message}")
                await self.process_message(message)
                
        except Exception as e:
            print(f"Error handling IRC messages: {e}")
        finally:
            self.connected = False
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
    
    async def process_message(self, message: str):
        """Process IRC message"""
        parts = message.split(' ', 3)
        
        if message.startswith('PING'):
            # Respond to PING
            pong_msg = message.replace('PING', 'PONG')
            await self.send_raw(pong_msg)
            return
        
        if len(parts) >= 2:
            # Handle numeric responses
            if parts[1] == '001':  # Welcome message
                print("IRC registration successful")
                await self.join_channel()
            elif parts[1] == '366':  # End of NAMES list (joined channel)
                self.joined = True
                print(f"Successfully joined {self.channel}")
            elif parts[1] == 'PRIVMSG' and len(parts) >= 4:
                # Parse PRIVMSG
                source = parts[0][1:]  # Remove leading ':'
                target = parts[2]
                msg_content = parts[3][1:]  # Remove leading ':'
                
                # Extract nickname from source
                nick = source.split('!')[0] if '!' in source else source
                
                # Only relay messages from our channel that aren't from us
                if target == self.channel and nick != self.nick:
                    formatted_msg = f"<{nick}> {msg_content}"
                    await self.bridge.send_to_telegram(formatted_msg)
    
    async def disconnect(self):
        """Disconnect from IRC"""
        self.connected = False
        if self.writer:
            await self.send_raw("QUIT :Bridge bot disconnecting")
            self.writer.close()
            await self.writer.wait_closed()


class TelegramIRCBridge:
    def __init__(self, telegram_token: str, irc_server: str, irc_port: int, 
                 irc_nick: str, irc_channel: str, telegram_chat_id: int):
        self.telegram_token = telegram_token
        self.irc_server = irc_server
        self.irc_port = irc_port
        self.irc_nick = irc_nick
        self.irc_channel = irc_channel
        self.telegram_chat_id = telegram_chat_id
        
        # Initialize Telegram bot
        self.telegram_app = Application.builder().token(telegram_token).build()
        self.irc_client: Optional[IRCClient] = None
        
        # Setup Telegram handlers
        self.telegram_app.add_handler(CommandHandler("start", self.start_command))
        self.telegram_app.add_handler(CommandHandler("status", self.status_command))
        self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_telegram_message))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        await update.message.reply_text(
            f"Telegram-IRC Bridge Bot is running!\n"
            f"Bridging with: {self.irc_channel} on {self.irc_server}"
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if self.irc_client:
            status = "Connected" if self.irc_client.connected else "Disconnected"
            joined = "Yes" if self.irc_client.joined else "No"
            await update.message.reply_text(
                f"IRC Status: {status}\n"
                f"Channel Joined: {joined}\n"
                f"Server: {self.irc_server}:{self.irc_port}\n"
                f"Channel: {self.irc_channel}"
            )
        else:
            await update.message.reply_text("IRC client not initialized")
    
    async def handle_telegram_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages from Telegram and relay to IRC"""
        # Only process messages from the configured chat
        if update.effective_chat.id != self.telegram_chat_id:
            return
        
        if self.irc_client and self.irc_client.connected and self.irc_client.joined:
            message = update.message.text
            sender = update.effective_user.first_name or update.effective_user.username or "Unknown"
            
            # Format message for IRC
            irc_message = f"{message}"
            await self.irc_client.send_privmsg(self.irc_channel, irc_message)
            print(f"Relayed to IRC: {irc_message}")
        else:
            print("IRC not connected, cannot relay message")
    
    async def send_to_telegram(self, message: str):
        """Send message to Telegram group"""
        try:
            await self.telegram_app.bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message
            )
            print(f"Relayed to Telegram: {message}")
        except Exception as e:
            print(f"Failed to send message to Telegram: {e}")
    
    async def start_irc_client(self):
        """Start the IRC client"""
        self.irc_client = IRCClient(
            self.irc_server, self.irc_port, self.irc_nick, self.irc_channel, self
        )
        await self.irc_client.connect()
        
        # Keep IRC connection alive
        while self.irc_client.connected:
            await asyncio.sleep(1)
    
    async def run(self):
        """Run both bots concurrently"""
        # Start IRC client in a separate task
        irc_task = asyncio.create_task(self.start_irc_client())
        
        # Start Telegram bot
        print("Starting Telegram bot...")
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        await self.telegram_app.updater.start_polling()
        
        try:
            # Wait for IRC task or until interrupted
            await irc_task
        except KeyboardInterrupt:
            print("Shutting down bots...")
        finally:
            if self.irc_client:
                await self.irc_client.disconnect()
            await self.telegram_app.stop()


async def main():
    # Configuration
    TELEGRAM_TOKEN = config.TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID

    IRC_SERVER = config.IRC_SERVER
    IRC_PORT = config.IRC_PORT
    IRC_NICK = config.IRC_NICK
    IRC_CHANNEL = config.IRC_CHANNEL
    
    # Create and run the bridge
    bridge = TelegramIRCBridge(
        telegram_token=TELEGRAM_TOKEN,
        irc_server=IRC_SERVER,
        irc_port=IRC_PORT,
        irc_nick=IRC_NICK,
        irc_channel=IRC_CHANNEL,
        telegram_chat_id=TELEGRAM_CHAT_ID
    )
    
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user")

