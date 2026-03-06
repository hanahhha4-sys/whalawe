"""
Whale Alert Bot — Cloud Version
Berjalan 24/7 di Railway. Kirim notif ke Telegram otomatis.
"""

import os
import time
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from collections import defaultdict

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("whale-alert")

# ─── Config dari Environment Variables ───────────────────────────────────────
ARKHAM_KEY    = os.environ.get("ARKHAM_KEY", "")
TG_TOKEN      = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

MIN_PCT       = float(os.environ.get("MIN_PCT", "0.3"))       # % supply minimum
MIN_USD       = float(os.environ.get("MIN_USD", "50000"))     # USD kumulatif minimum
MIN_TXS       = int(os.environ.get("MIN_TXS", "2"))           # jumlah TX minimum
WINDOW_HOURS  = float(os.environ.get("WINDOW_HOURS", "2"))    # clustering window
SCAN_MINUTES  = int(os.environ.get("SCAN_MINUTES", "5"))      # interval scan
TX_TYPE       = os.environ.get("TX_TYPE", "BOTH")             # BUY / SELL / BOTH

ARKHAM_BASE   = "https://api.arkhamintelligence.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
TG_BASE       = f"https://api.telegram.org/bot{TG_TOKEN}"

# ─── Cache ────────────────────────────────────────────────────────────────────
supply_cache = {}
seen_clusters = set()
pending_detail = {}   # token -> cluster data (untuk /detail command)
tg_offset = 0
stats = {"total": 0, "buys": 0, "sells": 0, "scans": 0, "extreme": 0, "tg_sent": 0}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def fmt_usd(v):
    if v is None: return "—"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"

def fmt_num(v):
    if v is None: return "—"
    if v >= 1e9: return f"{v/1e9:.2f}B"
    if v >= 1e6: return f"{v/1e6:.2f}M"
    if v >= 1e3: return f"{v/1e3:.1f}K"
    return f"{v:.0f}"

def short_addr(a):
    if not a or len(a) <= 10: return a or "?"
    return f"{a[:6]}...{a[-4:]}"

def now_str():
    return datetime.now().strftime("%d %b %Y %H:%M:%S")

def strength_meta(score):
    if score >= 75: return {"label": "EXTREME", "emoji": "🔴"}
    if score >= 50: return {"label": "STRONG",  "emoji": "🟡"}
    if score >= 25: return {"label": "MODERATE","emoji": "🟢"}
    return               {"label": "WEAK",    "emoji": "⚪"}

def is_exchange(label):
    if not label: return False
    keywords = ["exchange","cex","binance","coinbase","okx","bybit","kraken","huobi","kucoin","gate"]
    return any(k in label.lower() for k in keywords)

# ─── Calc Signal Strength ─────────────────────────────────────────────────────
def calc_strength(cluster):
    tx_score  = min(len(cluster["txs"]) / 20, 1) * 30
    usd_score = min(cluster["total_usd"] / 1_000_000, 1) * 30
    pct_score = min((cluster.get("supply_pct") or 0) / 2, 1) * 30
    dur_mins  = (cluster["last_ts"] - cluster["first_ts"]) / 60
    speed     = 10 if dur_mins < 30 else 7 if dur_mins < 60 else 4 if dur_mins < 120 else 1
    return min(int(tx_score + usd_score + pct_score + speed), 100)

# ─── Clustering Engine ────────────────────────────────────────────────────────
def cluster_transfers(transfers, window_secs):
    clusters = {}
    for t in transfers:
        symbol = t.get("tokenSymbol") or t.get("unitSymbol")
        if not symbol: continue

        to_label  = (t.get("toAddress",{}).get("arkhamEntity",{}).get("type","") or
                     t.get("toAddress",{}).get("arkhamLabel",{}).get("name","") or "")
        from_label = (t.get("fromAddress",{}).get("arkhamEntity",{}).get("type","") or
                      t.get("fromAddress",{}).get("arkhamLabel",{}).get("name","") or "")

        to_ex   = is_exchange(to_label)
        from_ex = is_exchange(from_label)

        # Strict direction: Exchange→Wallet = BUY, Wallet→Exchange = SELL
        if to_ex:
            tx_type = "SELL"
        elif from_ex:
            tx_type = "BUY"
        else:
            continue  # wallet→wallet: ambiguous, skip

        wallet = (t.get("toAddress",{}).get("address") if tx_type == "BUY"
                  else t.get("fromAddress",{}).get("address"))
        if not wallet: continue

        ts = (t.get("blockTimestamp") or 0)
        bucket = int(ts // window_secs)
        key = f"{wallet}::{symbol}::{tx_type}::{bucket}"

        if key not in clusters:
            wallet_label = (
                (t.get("toAddress",{}).get("arkhamEntity",{}).get("name") or
                 t.get("toAddress",{}).get("arkhamLabel",{}).get("name"))
                if tx_type == "BUY" else
                (t.get("fromAddress",{}).get("arkhamEntity",{}).get("name") or
                 t.get("fromAddress",{}).get("arkhamLabel",{}).get("name"))
            )
            exchange_name = (
                (t.get("fromAddress",{}).get("arkhamEntity",{}).get("name") or
                 t.get("fromAddress",{}).get("arkhamLabel",{}).get("name") or "Exchange")
                if tx_type == "BUY" else
                (t.get("toAddress",{}).get("arkhamEntity",{}).get("name") or
                 t.get("toAddress",{}).get("arkhamLabel",{}).get("name") or "Exchange")
            )
            clusters[key] = {
                "id": key, "type": tx_type, "token": symbol,
                "wallet": wallet, "wallet_label": wallet_label,
                "exchange": exchange_name,
                "chain": t.get("chain") or t.get("blockchain") or "—",
                "txs": [], "total_usd": 0, "total_amount": 0,
                "first_ts": ts, "last_ts": ts,
            }

        c = clusters[key]
        c["txs"].append(t)
        c["total_usd"]    += t.get("historicalUSD") or 0
        c["total_amount"] += t.get("tokenAmount") or t.get("unitValue") or 0
        c["first_ts"]      = min(c["first_ts"], ts)
        c["last_ts"]       = max(c["last_ts"], ts)

    return list(clusters.values())

# ─── CoinGecko: Total Supply ──────────────────────────────────────────────────
async def get_coin_data(session, symbol):
    key = symbol.lower()
    if key in supply_cache:
        return supply_cache[key]
    try:
        async with session.get(f"{COINGECKO_BASE}/coins/list", timeout=aiohttp.ClientTimeout(total=30)) as r:
            coins = await r.json()
        coin = next((c for c in coins if c["symbol"].lower() == key), None)
        if not coin:
            supply_cache[key] = None
            return None
        async with session.get(
            f"{COINGECKO_BASE}/coins/{coin['id']}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false",
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
        md = data.get("market_data", {})
        result = {
            "supply": md.get("total_supply"),
            "price":  md.get("current_price", {}).get("usd"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "name": data.get("name"),
        }
        supply_cache[key] = result
        return result
    except Exception as e:
        log.warning(f"CoinGecko error for {symbol}: {e}")
        supply_cache[key] = None
        return None

# ─── Telegram ─────────────────────────────────────────────────────────────────
async def tg_send(session, text, reply_markup=None):
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(
            f"{TG_BASE}/sendMessage",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            result = await r.json()
            if not result.get("ok"):
                log.error(f"TG send failed: {result.get('description')}")
            return result
    except Exception as e:
        log.error(f"TG send error: {e}")
        return {"ok": False}

async def tg_get_updates(session):
    global tg_offset
    try:
        async with session.get(
            f"{TG_BASE}/getUpdates?offset={tg_offset}&timeout=1",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("ok"):
                return data.get("result", [])
    except:
        pass
    return []

# ─── AI Analysis ──────────────────────────────────────────────────────────────
async def analyze_with_ai(session, cluster, coin_data):
    if not ANTHROPIC_KEY:
        return "AI tidak tersedia (ANTHROPIC_KEY belum diset)."

    buy = cluster["type"] == "BUY"
    dur = int((cluster["last_ts"] - cluster["first_ts"]) / 60)
    supply_pct = cluster.get("supply_pct")
    price = coin_data.get("price") if coin_data else None
    market_cap = coin_data.get("market_cap") if coin_data else None

    prompt = f"""Kamu adalah crypto analyst profesional. Analisis sinyal whale berikut dan berikan analisis teknikal singkat dalam Bahasa Indonesia.

TOKEN: ${cluster['token']}{f" ({coin_data['name']})" if coin_data and coin_data.get('name') else ""}
AKSI: {"AKUMULASI (Exchange → Wallet)" if buy else "DISTRIBUSI (Wallet → Exchange)"}
TOTAL USD: {fmt_usd(cluster['total_usd'])}
JUMLAH TX: {len(cluster['txs'])} transaksi dalam {dur} menit
% SUPPLY: {f"{supply_pct:.3f}%" if supply_pct is not None else "tidak diketahui"}
SIGNAL STRENGTH: {cluster['strength']}/100 ({strength_meta(cluster['strength'])['label']})
HARGA SAAT INI: {f"${price}" if price else "tidak diketahui"}
MARKET CAP: {fmt_usd(market_cap) if market_cap else "tidak diketahui"}
WALLET LABEL: {cluster.get('wallet_label') or 'tidak diketahui'}
EXCHANGE: {cluster.get('exchange', '—')}

Berikan analisis SINGKAT mencakup:
1. Interpretasi sinyal whale ini
2. Level EMA 20 dan EMA 50 yang perlu dipantau
3. Kondisi RSI ideal untuk entry di timeframe 4H
4. Setup entry: zona entry, target 1, target 2, stop loss
5. Risiko utama
6. Verdict: ENTRY / WAIT / AVOID

Format ringkas, maksimal 250 kata. Gunakan angka spesifik kalau tersedia."""

    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
            return data.get("content", [{}])[0].get("text", "Analisis tidak tersedia.")
    except Exception as e:
        return f"Error AI: {e}"

# ─── Build Telegram Messages ───────────────────────────────────────────────────
def build_short_alert(cluster):
    buy = cluster["type"] == "BUY"
    str_meta = strength_meta(cluster["strength"])
    dur = int((cluster["last_ts"] - cluster["first_ts"]) / 60)
    dur_str = f"{dur}m" if dur < 60 else f"{dur//60}j {dur%60}m"
    supply_pct = cluster.get("supply_pct")
    ts = datetime.fromtimestamp(cluster["last_ts"], tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")

    return f"""{str_meta['emoji']} <b>{str_meta['label']} {"AKUMULASI" if buy else "DISTRIBUSI"} ${cluster['token']}</b>

💰 Total: <b>{fmt_usd(cluster['total_usd'])}</b>
📊 Supply: <b>{f"{supply_pct:.3f}%" if supply_pct is not None else "—"}</b>
🔄 TX: <b>{len(cluster['txs'])}x dalam {dur_str}</b>
⚡ Strength: <b>{cluster['strength']}/100</b>
🏦 {"Dari" if buy else "Ke"}: {cluster.get('exchange') or '—'}
👛 Wallet: <code>{short_addr(cluster['wallet'])}</code>
⛓ Chain: {cluster.get('chain','—')}
🕐 {ts}

{"💡 Cek chart, konfirmasi RSI + EMA sebelum entry" if buy else "⚠️ Waspadai tekanan jual berlanjut"}

Balas /detail_{cluster['token'].lower()} untuk analisis AI lengkap"""

def build_detail_alert(cluster, analysis):
    buy = cluster["type"] == "BUY"
    str_meta = strength_meta(cluster["strength"])
    dur = int((cluster["last_ts"] - cluster["first_ts"]) / 60)
    supply_pct = cluster.get("supply_pct")

    return f"""{str_meta['emoji']} <b>ANALISIS LENGKAP ${cluster['token']}</b>
{"🟢 AKUMULASI" if buy else "🔴 DISTRIBUSI"} — Strength {cluster['strength']}/100

━━━━━━━━━━━━━━━━━━━
📊 <b>DATA CLUSTER</b>
• Total USD: {fmt_usd(cluster['total_usd'])}
• % Supply: {f"{supply_pct:.3f}%" if supply_pct is not None else "—"}
• Jumlah TX: {len(cluster['txs'])}x dalam {dur} menit
• Exchange: {cluster.get('exchange','—')}
• Chain: {cluster.get('chain','—')}
• Wallet: <code>{short_addr(cluster['wallet'])}</code>

━━━━━━━━━━━━━━━━━━━
🤖 <b>ANALISIS AI</b>

{analysis}

━━━━━━━━━━━━━━━━━━━
⚠️ <i>Bukan financial advice. Selalu manage risk.</i>"""

# ─── Handle Telegram Commands ─────────────────────────────────────────────────
async def handle_tg_commands(session):
    global tg_offset
    updates = await tg_get_updates(session)
    for u in updates:
        tg_offset = u["update_id"] + 1
        msg = u.get("message", {})
        text = msg.get("text", "").strip()
        chat_id_msg = str(msg.get("chat", {}).get("id", ""))

        # Hanya proses dari chat ID yang benar
        if chat_id_msg != TG_CHAT_ID:
            continue

        if text == "/status":
            status_text = f"""📡 <b>Whale Alert System — Status</b>

✅ Sistem aktif berjalan
🔄 Scan tiap {SCAN_MINUTES} menit
📊 Min supply: {MIN_PCT}%
💰 Min USD kumulatif: {fmt_usd(MIN_USD)}
🔢 Min TX: {MIN_TXS}x
⏱ Window clustering: {WINDOW_HOURS}j

📈 Statistik:
• Total alert: {stats['total']}
• Akumulasi: {stats['buys']}
• Distribusi: {stats['sells']}
• Extreme: {stats['extreme']}
• TG terkirim: {stats['tg_sent']}
• Total scan: {stats['scans']}

🕐 {now_str()}"""
            await tg_send(session, status_text)
            log.info("Status dikirim ke Telegram")

        elif text.startswith("/detail_"):
            sym = text.replace("/detail_", "").upper()
            if sym in pending_detail:
                cl = pending_detail[sym]
                await tg_send(session, f"⏳ Generating analisis AI untuk <b>${sym}</b>...")
                coin_data = supply_cache.get(sym.lower())
                analysis = await analyze_with_ai(session, cl, coin_data)
                detail_msg = build_detail_alert(cl, analysis)
                await tg_send(session, detail_msg)
                log.info(f"Detail ${sym} terkirim ke Telegram")
            else:
                await tg_send(session, f"❌ Tidak ada data untuk <b>${sym}</b>.\n\nToken yang tersedia: {', '.join(f'${k}' for k in pending_detail.keys()) or 'belum ada'}")

        elif text == "/help":
            help_text = """📋 <b>Daftar Command</b>

/status — Cek status sistem & statistik
/detail_TOKEN — Analisis AI lengkap untuk token
  Contoh: /detail_avnt, /detail_layer
/tokens — Lihat token yang sudah terdeteksi
/help — Tampilkan pesan ini"""
            await tg_send(session, help_text)

        elif text == "/tokens":
            if pending_detail:
                token_list = "\n".join([f"• ${k} — {fmt_usd(v['total_usd'])} ({v['type']})" for k,v in pending_detail.items()])
                await tg_send(session, f"📊 <b>Token Terdeteksi:</b>\n\n{token_list}\n\nBalas /detail_TOKEN untuk analisis AI.")
            else:
                await tg_send(session, "Belum ada token yang terdeteksi sejak sistem start.")

# ─── Main Scan ────────────────────────────────────────────────────────────────
async def run_scan(session):
    stats["scans"] += 1
    log.info(f"Scan #{stats['scans']} dimulai...")

    window_secs = WINDOW_HOURS * 3600
    since = int(time.time() - (window_secs + 600))  # extra 10 menit buffer

    try:
        async with session.get(
            f"{ARKHAM_BASE}/transfers?usdGte=500&limit=100&timeGte={since}",
            headers={"API-Key": ARKHAM_KEY},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            if r.status != 200:
                log.error(f"Arkham API error: HTTP {r.status}")
                return
            data = await r.json()

        transfers = data.get("transfers", [])
        log.info(f"Dapat {len(transfers)} transfer. Clustering...")

        raw_clusters = cluster_transfers(transfers, window_secs)
        log.info(f"{len(raw_clusters)} cluster terbentuk. Filtering...")

        new_count = 0
        for cl in raw_clusters:
            # Filter tipe
            if TX_TYPE == "BUY" and cl["type"] != "BUY": continue
            if TX_TYPE == "SELL" and cl["type"] != "SELL": continue

            # Filter jumlah TX
            if len(cl["txs"]) < MIN_TXS: continue

            # Filter USD kumulatif
            if cl["total_usd"] < MIN_USD: continue

            # Get supply data dari CoinGecko
            coin_data = await get_coin_data(session, cl["token"])
            supply = coin_data.get("supply") if coin_data else None
            supply_pct = (cl["total_amount"] / supply * 100) if supply and cl["total_amount"] else None

            # Filter % supply
            if supply_pct is not None and supply_pct < MIN_PCT:
                log.debug(f"{cl['token']}: {supply_pct:.3f}% < {MIN_PCT}%, skip")
                continue

            cl["supply_pct"] = supply_pct
            cl["total_supply"] = supply
            cl["strength"] = calc_strength(cl)

            # Skip kalau sudah pernah dikirim
            if cl["id"] in seen_clusters:
                continue
            seen_clusters.add(cl["id"])

            # Simpan untuk /detail command
            pending_detail[cl["token"].upper()] = cl

            # Kirim short alert ke Telegram
            short_msg = build_short_alert(cl)
            tg_result = await tg_send(session, short_msg)

            str_meta = strength_meta(cl["strength"])
            if tg_result.get("ok"):
                stats["tg_sent"] += 1
                log.info(f"TG terkirim: {str_meta['emoji']} {cl['type']} ${cl['token']} — {len(cl['txs'])}tx — {fmt_usd(cl['total_usd'])}{f' — {supply_pct:.2f}% supply' if supply_pct else ''}")
            else:
                log.warning(f"TG gagal untuk ${cl['token']}")

            # Update stats
            stats["total"] += 1
            if cl["type"] == "BUY": stats["buys"] += 1
            else: stats["sells"] += 1
            if cl["strength"] >= 75: stats["extreme"] += 1

            new_count += 1

        if new_count == 0:
            log.info("Tidak ada cluster baru memenuhi threshold")
        else:
            log.info(f"Scan selesai. {new_count} alert baru dikirim.")

    except Exception as e:
        log.error(f"Scan error: {e}")

# ─── Startup Check ────────────────────────────────────────────────────────────
async def startup_check(session):
    missing = []
    if not ARKHAM_KEY:   missing.append("ARKHAM_KEY")
    if not TG_TOKEN:     missing.append("TG_TOKEN")
    if not TG_CHAT_ID:   missing.append("TG_CHAT_ID")

    if missing:
        log.error(f"Environment variables belum diset: {', '.join(missing)}")
        log.error("Set di Railway Dashboard → Variables")
        return False

    # Test Telegram
    result = await tg_send(session, f"""✅ <b>Whale Alert System Aktif!</b>

🤖 Bot berhasil terhubung dan berjalan di cloud.

⚙️ <b>Konfigurasi:</b>
• Min supply: {MIN_PCT}%
• Min USD: {fmt_usd(MIN_USD)}
• Min TX: {MIN_TXS}x
• Window: {WINDOW_HOURS} jam
• Interval scan: {SCAN_MINUTES} menit
• Tipe: {TX_TYPE}

📋 Command tersedia:
/status — Cek status sistem
/detail_TOKEN — Analisis AI lengkap
/tokens — Token yang terdeteksi
/help — Bantuan

🕐 Start: {now_str()}""")

    if result.get("ok"):
        log.info("Startup message terkirim ke Telegram!")
        return True
    else:
        log.error(f"Telegram test gagal: {result}")
        return False

# ─── Main Loop ────────────────────────────────────────────────────────────────
async def main():
    log.info("=" * 50)
    log.info("WHALE ALERT SYSTEM — CLOUD VERSION")
    log.info("=" * 50)

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Startup check
        ok = await startup_check(session)
        if not ok:
            log.error("Startup gagal. Cek environment variables di Railway.")
            return

        log.info(f"Sistem aktif. Scan setiap {SCAN_MINUTES} menit.")
        log.info(f"Config: min {MIN_PCT}% supply, min {fmt_usd(MIN_USD)}, min {MIN_TXS}tx, window {WINDOW_HOURS}j")

        tg_poll_counter = 0

        while True:
            try:
                # Scan Arkham
                await run_scan(session)

                # Poll Telegram commands setiap 10 detik selama interval scan
                interval_secs = SCAN_MINUTES * 60
                steps = interval_secs // 10
                for _ in range(int(steps)):
                    await asyncio.sleep(10)
                    await handle_tg_commands(session)

            except Exception as e:
                log.error(f"Main loop error: {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
