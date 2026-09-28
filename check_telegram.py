"""
Telegram credentials & channel-access check — read-only diagnostic.

Answers three questions before you run the bot:
  1. Are the api_id / api_hash in config.json still valid (not revoked/flagged)?
  2. Is there a working saved session, and which Telegram account does it belong to?
  3. Can that account actually READ every channel listed in config.json?

This script never sends a login code and never changes anything.

Run on the VPS (from the bot folder):
    venv\\Scripts\\python check_telegram.py
"""

import asyncio
import json
import os
import sys

CONFIG = "config.json"


def load_config():
    if not os.path.exists(CONFIG):
        print(f"❌ {CONFIG} not found. Run this from the bot folder.")
        sys.exit(1)
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


async def main():
    try:
        from telethon import TelegramClient, errors
    except ImportError:
        print("❌ telethon is not installed. Run install.bat first.")
        sys.exit(1)

    cfg = load_config()
    tg = cfg.get("telegram", {})
    api_id = tg.get("api_id")
    api_hash = tg.get("api_hash")
    phone = tg.get("phone")
    session_name = tg.get("session_name", "trader_session")
    channels = cfg.get("channels", [])

    print("=" * 60)
    print("Telegram credential check")
    print("=" * 60)
    print(f"api_id       : {api_id}")
    print(f"api_hash     : {'set (' + str(len(api_hash)) + ' chars)' if api_hash else '❌ MISSING'}")
    print(f"phone        : {phone}")
    print(f"session_name : {session_name}")

    sess_file = f"{session_name}.session"
    if os.path.exists(sess_file):
        size = os.path.getsize(sess_file)
        print(f"session file : ✅ {sess_file} ({size} bytes)")
    else:
        print(f"session file : ⚪ {sess_file} does not exist yet "
              f"(the first run will ask for a Telegram code)")

    if not api_id or not api_hash:
        print("\n❌ api_id / api_hash are missing in config.json — fill them in first.")
        sys.exit(1)

    client = TelegramClient(session_name, api_id, api_hash)

    # ---- 1) do the credentials work at all? ----
    try:
        await client.connect()
    except errors.ApiIdInvalidError:
        print("\n❌ API_ID_INVALID")
        print("   The api_id/api_hash pair is wrong, deleted, or revoked by Telegram.")
        print("   → Create a new app at https://my.telegram.org/apps and update config.json.")
        return
    except errors.ApiIdPublishedFloodError:
        print("\n❌ API_ID_PUBLISHED_FLOOD")
        print("   Telegram flagged this api_id (usually because it was shared/published).")
        print("   → Create a NEW app at https://my.telegram.org/apps; do not share the new one.")
        return
    except Exception as e:
        print(f"\n❌ Could not reach Telegram: {type(e).__name__}: {e}")
        print("   Check the VPS internet connection / firewall.")
        return

    print("\n✅ api_id / api_hash accepted by Telegram (not expired, not revoked)")

    # ---- 2) is the session authorized, and for which account? ----
    try:
        authorized = await client.is_user_authorized()
    except Exception as e:
        print(f"⚠️ Could not read the session: {type(e).__name__}: {e}")
        authorized = False

    if not authorized:
        print("\n⚪ No active login session.")
        print("   → Run start.bat and enter the Telegram code once; it will be saved.")
        await client.disconnect()
        return

    me = await client.get_me()
    print("\n✅ Logged in as:")
    print(f"   name     : {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '') or ''}".rstrip())
    print(f"   username : @{me.username}" if getattr(me, "username", None) else "   username : (none)")
    print(f"   phone    : {getattr(me, 'phone', None) or '(hidden)'}")
    print(f"   user id  : {me.id}")

    cfg_phone = (phone or "").replace(" ", "")
    acc_phone = "+" + str(getattr(me, "phone", "") or "")
    if getattr(me, "phone", None) and cfg_phone and acc_phone != cfg_phone:
        print(f"\n⚠️ The session belongs to {acc_phone} but config.json says {cfg_phone}.")
        print("   That is fine as long as this account is a member of your channels.")

    # ---- 3) can this account read the configured channels? ----
    print(f"\nChecking access to {len(channels)} channels...")
    ok, failed = 0, []
    for ch in channels:
        cid = ch.get("id")
        try:
            entity = await client.get_entity(cid)
            title = getattr(entity, "title", None) or getattr(entity, "username", cid)
            print(f"   ✅ {cid}  →  {title}")
            ok += 1
        except Exception as e:
            print(f"   ❌ {cid}  →  {type(e).__name__}")
            failed.append(cid)

    print("\n" + "=" * 60)
    print(f"RESULT: {ok}/{len(channels)} channels reachable")
    if failed:
        print("These channels could NOT be read:")
        for cid in failed:
            print(f"   - {cid}")
        print("\nFixes:")
        print("   • Join each channel with THIS Telegram account (the one logged in above)")
        print("   • Check the @username is spelled exactly as in the channel")
        print("   • If the channel is private, the account must already be a member")
    else:
        print("Everything is ready — the bot can read all of your channels.")
    print("=" * 60)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
