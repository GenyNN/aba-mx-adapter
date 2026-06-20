import asyncio
import json
import logging
import os
from datetime import datetime
from typing import List, Optional

import aio_pika
import httpx
from pydantic import BaseModel, Field
#--mode mx-check --xls Klienty_301.xls
from max_playwright_sender import (
    MaxBrowserManager,
    logger,
    normalize_phone_for_max,
)

# --- Configuration ---
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
QUEUE_SEND = "tasks.messages.send"
QUEUE_POLL = "tasks.messages.poll_replies"

# --- Models ---
class SendTask(BaseModel):
    task_id: str
    campaign_id: str
    messenger: str
    phone: str
    message_text: str

class CallbackPayload(BaseModel):
    task_id: str
    status: str
    error_message: str = ""
    sent_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

class ReplyItem(BaseModel):
    phone: str
    reply_text: str
    replied_at: str

class BatchReplyPayload(BaseModel):
    campaign_id: str
    replies: List[ReplyItem]

class PollTask(BaseModel):
    campaign_id: str
    phones: List[str]

# --- Worker Logic ---

class MaxWorkerDaemon:
    def __init__(self):
        self.browser_manager = MaxBrowserManager(headless=False)
        self.http_client = httpx.AsyncClient(base_url=ORCHESTRATOR_URL, timeout=30.0)

    async def send_callback(self, payload: CallbackPayload):
        try:
            logger.info(f"Sending callback for task {payload.task_id}: {payload.status}")
            resp = await self.http_client.post("/api/v1/workers/callback", json=payload.model_dump())
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send callback to orchestrator: {e}")

    async def send_replies_webhook(self, payload: BatchReplyPayload):
        try:
            logger.info(f"Sending batch replies for campaign {payload.campaign_id}")
            resp = await self.http_client.post("/api/v1/workers/replies-webhook", json=payload.model_dump())
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send replies webhook: {e}")

    async def process_send_task(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                body = json.loads(message.body.decode())
                task = SendTask(**body)
                logger.info(f"Processing send task: {task.task_id} for {task.phone}")

                phone = normalize_phone_for_max(task.phone)
                if not phone:
                    await self.send_callback(CallbackPayload(
                        task_id=task.task_id,
                        status="failed",
                        error_message="Invalid phone format"
                    ))
                    return

                result = await self.browser_manager.send_message(phone, task.message_text)


                #await self.send_callback(CallbackPayload(
                #    task_id=task.task_id,
                #    status=result.status_note,
                #    error_message=result.error_message
                #))

            except Exception as e:
                logger.exception("Error in process_send_task")

    async def process_poll_task(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                body = json.loads(message.body.decode())
                task = PollTask(**body)
                logger.info(f"Processing poll task for campaign: {task.campaign_id}")

                replies = []
                for phone_raw in task.phones:
                    phone = normalize_phone_for_max(phone_raw)
                    if not phone: continue
                    
                    result = await self.browser_manager.check_reply(phone)
                    if result.check_ok and result.reply_value:
                        replies.append(ReplyItem(
                            phone=phone_raw,
                            reply_text=result.reply_value,
                            replied_at=result.replied_at or (datetime.utcnow().isoformat() + "Z")
                        ))
                
                if replies:
                    await self.send_replies_webhook(BatchReplyPayload(
                        campaign_id=task.campaign_id,
                        replies=replies
                    ))

            except Exception as e:
                logger.exception("Error in process_poll_task")

    async def run(self):
        logger.info("Starting Max Worker Daemon...")
        await self.browser_manager.start()
        
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=1)

                send_queue = await channel.declare_queue(QUEUE_SEND, durable=True)
                poll_queue = await channel.declare_queue(QUEUE_POLL, durable=True)

                logger.info(f"Waiting for messages on {QUEUE_SEND} and {QUEUE_POLL}...")
                
                # Consume from both queues
                await send_queue.consume(self.process_send_task)
                await poll_queue.consume(self.process_poll_task)

                # Keep running
                await asyncio.Future()
        finally:
            await self.browser_manager.stop()
            await self.http_client.aclose()

if __name__ == "__main__":
    daemon = MaxWorkerDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
