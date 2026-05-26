import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = client.get_me()
        print(f"Logged in as: {getattr(me, 'username', None) or me.id}")
        print()
        print("Copy this value into the GitHub secret named TG_STRING_SESSION:")
        print(StringSession.save(client.session))


if __name__ == "__main__":
    main()
