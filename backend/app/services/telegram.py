import httpx


async def send_telegram_message(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Bot token and chat ID are required"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": text})
        data = response.json()
        if response.status_code == 200 and data.get("ok"):
            return True, "Sent"
        return False, data.get("description", f"HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        return False, str(exc)


async def send_telegram_photo(bot_token: str, chat_id: str, photo_path: str, caption: str) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Bot token and chat ID are required"

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"photo": ("snapshot.jpg", f, "image/jpeg")},
                )
        data = response.json()
        if response.status_code == 200 and data.get("ok"):
            return True, "Sent"
        return False, data.get("description", f"HTTP {response.status_code}")
    except (httpx.HTTPError, OSError) as exc:
        return False, str(exc)
