import httpx

# Official WhatsApp Business Cloud API only - there's no free, simple bot API
# for WhatsApp like Telegram's. This requires the user to have already set up
# a Meta Developer app + verified WhatsApp Business Account and obtained a
# phone_number_id + access token outside LightNVR. Unofficial automation
# (e.g. driving a real WhatsApp Web session) is against WhatsApp's Terms of
# Service and risks the user's account being banned, so it's intentionally
# not implemented here.
GRAPH_API_VERSION = "v20.0"


async def send_whatsapp_message(phone_number_id: str, access_token: str, recipient: str, text: str) -> tuple[bool, str]:
    if not phone_number_id or not access_token or not recipient:
        return False, "Phone number ID, access token, and recipient number are required"

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload, headers=headers)
        data = response.json()
        if response.status_code == 200:
            return True, "Sent"
        error = data.get("error", {}).get("message", f"HTTP {response.status_code}")
        return False, error
    except httpx.HTTPError as exc:
        return False, str(exc)
