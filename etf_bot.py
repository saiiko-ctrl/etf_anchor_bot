# etf_bot.py — Alerts-only (Render-ready)
import os, json, asyncio, datetime as dt
import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
from aiohttp import web

# ====== Config from environment ======
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ALERT_INTERVAL_SEC = int(os.getenv("ALERT_INTERVAL_SEC", "300"))  # default 5 min

# Behavior
SKIP_IF_NO_SHARES = True
PIP50, PIP75, PIP100 = 0.50, 0.75, 1.00

DATA_FILE = "positions.json"

# ====== Persistence ======
def load_positions():
    try:
        if not os.path.exists(DATA_FILE):
            return {}
        txt = open(DATA_FILE, "r", encoding="utf-8").read().strip()
        return json.loads(txt) if txt else {}
    except Exception as e:
        print(f"[WARN] positions.json parse failed: {e}")
        return {}

def save_positions(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

positions = load_positions()
alert_fired = set()   # (YYYY-MM-DD, TICKER, "50"|"75"|"100")

# ====== Discord bot ======
intents = discord.Intents.default()
intents.message_content = True  # enable in Dev Portal
bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)

# ====== Prices ======
async def fetch_prices_batch(tickers):
    out = {t: None for t in tickers}
    if not tickers:
        return out
    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval="1m",
            progress=False,
            threads=True,
            group_by="ticker",
        )
        if isinstance(data.columns, pd.MultiIndex):
            for t in tickers:
                try:
                    s = data[(t, "Close")].dropna()
                    if not s.empty:
                        out[t] = float(s.iloc[-1])
                except Exception:
                    pass
        else:
            try:
                s = data["Close"].dropna()
                if not s.empty:
                    out[tickers[0]] = float(s.iloc[-1])
            except Exception:
                pass
    except Exception:
        # Fallback per-ticker
        for t in tickers:
            try:
                s = yf.Ticker(t).history(period="1d", interval="1m")["Close"].dropna()
                if not s.empty:
                    out[t] = float(s.iloc[-1])
            except Exception:
                out[t] = None
    return out

def income_floor_for_price(price):
    if price is None: return None
    if price < 5:   return None
    if price <= 11: return 0.50
    if price <= 17: return 0.75
    if price <= 22: return 1.00
    if price <= 27: return 1.50
    if price <= 32: return 2.50
    if price <= 36: return 3.00
    return "No-buys > $36"

def calc_triggers(adj, price):
    pip50 = adj - PIP50; pip75 = adj - PIP75; pip100 = adj - PIP100
    if price is None: return None, "No price"
    if price <= pip100: return "100", f"Buy trigger hit at 100 pip (${pip100:.2f})"
    if price <= pip75:  return "75",  f"Buy trigger hit at 75 pip (${pip75:.2f})"
    if price <= pip50:  return "50",  f"Buy trigger hit at 50 pip (${pip50:.2f})"
    return None, "No buy levels triggered."

def line_for_report(t, info, price):
    if not info.get("active", True): return None
    shares = int(info.get("shares", 0) or 0)
    if SKIP_IF_NO_SHARES and shares == 0: return None
    avg = info.get("avg_cost"); cum = float(info.get("cum_div", 0.0) or 0.0)
    if avg is None or price is None: return None
    adj = avg - cum
    level, trig_text = calc_triggers(adj, price)
    floor = income_floor_for_price(price)
    floor_txt = f"Income floor: ${floor}/mo." if isinstance(floor,(int,float)) else f"Income floor: {floor}."
    return f"{t} — Adjusted ${adj:.2f}, current ${price:.2f}. {trig_text} {floor_txt} Shares: {shares}"

async def build_status():
    actives = [t for t,i in positions.items() if i.get("active", True)]
    prices = await fetch_prices_batch(actives)
    lines = []
    for t in sorted(positions.keys()):
        ln = line_for_report(t, positions[t], prices.get(t))
        if ln: lines.append(ln)
    return "📊 ETF Status\n" + "\n".join(lines) if lines else "No active tickers (skipped or missing data)."

async def send_alert(ch, t, price, adj, trig_text):
    await ch.send(
        f"📉 **BUY ALERT** {t}: current ${price:.2f}, adjusted ${adj:.2f}. "
        f"{trig_text} → Consider GTC limit buy."
    )

# ====== Alerts loop only ======
async def alerts_loop():
    await bot.wait_until_ready()
    ch = bot.get_channel(CHANNEL_ID)
    if ch:
        await ch.send("🚀 ETF Anchor Bot online! Alerts enabled. Use `/help` or `/status` anytime.")
    while not bot.is_closed():
        try:
            actives = [t for t,i in positions.items() if i.get("active", True)]
            prices = await fetch_prices_batch(actives)
            today = dt.datetime.now().strftime("%Y-%m-%d")
            for t in actives:
                info = positions.get(t, {})
                shares = int(info.get("shares", 0) or 0)
                if SKIP_IF_NO_SHARES and shares == 0:
                    continue
                avg = info.get("avg_cost"); cum = float(info.get("cum_div", 0.0) or 0.0)
                price = prices.get(t)
                if avg is None or price is None: continue
                adj = avg - cum
                level, trig_text = calc_triggers(adj, price)
                if level:
                    key = (today, t, level)
                    if key not in alert_fired:
                        alert_fired.add(key)
                        if ch:
                            await send_alert(ch, t, price, adj, trig_text)
        except Exception as e:
            if ch: await ch.send(f"Alert loop error: {e}")
        await asyncio.sleep(ALERT_INTERVAL_SEC)

# ====== Commands ======
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    bot.loop.create_task(alerts_loop())

@bot.command()
async def help(ctx):
    await ctx.send(
        "Commands:\n"
        "`/status` — fetch current prices & show triggers\n"
        "`/tickers` — list tracked tickers\n"
        "`/add TICKER` — start tracking (defaults active, shares=0)\n"
        "`/remove TICKER` — stop tracking\n"
        "`/setavg TICKER 12.34` — set average cost\n"
        "`/setdiv TICKER 0.25` — set cumulative dividends per share\n"
        "`/adddiv TICKER 0.25` — increment cum_div by amount\n"
        "`/resetdiv TICKER` — set cumulative dividends to 0.0\n"
        "`/setshares TICKER 10` — set shares\n"
        "`/active TICKER on|off` — toggle active\n"
        "`/setpips 0.50 0.75 1.00` — set pip sizes\n"
        "`/setinterval 300` — set alert interval seconds (min 60)"
    )

@bot.command()
async def status(ctx):
    await ctx.send(await build_status())

@bot.command(name="tickers")
async def tickers_cmd(ctx):
    if not positions: return await ctx.send("No tickers tracked yet.")
    rows = []
    for t,i in positions.items():
        rows.append(f"{t}: shares={i.get('shares',0)}, avg={i.get('avg_cost')}, cumDiv={i.get('cum_div',0)}, active={i.get('active',True)}")
    await ctx.send("Tracked:\n" + "\n".join(rows))

@bot.command()
async def add(ctx, ticker: str):
    t = ticker.upper()
    positions.setdefault(t, {"avg_cost": None, "cum_div": 0.0, "shares": 0, "active": True})
    save_positions(positions)
    await ctx.send(f"Added {t}. Use `/setavg {t} 12.34`, `/setshares {t} 10`, `/setdiv {t} 0.25`.")

@bot.command()
async def remove(ctx, ticker: str):
    t = ticker.upper()
    if t in positions:
        del positions[t]; save_positions(positions); await ctx.send(f"Removed {t}.")
    else:
        await ctx.send(f"{t} not found.")

@bot.command()
async def setavg(ctx, ticker: str, value: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    try:
        positions[t]["avg_cost"] = float(value); save_positions(positions)
        await ctx.send(f"{t} avg cost = {float(value):.4f}.")
    except ValueError:
        await ctx.send("Invalid number.")

@bot.command()
async def setdiv(ctx, ticker: str, value: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    try:
        positions[t]["cum_div"] = float(value); save_positions(positions)
        await ctx.send(f"{t} cumulative div = {float(value):.4f}.")
    except ValueError:
        await ctx.send("Invalid number.")

@bot.command()
async def adddiv(ctx, ticker: str, value: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    try:
        inc = float(value)
        positions[t]["cum_div"] = float(positions[t].get("cum_div", 0.0) or 0.0) + inc
        save_positions(positions)
        await ctx.send(f"{t} cum_div increased by {inc:.4f}. New cum_div = {positions[t]['cum_div']:.4f}.")
    except ValueError:
        await ctx.send("Invalid number. Usage: `/adddiv TICKER 0.25`")

@bot.command()
async def resetdiv(ctx, ticker: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    positions[t]["cum_div"] = 0.0; save_positions(positions)
    await ctx.send(f"{t} cum_div reset to 0.0000.")

@bot.command()
async def setshares(ctx, ticker: str, value: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    try:
        positions[t]["shares"] = int(float(value)); save_positions(positions)
        await ctx.send(f"{t} shares = {int(float(value))}.")
    except ValueError:
        await ctx.send("Invalid number.")

@bot.command()
async def active(ctx, ticker: str, flag: str):
    t = ticker.upper()
    if t not in positions: return await ctx.send(f"{t} not tracked. `/add {t}` first.")
    fl = flag.lower()
    if fl in ("on","true","yes","1"): positions[t]["active"] = True
    elif fl in ("off","false","no","0"): positions[t]["active"] = False
    else: return await ctx.send("Use `/active TICKER on|off`")
    save_positions(positions); await ctx.send(f"{t} active = {positions[t]['active']}.")

@bot.command()
async def setpips(ctx, p50: str, p75: str, p100: str):
    global PIP50, PIP75, PIP100
    try:
        PIP50, PIP75, PIP100 = float(p50), float(p75), float(p100)
        await ctx.send(f"Pips set: 50¢={PIP50:.2f}, 75¢={PIP75:.2f}, 100¢={PIP100:.2f}")
    except ValueError:
        await ctx.send("Usage: `/setpips 0.50 0.75 1.00`")

@bot.command()
async def setinterval(ctx, seconds: str):
    global ALERT_INTERVAL_SEC
    try:
        s = int(seconds)
        if s < 60: return await ctx.send("Minimum interval is 60 seconds.")
        ALERT_INTERVAL_SEC = s
        await ctx.send(f"Alert interval set to {ALERT_INTERVAL_SEC} seconds.")
    except ValueError:
        await ctx.send("Usage: `/setinterval 300` (seconds)")

# ====== Keep-alive web server (Render expects a port) ======
async def handle_root(request): return web.Response(text="OK")
async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

# ====== Entry ======
async def main():
    await start_web()          # start tiny web server
    await bot.start(TOKEN)     # start Discord bot

if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        raise SystemExit("Set DISCORD_TOKEN and CHANNEL_ID env vars.")
    asyncio.run(main())
