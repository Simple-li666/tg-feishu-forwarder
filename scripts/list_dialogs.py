import os

from telethon import utils
from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session = os.environ["TG_STRING_SESSION"]

    with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for dialog in client.iter_dialogs():
            entity = dialog.entity
            peer_id = utils.get_peer_id(entity)
            title = getattr(entity, "title", None) or dialog.name or ""
            username = getattr(entity, "username", None) or ""
            print(f"{peer_id}\t{title}\t@{username}" if username else f"{peer_id}\t{title}")


if __name__ == "__main__":
    main()
