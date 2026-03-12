def handle_telegram_messages():
    messages = bash_command("cat /repo/telegram_messages.txt")
    for message in messages:
        send_telegram_message(message)