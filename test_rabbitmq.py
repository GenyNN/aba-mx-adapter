import pika
import json

# Sample message matching the provided structure
message = {
    "meta": {
        "event_id": "a1b2c3d4",
        "timestamp": "2025-12-25T12:00:00Z",
        "salon_id": "salon_top_cut_01"
    },
    "target": {
        "messenger": "telegram",
        "chat_id": "123456789",
        "phone": "+79991234567"
    },
    "content": {
        "type": "text",
        "text": "Hello, Ivan! You have a haircut appointment tomorrow at 3:00 PM. Do you want to confirm your appointment?",
        "buttons": [
            {"text": "Yes, I confirm", "callback_data": "confirm_123"},
            {"text": "No, I cancel", "callback_data": "cancel_123"}
        ]
    },
    "retry_count": 0
}

# Connect to RabbitMQ and publish the message
connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()
channel.queue_declare(queue='mx_tasks', durable=True)
channel.basic_publish(
    exchange='',
    routing_key='mx_tasks',
    body=json.dumps(message)
)
print("Test message published to mx_tasks queue")
connection.close()