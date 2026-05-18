import argparse
import asyncio
import json
import logging
import threading

import pika
from fastapi import FastAPI

from max_playwright_sender import (
    DEFAULT_MESSAGE_TEMPLATE,
    DEFAULT_SESSION_FILE,
    send_messages_from_xls,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#using fast api
app = FastAPI()

# RabbitMQ consumer function
def consume_rabbitmq():
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters('127.0.0.1'))
        channel = connection.channel()
        channel.queue_declare(queue='mx_tasks', durable=True)
        
        def callback(ch, method, properties, body):
            try:
                message = json.loads(body)
                logger.info("Received message from RabbitMQ:")
                logger.info(json.dumps(message, indent=2))
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON: {e}, body: {body}")
            except Exception as e:
                logger.error(f"Unexpected error in callback: {e}")
        
        channel.basic_consume(queue='mx_tasks', on_message_callback=callback, auto_ack=True)
        logger.info(' [*] Waiting for messages. To exit press CTRL+C')
        channel.start_consuming()
    except pika.exceptions.AMQPConnectionError as e:
        logger.error(f"Failed to connect to RabbitMQ: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in consumer: {e}")
    finally:
        if 'connection' in locals() and connection.is_open:
            connection.close()

def start_rabbit_consumer_thread() -> None:
    rabbit_thread = threading.Thread(target=consume_rabbitmq, daemon=True)
    rabbit_thread.start()



@app.get("/")
def read_root():
    return {"Hello": "World11_11"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run API/Rabbit or MAX sender mode")
    parser.add_argument(
        "--mode",
        choices=["api", "mx-send"],
        default="api",
        help="api: FastAPI + RabbitMQ consumer, mx-send: XLS рассылка в MAX",
    )
    parser.add_argument("--xls", default="Klienty_301.xls", help="Путь к .xls для mx-send")
    parser.add_argument(
        "--template",
        default=DEFAULT_MESSAGE_TEMPLATE,
        help="Шаблон сообщения для mx-send, используйте {name}",
    )
    parser.add_argument("--delay", type=float, default=3.0, help="Пауза между отправками (сек)")
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="Файл Playwright session (storage state)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Браузер без окна (по умолчанию true)",
    )
    parser.add_argument("--attachment", default=None, help="Опциональный файл вложения")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.mode == "mx-send":
        asyncio.run(
            send_messages_from_xls(
                args.xls,
                message_template=args.template,
                headless=args.headless,
                delay_seconds=args.delay,
                session_file=args.session,
                attachment_path=args.attachment,
            )
        )
        raise SystemExit(0)

    start_rabbit_consumer_thread()
    import uvicorn

    # Передаем "main:app" строкой, чтобы работал reload
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)