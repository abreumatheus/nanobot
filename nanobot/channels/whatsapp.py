"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WhatsAppConfig

MAX_IMAGE_DIMENSION = 1024


def _process_whatsapp_image(image_data: dict[str, str]) -> str | None:
    """Decode a base64 image from the bridge, resize it with Pillow, and save to disk.

    Returns the local file path on success, or None on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow is not installed — cannot process WhatsApp images")
        return None

    try:
        raw = base64.b64decode(image_data["data"])

        img = Image.open(io.BytesIO(raw))

        if max(img.size) > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / max(img.size)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        media_dir = Path.home() / ".nanobot" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        ts = int(time.time() * 1000)
        short_hash = hashlib.md5(raw[:256]).hexdigest()[:8]
        file_path = media_dir / f"wa_{ts}_{short_hash}.jpg"

        img.save(str(file_path), format="JPEG", quality=85)
        logger.debug(f"Saved WhatsApp image ({img.size[0]}x{img.size[1]}) to {file_path}")
        return str(file_path)
    except Exception as e:
        logger.error(f"Failed to process WhatsApp image: {e}")
        return None


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.
    
    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """
    
    name = "whatsapp"
    
    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
    
    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets
        
        bridge_url = self.config.bridge_url
        
        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")
        
        self._running = True
        
        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")
                    
                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")
                
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
    
    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        
        if self._ws:
            await self._ws.close()
            self._ws = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        
        try:
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content
            }
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")
    
    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return
        
        msg_type = data.get("type")
        
        if msg_type == "message":
            pn = data.get("pn", "")
            sender = data.get("sender", "")
            content = data.get("content", "")

            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info(f"Sender {sender}")

            if content == "[Voice Message]":
                logger.info(f"Voice message from {sender_id} — transcription not yet supported")
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            media_paths: list[str] = []
            image_data = data.get("image")
            if image_data:
                image_path = _process_whatsapp_image(image_data)
                if image_path:
                    media_paths.append(image_path)
                    if not content:
                        content = "[image]"

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,
                content=content,
                media=media_paths or None,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                },
            )
        
        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")
            
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
        
        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        
        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")
