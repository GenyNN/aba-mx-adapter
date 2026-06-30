import asyncio
import json
import logging
import os
import random
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
QUEUE_RESULTS = "tasks.messages.results_replies_queue"
QUEUE_NOTIFY = "tasks.messages.tenant_admin_notify"

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

class PollTarget(BaseModel):
    target_id: str
    phone_normalized: str

class PollTask(BaseModel):
    campaign_id: str
    targets: List[PollTarget]

class TargetResult(BaseModel):
    target_id: str
    campaign_id: str
    phone_number: str
    status: str
    reply_text: Optional[str] = None
    timestamp: str

class ClientReplyInfo(BaseModel):
    user_phone: str
    user_name: str
    message: str
    time: datetime

class TenantAdminNotificationTask(BaseModel):
    tenant_phone: str
    replies: List[ClientReplyInfo]

# --- Worker Logic ---

class MaxWorkerDaemon:
    def __init__(self):
        self.browser_manager = MaxBrowserManager(headless=False)
        self.http_client = httpx.AsyncClient(base_url=ORCHESTRATOR_URL, timeout=30.0)
        self.publish_channel: Optional[aio_pika.Channel] = None
        self.publish_connection: Optional[aio_pika.Connection] = None

    async def send_callback(self, payload: CallbackPayload):
        try:
            logger.info(f"Sending callback for task {payload.task_id}: {payload.status}")
            resp = await self.http_client.post("/api/v1/workers/callback", json=payload.model_dump())
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send callback to orchestrator: {e}")

    async def publish_result(self, result: TargetResult):
        try:
            if not self.publish_channel:
                return
            
            body = result.model_dump_json().encode()
            await self.publish_channel.default_exchange.publish(
                aio_pika.Message(body=body, content_type="application/json"),
                routing_key=QUEUE_RESULTS
            )
            logger.info(f"Published result for target {result.target_id}")
        except Exception as e:
            logger.error(f"Failed to publish result: {e}")

    def format_notification_message(self, task: TenantAdminNotificationTask) -> str:
        lines = ["🔔 New responses received:"]
        for i, reply in enumerate(task.replies, start=1):
            time_str = reply.time.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"\n{i}. 📱 {reply.user_phone} ({reply.user_name})")
            lines.append(f"   💬 Reply: {reply.message}")
            lines.append(f"   🕒 Time: {time_str}")
        return "\n".join(lines)

    async def process_notify_task(self, message: aio_pika.IncomingMessage):
        try:
            body = json.loads(message.body.decode())
            task = TenantAdminNotificationTask(**body)
            logger.info(f"Processing notify task for tenant: {task.tenant_phone} with {len(task.replies)} replies")

            phone = normalize_phone_for_max(task.tenant_phone)
            if not phone:
                logger.error(f"Invalid tenant phone format: {task.tenant_phone}")
                await message.ack()
                return

            # Build message
            notification_text = self.format_notification_message(task)
            logger.info(f"Notification text:\n{notification_text}")

            # Send message
            result = await self.browser_manager.send_message(phone, notification_text)
            if result.status_note == "success":
                logger.info(f"Notification sent successfully to {task.tenant_phone}")
            else:
                logger.error(f"Failed to send notification: {result.error_message}")

            await message.ack()
        except Exception as e:
            logger.exception("Error in process_notify_task")
            await message.nack(requeue=True)  # Requeue on failure

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
                logger.info(f"Processing poll task for campaign: {task.campaign_id} with {len(task.targets)} targets")

                for i, target in enumerate(task.targets):
                    # Add jitter delay (1.1 - 3.2 seconds)
                    delay = random.uniform(1.1, 3.2)
                    if i > 0:  # Don't delay first target
                        logger.info(f"Waiting {delay:.2f}s before checking next target")
                        await asyncio.sleep(delay)

                    phone = normalize_phone_for_max(target.phone_normalized)
                    if not phone:
                        logger.warning(f"Invalid phone for target {target.target_id}, skipping")
                        continue

                    result = await self.browser_manager.check_reply(phone)

                    if result.check_ok and result.reply_value:
                        # Has a reply
                        target_result = TargetResult(
                            target_id=target.target_id,
                            campaign_id=task.campaign_id,
                            phone_number=target.phone_normalized,
                            status="replied",
                            reply_text=result.reply_value,
                            timestamp=result.replied_at or (datetime.utcnow().isoformat() + "Z")
                        )
                    else:
                        # No reply yet
                        target_result = TargetResult(
                            target_id=target.target_id,
                            campaign_id=task.campaign_id,
                            phone_number=target.phone_normalized,
                            status="delivered",
                            timestamp=datetime.utcnow().isoformat() + "Z"
                        )

                    await self.publish_result(target_result)

            except Exception as e:
                logger.exception("Error in process_poll_task")

    async def run(self):
        logger.info("Starting Max Worker Daemon...")
        await self.browser_manager.start()
        
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            self.publish_connection = connection
            async with connection:
                channel = await connection.channel()
                self.publish_channel = channel
                await channel.set_qos(prefetch_count=1)

                send_queue = await channel.declare_queue(QUEUE_SEND, durable=True)
                poll_queue = await channel.declare_queue(QUEUE_POLL, durable=True)
                results_queue = await channel.declare_queue(QUEUE_RESULTS, durable=True)
                notify_queue = await channel.declare_queue(QUEUE_NOTIFY, durable=True)

                logger.info(f"Waiting for messages on {QUEUE_SEND}, {QUEUE_POLL}, {QUEUE_NOTIFY}...")
                
                # Consume from all queues
                await send_queue.consume(self.process_send_task)
                await poll_queue.consume(self.process_poll_task)
                await notify_queue.consume(self.process_notify_task)

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
