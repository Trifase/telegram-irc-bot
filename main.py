import asyncio
import base64
import os
import tempfile
from typing import Optional

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

# No logging configuration needed

class IRCClient:
    def __init__(self, server: str, port: int, nick: str, channel: str, bridge, nickserv_password: str = None):
        self.server = server
        self.port = port
        self.nick = nick
        self.channel = channel
        self.bridge = bridge
        self.nickserv_password = nickserv_password
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.joined = False
        self.channel_users = []  # Store current channel users
        self.names_pending = False  # Track if we're waiting for NAMES response
        self.channel_topic = ""  # Store current channel topic
        self.topic_pending = False  # Track if we're waiting for TOPIC response
    
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
            # print(f"IRC >> {message}")  # Uncomment for debug
    
    async def join_channel(self):
        """Join the specified channel"""
        if self.connected and not self.joined:
            await self.send_raw(f"JOIN {self.channel}")
    
    async def send_privmsg(self, target: str, message: str):
        """Send PRIVMSG to channel or user"""
        await self.send_raw(f"PRIVMSG {target} :{message}")
    
    async def request_names(self):
        """Request list of users in channel"""
        if self.connected and self.joined:
            self.names_pending = True
            self.channel_users = []  # Clear current list
            await self.send_raw(f"NAMES {self.channel}")
            return True
        return False
    
    async def request_topic(self):
        """Request current channel topic"""
        if self.connected and self.joined:
            self.topic_pending = True
            await self.send_raw(f"TOPIC {self.channel}")
            return True
        return False
    
    async def identify_nickserv(self):
        """Identify with NickServ using password"""
        if self.nickserv_password:
            await self.send_raw(f"PRIVMSG NickServ :IDENTIFY {self.nick} {self.nickserv_password}")
            print(f"Sent IDENTIFY command to NickServ for {self.nick}")
    
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
                
                # print(f"IRC << {message}")  # Uncomment for debug
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
        # print(f"Processing message: {message}")  # Debug output
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
                # Identify with NickServ if password is provided
                if self.nickserv_password:
                    await self.identify_nickserv()
                await self.join_channel()
            elif parts[1] == '332':  # Topic reply (RPL_TOPIC)
                # Format: :server 332 nick #channel :topic text
                if len(parts) >= 4:
                    topic_text = parts[3][1:]  # Remove leading ':'
                    self.channel_topic = topic_text
                    if self.topic_pending:
                        # This is a response to our TOPIC request
                        self.topic_pending = False
                        response = f"📌 Topic for {self.channel}:\n{topic_text}"
                        await self.bridge.send_to_telegram(response)
                    # print(f"Topic set: {topic_text}")  # Uncomment for debug
            elif parts[1] == '331':  # No topic set (RPL_NOTOPIC)
                # Format: :server 331 nick #channel :No topic is set
                self.channel_topic = ""
                if self.topic_pending:
                    self.topic_pending = False
                    response = f"📌 No topic is set for {self.channel}"
                    await self.bridge.send_to_telegram(response)
            elif parts[1] == '333':  # Topic info (RPL_TOPICWHOTIME)
                # Format: :server 333 nick #channel setter timestamp
                # This comes after 332, we can ignore it or use it for additional info
                pass
            elif parts[1] == '353':  # NAMES reply
                # Format: :server 353 nick = #channel :user1 user2 user3...
                if len(parts) >= 4:
                    users_str = parts[3][1:]  # Remove leading ':'
                    users = users_str.split()
                    # Keep all prefixes (@, +, %, etc.) to show user privileges
                    self.channel_users.extend(users)
                    # print(f"NAMES response: {users}")  # Uncomment for debug
            elif parts[1] == '366':  # End of NAMES list
                if self.names_pending:
                    # Send the complete user list to Telegram
                    self.names_pending = False
                    user_count = len(self.channel_users)
                    users_text = ', '.join(sorted(self.channel_users))
                    response = f"📋 Users in {self.channel} ({user_count}):\n{users_text}"
                    await self.bridge.send_to_telegram(response)
                else:
                    # This is from joining the channel
                    self.joined = True
                    print(f"Successfully joined {self.channel}")
                    # Request topic after joining
                    await self.request_topic()
            elif parts[1] == 'PRIVMSG' and len(parts) >= 4:
                # Parse PRIVMSG
                # print(parts)
                source = parts[0][1:]  # Remove leading ':'
                target = parts[2]
                msg_content = parts[3][1:]  # Remove leading ':'
                
                # Extract nickname from source
                nick = source.split('!')[0] if '!' in source else source
                # Only relay messages from our channel that aren't from us
                if target == self.channel and nick != self.nick:
                    formatted_msg = f"<{nick}> {msg_content}"
                    if 'ACTION ' in msg_content and '\x01' in msg_content:
                        action_text = msg_content.split(' ', 1)[-1][:-1]  # Remove trailing '\x01'
                        print(f"Action from {nick} in {target}: {action_text}")
                        formatted_msg = f"*{nick} {action_text}"
                    await self.bridge.send_to_telegram(formatted_msg)
            elif parts[1] == 'TOPIC':
                # Topic change
                # Format: :nick!user@host TOPIC #channel :new topic
                source = parts[0][1:]  # Remove leading ':'
                nick = source.split('!')[0] if '!' in source else source
                if len(parts) >= 4:
                    channel = parts[2]
                    new_topic = parts[3][1:]  # Remove leading ':'
                    if channel == self.channel:
                        self.channel_topic = new_topic
                        topic_msg = f"📌 {nick} changed topic to: {new_topic}"
                        await self.bridge.send_to_telegram(topic_msg)
                elif len(parts) >= 3:
                    # Topic removed (no topic text)
                    channel = parts[2]
                    if channel == self.channel:
                        self.channel_topic = ""
                        topic_msg = f"📌 {nick} removed the topic"
                        await self.bridge.send_to_telegram(topic_msg)
            elif parts[1] == 'JOIN':
                # Someone joined the channel
                source = parts[0][1:]  # Remove leading ':'
                nick = source.split('!')[0] if '!' in source else source
                if len(parts) >= 3:
                    channel = parts[2]
                    if channel == self.channel and nick != self.nick:
                        join_msg = f"[IRC] → {nick} joined {channel}"
                        await self.bridge.send_to_telegram(join_msg)
            elif parts[1] == 'PART':
                # Someone left the channel
                source = parts[0][1:]  # Remove leading ':'
                nick = source.split('!')[0] if '!' in source else source
                if len(parts) >= 3:
                    channel = parts[2]
                    if channel == self.channel and nick != self.nick:
                        part_msg = f"[IRC] ← {nick} left {channel}"
                        await self.bridge.send_to_telegram(part_msg)
            elif parts[1] == 'QUIT':
                # Someone quit IRC
                source = parts[0][1:]  # Remove leading ':'
                nick = source.split('!')[0] if '!' in source else source
                quit_reason = parts[2][1:] if len(parts) >= 3 else "No reason"
                quit_msg = f"[IRC] ← {nick} quit ({quit_reason})"
                await self.bridge.send_to_telegram(quit_msg)
    
    async def disconnect(self):
        """Disconnect from IRC"""
        self.connected = False
        if self.writer:
            await self.send_raw("QUIT :Ritorneremo")
            self.writer.close()
            await self.writer.wait_closed()


class TelegramIRCBridge:
    def __init__(self, telegram_token: str, irc_server: str, irc_port: int, 
                 irc_nick: str, irc_channel: str, telegram_chat_id: int, 
                 nickserv_password: str = None):
        self.telegram_token = telegram_token
        self.irc_server = irc_server
        self.irc_port = irc_port
        self.irc_nick = irc_nick
        self.irc_channel = irc_channel
        self.telegram_chat_id = telegram_chat_id
        self.nickserv_password = nickserv_password
        
        # Initialize Telegram bot
        self.telegram_app = Application.builder().token(telegram_token).build()
        self.irc_client: Optional[IRCClient] = None
        
        # Setup Telegram handlers
        self.telegram_app.add_handler(CommandHandler("start", self.start_command))
        self.telegram_app.add_handler(CommandHandler("status", self.status_command))
        self.telegram_app.add_handler(CommandHandler("names", self.names_command))
        self.telegram_app.add_handler(CommandHandler("list", self.names_command))  # Alias for names
        self.telegram_app.add_handler(CommandHandler("topic", self.topic_command))
        self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_telegram_message))
        self.telegram_app.add_handler(MessageHandler(filters.PHOTO, self.handle_telegram_photo))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        await update.message.reply_text(
            f"Telegram-IRC Bridge Bot is running!\n"
            f"Bridging with: {self.irc_channel} on {self.irc_server}\n\n"
            f"Available commands:\n"
            f"/status - Show connection status\n"
            f"/names or /list - Show channel users\n"
            f"/topic - Show current channel topic"
        )
    
    async def names_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /names or /list command"""
        if update.effective_chat.id != self.telegram_chat_id:
            await update.message.reply_text("This command only works in the bridged group.")
            return
        
        if self.irc_client and self.irc_client.connected and self.irc_client.joined:
            success = await self.irc_client.request_names()
            if success:
                await update.message.reply_text(f"🔄 Requesting user list for {self.irc_channel}...")
            else:
                await update.message.reply_text("❌ Failed to request user list.")
        else:
            await update.message.reply_text("❌ IRC not connected or not joined to channel.")
    
    async def topic_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /topic command"""
        if update.effective_chat.id != self.telegram_chat_id:
            await update.message.reply_text("This command only works in the bridged group.")
            return
        
        if self.irc_client and self.irc_client.connected and self.irc_client.joined:
            success = await self.irc_client.request_topic()
            if success:
                await update.message.reply_text(f"🔄 Requesting topic for {self.irc_channel}...")
            else:
                await update.message.reply_text("❌ Failed to request topic.")
        else:
            await update.message.reply_text("❌ IRC not connected or not joined to channel.")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if self.irc_client:
            status = "Connected" if self.irc_client.connected else "Disconnected"
            joined = "Yes" if self.irc_client.joined else "No"
            topic = self.irc_client.channel_topic if self.irc_client.channel_topic else "No topic set"
            await update.message.reply_text(
                f"IRC Status: {status}\n"
                f"Channel Joined: {joined}\n"
                f"Server: {self.irc_server}:{self.irc_port}\n"
                f"Channel: {self.irc_channel}\n"
                f"Topic: {topic}"
            )
        else:
            await update.message.reply_text("IRC client not initialized")
    
    async def handle_telegram_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photos from Telegram and relay to IRC"""

        # Only process messages from the configured chat
        if update.effective_chat.id != self.telegram_chat_id:
            return

        # If this is None, file upload is disabled
        IMGBB_API_KEY = getattr(config, 'IMGBB_API_KEY', None)

        
        if self.irc_client and self.irc_client.connected and self.irc_client.joined and IMGBB_API_KEY:

            with tempfile.NamedTemporaryFile(suffix=".jpg") as tempphoto:
                picture = update.message.photo[-1]
                actual_picture = await picture.get_file()
                await actual_picture.download_to_drive(custom_path=tempphoto.name)
                img_url = upload_image_to_imgbb(tempphoto.name, IMGBB_API_KEY)

            if img_url:
                await self.irc_client.send_privmsg(self.irc_channel, img_url)
                print(f"→ IRC: {img_url}")
        else:
            print("IRC not connected, cannot relay message")

    async def handle_telegram_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages from Telegram and relay to IRC"""
        # Only process messages from the configured chat
        if update.effective_chat.id != self.telegram_chat_id:
            return
        
        if self.irc_client and self.irc_client.connected and self.irc_client.joined:
            message = update.message.text

            # sender = update.effective_user.first_name or update.effective_user.username or "Unknown"
            
            # Format message for IRC
            irc_message = f"{message}"
            if update.message and update.message.reply_to_message  and update.message.reply_to_message.text:
                # If this is a reply, include the original message
               irc_message = f"{update.message.reply_to_message.text} → {irc_message}"
            await self.irc_client.send_privmsg(self.irc_channel, irc_message)
            print(f"→ IRC: {irc_message}")
        else:
            print("IRC not connected, cannot relay message")
    
    async def send_to_telegram(self, message: str):
        """Send message to Telegram group"""
        try:
            await self.telegram_app.bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message
            )
            print(f"← Telegram: {message}")
        except Exception as e:
            print(f"Failed to send message to Telegram: {e}")
    
    async def start_irc_client(self):
        """Start the IRC client"""
        self.irc_client = IRCClient(
            self.irc_server, self.irc_port, self.irc_nick, self.irc_channel, 
            self, self.nickserv_password
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


def upload_image_to_imgbb(image_path, api_key):
    """
    Uploads an image to ImgBB and returns its public URL.

    Args:
        image_path (str): The path to the image file you want to upload.
        api_key (str): Your ImgBB API key.

    Returns:
        str: The public URL of the uploaded image, or None if the upload fails.
    """
    if not os.path.exists(image_path):
        print(f"Error: Image file not found at '{image_path}'")
        return None

    # ImgBB API endpoint for uploads
    upload_url = "https://api.imgbb.com/1/upload"

    try:
        # Read the image file in binary mode
        with open(image_path, "rb") as f:
            image_data = f.read()

        # Encode the image data to base64
        # ImgBB API accepts base64 encoded image data or a direct file upload
        # Using base64 is often more straightforward for simple scripts
        encoded_image = base64.b64encode(image_data).decode('utf-8')

        # Prepare the payload for the POST request
        # 'key' is your API key
        # 'image' is the base64 encoded image data
        payload = {
            "key": api_key,
            "image": encoded_image
        }

        # Send the POST request to ImgBB
        print(f"Uploading '{image_path}' to ImgBB...")
        response = requests.post(upload_url, data=payload)
        response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)

        # Parse the JSON response
        result = response.json()

        # Check if the upload was successful and get the URL
        if result.get("success"):
            public_url = result["data"]["url"]
            print(f"Image uploaded successfully! Public URL: {public_url}")
            return public_url
        else:
            error_message = result.get("error", "Unknown error during upload.")
            print(f"Image upload failed: {error_message}")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Network or API error: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None

async def main():
    # Configuration from config module
    TELEGRAM_TOKEN = config.TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID
    
    IRC_SERVER = config.IRC_SERVER
    IRC_PORT = config.IRC_PORT
    IRC_NICK = config.IRC_NICK
    IRC_CHANNEL = config.IRC_CHANNEL
    
    # Optional NickServ password (can be None if not needed)
    NICKSERV_PASSWORD = getattr(config, 'NICKSERV_PASSWORD', None)
    
    # Create and run the bridge
    bridge = TelegramIRCBridge(
        telegram_token=TELEGRAM_TOKEN,
        irc_server=IRC_SERVER,
        irc_port=IRC_PORT,
        irc_nick=IRC_NICK,
        irc_channel=IRC_CHANNEL,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        nickserv_password=NICKSERV_PASSWORD
    )
    
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user")