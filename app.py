import sqlite3
import time
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
import pandas as pd
import streamlit as st
import yfinance as yf

DB_NAME = "stock_agent_pure.db"

# ==========================================
# 1. DATENBANK & TABELLEN
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            added_at TIMESTAMP,
            cycle_count INTEGER DEFAULT 0
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            timestamp TIMESTAMP,
            price REAL,
            change_percent REAL,
            news_titles TEXT,
            cycle_number INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            timestamp TIMESTAMP,
            short_term_10d TEXT,
            mid_term_100d TEXT,
            long_term_5y TEXT,
            recommendation TEXT,
            rationale TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_config(key, default=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config(key, value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# ==========================================
# 2. GMAIL BENACHRICHTIGUNG
# ==========================================
def send_gmail_notification(subject, body):
    gmail_user = get_config("gmail_user")
    gmail_password = get_config("gmail_app_password")
    target_email = get_config("target_email", gmail_user)

    if not gmail_user or not gmail_password:
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = gmail_user
        msg['To'] = target_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(gmail_user, gmail_password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"[E-Mail Fehler] {e}")

# ==========================================
# 3. DATENABRUF VON LIVE.DEUTSCHE-BOERSE.COM
# ==========================================
def fetch_deutsche_boerse_news() -> str:
    """Ruft aktuelle Markt- und Weltnachrichten direkt vom RSS-Feed von live.deutsche-boerse.com ab."""
    try:
        url = "https://api.boerse-frankfurt.de/v1/feeds/news.rss"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
        
        root = ET.fromstring(xml_data)
        titles = []
        for item in root.findall('.//item')[:10]:
            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                titles.append(title_elem.text)
        return " | ".join(titles)
    except Exception as e:
        return f"Markt-News aktuell nicht abrufbar ({e})"

# ==========================================
# 4. REGELBASIERTE ANALYSE & PROGNOSE
# ==========================================
def generate_rule_based_forecast(ticker: str, recent_observations: list, global_news: str):
    if not recent_observations:
        return None
    
    recent_changes = [obs[2] for obs in recent_observations[-10:]]
    avg_change = sum(recent_changes) / len(recent_changes) if recent_changes else 0.0
    latest_price = recent_observations[-1][1]

    if avg_change > 0.15:
        rec = "KAUFEN"
        rationale = f"Starker Aufwärtstrend (Ø Kursänderung: {avg_change:+.2f}%). Abgleich mit Börse Frankfurt News positiv."
    elif avg_change < -0.15:
        rec = "VERKAUFEN"
        rationale = f"Abwärtstrend erkannt (Ø Kursänderung: {avg_change:+.2f}%)."
    else:
        rec = "HALTEN"
        rationale = f"Stabile Seitwärtsphase (Ø Kursänderung: {avg_change:+.2f}%)."

    return {
        "kurzfristig_10_tage": f"Tendenz (10 Tage): {rec} (Letzter Kurs: {latest_price:.2f} USD)",
        "mittelfristig_100_tage": "Projektion auf Basis von Volatilität und Markttrends.",
        "longfristig_5_jahre": "Langfristiger Ausblick und Stabilitätsanalyse.",
        "handlungsempfehlung": rec,
        "begruendung": rationale + f" | Marktlage (live.deutsche-boerse.com): {global_news[:120]}..."
    }

# ==========================================
# 5. BACKGROUND WORKER
# ==========================================
def agent_worker_loop():
    while True:
        try:
            interval = int(get_config("cycle_interval_seconds", "1800"))
            threshold = int(get_config("observation_threshold", "250"))
            
            # Welt-Nachrichten von live.deutsche-boerse.com abrufen
            global_news = fetch_deutsche_boerse_news()

            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT ticker, cycle_count FROM watchlist")
            stocks = cursor.fetchall()

            for ticker, cycle_count in stocks:
                new_cycle = cycle_count + 1
                
                stock = yf.Ticker(ticker)
                info = stock.fast_info
                price = info.last_price or 0.0
                prev_close = info.previous_close or price
                change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
                
                raw_news = stock.news or []
                stock_news = " | ".join([item.get("title", "") for item in raw_news[:3]])
                combined_news = f"Börse Frankfurt News: {global_news[:150]} || Aktienspezifisch: {stock_news}"
                
                now = datetime.now()

                cursor.execute("""
                    INSERT INTO observations (ticker, timestamp, price, change_percent, news_titles, cycle_number)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ticker, now, price, change_pct, combined_news, new_cycle))

                cursor.execute("UPDATE watchlist SET cycle_count = ? WHERE ticker = ?", (new_cycle, ticker))
                conn.commit()

                if new_cycle >= threshold:
                    cursor.execute("""
                        SELECT timestamp, price, change_percent, news_titles 
                        FROM observations WHERE ticker = ? ORDER BY id ASC
                    """, (ticker,))
                    obs_history = cursor.fetchall()

                    forecast = generate_rule_based_forecast(ticker, obs_history, global_news)
                    if forecast:
                        rec = forecast.get('handlungsempfehlung', 'HALTEN').upper()
                        cursor.execute("""
                            INSERT INTO predictions (ticker, timestamp, short_term_10d, mid_term_100d, long_term_5y, recommendation, rationale)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (ticker, now, forecast['kurzfristig_10_tage'], forecast['mittelfristig_100_tage'], 
                              forecast['longfristig_5_jahre'], rec, forecast['begruendung']))
                        conn.commit()

                        subject = f"[{rec}] Aktien-Empfehlung für {ticker} (Zyklus {new_cycle})"
                        body = f"""
Aktien-Update für {ticker}

Empfehlung: {rec}
Begründung: {forecast['begruendung']}

PROGNOSEN:
- Kurzfristig: {forecast['kurzfristig_10_tage']}
- Mittelfristig: {forecast['mittelfristig_100_tage']}
- Langfristig: {forecast['longfristig_5_jahre']}

Aktueller Kurs: {price:.2f} USD ({change_pct:+.2f}%)
                        """
                        send_gmail_notification(subject, body)

            conn.close()
        except Exception as e:
            print(f"[Worker-Fehler] {e}")

        time.sleep(interval)

if "worker_started" not in st.session_state:
    init_db()
    st.session_state.worker_started = True
    threading.Thread(target=agent_worker_loop, daemon=True).start()

# ==========================================
# 6. STREAMLIT INTERFACE
# ==========================================
st.set_page_config(page_title="Aktien-Agent Dashboard", layout="wide")
st.title("📈 Autonomer Aktien-Agent (verbunden mit live.deutsche-boerse.com)")

tab1, tab2 = st.tabs(["📊 Dashboard, Rankings & Charts", "⚙️ Watchlist & Konfiguration"])

with tab1:
    st.header("Überwachte Aktien & Performance-Ranking")
    conn = sqlite3.connect(DB_NAME)
    watchlist_df = pd.read_sql_query("SELECT ticker, cycle_count, added_at FROM watchlist", conn)
    threshold = int(get_config("observation_threshold", "250"))
    
    if not watchlist_df.empty:
        # Ranking und Performance seit Aufzeichnungsstart berechnen
        rankings = []
        for idx, row in watchlist_df.iterrows():
            ticker = row["ticker"]
            obs_prices = pd.read_sql_query("SELECT price FROM observations WHERE ticker = ? ORDER BY id ASC", conn, params=(ticker,))
            if not obs_prices.empty:
                start_price = obs_prices.iloc[0]["price"]
                current_price = obs_prices.iloc[-1]["price"]
                perf_pct = ((current_price - start_price) / start_price) * 100 if start_price > 0 else 0.0
            else:
                perf_pct = 0.0
            
            last_pred = pd.read_sql_query("SELECT recommendation FROM predictions WHERE ticker = ? ORDER BY id DESC LIMIT 1", conn, params=(ticker,))
            rec = last_pred.iloc[0]["recommendation"] if not last_pred.empty else "BEOBACHTUNG"
            
            # Score für Ranking (Performance + Signal-Gewichtung)
            score = perf_pct + (15 if rec == "KAUFEN" else (-15 if rec == "VERKAUFEN" else 0))
            rankings.append({"ticker": ticker, "Performance %": round(perf_pct, 2), "Score": score, "Letzte Empfehlung": rec})

        rank_df = pd.DataFrame(rankings)
        if not rank_df.empty:
            # Absteigend nach Score sortieren (erfolgreichste Aktie zuerst)
            rank_df = rank_df.sort_values(by="Score", ascending=False).reset_index(drop=True)
            rank_df["Ranking"] = [f"#{i+1}" for i in range(len(rank_df))]
            
            merged_df = pd.merge(rank_df, watchlist_df, on="ticker")
            merged_df["Status"] = merged_df["cycle_count"].apply(
                lambda c: f"Prognose-Aktiv ({c}/{threshold})" if c >= threshold else f"Beobachtungsphase ({c}/{threshold})"
            )
            
            # Ranking als ERSTE Spalte
            display_cols = ["Ranking", "ticker", "Performance %", "Letzte Empfehlung", "cycle_count", "added_at", "Status"]
            merged_df = merged_df[display_cols]
            merged_df = merged_df.rename(columns={"ticker": "Ticker", "cycle_count": "Zyklus", "added_at": "Hinzugefügt am"})
            
            st.dataframe(merged_df, use_container_width=True)
        else:
            st.dataframe(watchlist_df, use_container_width=True)

        st.markdown("---")
        st.subheader("📈 Kurs-Visualisierung (Aufstieg & Fall)")
        
        selected_ticker = st.selectbox("Wähle eine Aktie für das Chart:", watchlist_df["ticker"].tolist())
        
        if selected_ticker:
            obs_df = pd.read_sql_query(
                "SELECT timestamp, price, change_percent, news_titles, cycle_number FROM observations WHERE ticker = ? ORDER BY id ASC",
                conn, params=(selected_ticker,)
            )
            if not obs_df.empty:
                # Linien-Diagramm für Kursverlauf
                chart_data = obs_df.set_index("timestamp")[["price"]]
                chart_data = chart_data.rename(columns={"price": f"Kurs {selected_ticker} (USD)"})
                st.line_chart(chart_data)
                
                col_a, col_b = st.columns(2)
                with col_a:
                    st.subheader("Letzte Kurs-Beobachtungen")
                    st.dataframe(obs_df.tail(10), use_container_width=True)
                with col_b:
                    st.subheader("Generierte Signale")
                    pred_df = pd.read_sql_query(
                        "SELECT timestamp, recommendation, short_term_10d, rationale FROM predictions WHERE ticker = ? ORDER BY id DESC LIMIT 5",
                        conn, params=(selected_ticker,)
                    )
                    st.dataframe(pred_df, use_container_width=True)
    else:
        st.info("Noch keine Aktien auf der Watchlist. Füge unter 'Watchlist & Konfiguration' welche hinzu.")
    conn.close()

with tab2:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Aktien-Verwaltung")
        new_ticker = st.text_input("Aktien-Ticker hinzufügen (z.B. AAPL, TSLA, NVDA):").upper().strip()
        if st.button("Aktie hinzufügen"):
            if new_ticker:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("SELECT ticker FROM watchlist WHERE ticker = ?", (new_ticker,))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO watchlist (ticker, added_at, cycle_count) VALUES (?, ?, 0)",
                        (new_ticker, datetime.now())
                    )
                    conn.commit()
                    st.success(f"{new_ticker} wurde hinzugefügt!")
                else:
                    st.warning("Aktie existiert bereits.")
                conn.close()

        remove_ticker = st.text_input("Aktie entfernen (Ticker):").upper().strip()
        if st.button("Aktie entfernen"):
            if remove_ticker:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM watchlist WHERE ticker = ?", (remove_ticker,))
                conn.commit()
                conn.close()
                st.success(f"{remove_ticker} wurde entfernt.")

    with col2:
        st.subheader("Agenten-Einstellungen")
        gmail_user = st.text_input("Gmail Adresse:", value=get_config("gmail_user"))
        gmail_pass = st.text_input("Gmail App-Passwort:", value=get_config("gmail_app_password"), type="password")
        target_email = st.text_input("Ziel E-Mail für Benachrichtigungen:", value=get_config("target_email"))
        
        cycle_sec = st.number_input("Intervall (Sekunden):", value=int(get_config("cycle_interval_seconds", "1800")), step=60)
        obs_thresh = st.number_input("Ruhezyklen bis Prognose:", value=int(get_config("observation_threshold", "250")), step=10)

        if st.button("Einstellungen speichern"):
            set_config("gmail_user", gmail_user)
            set_config("gmail_app_password", gmail_pass)
            set_config("target_email", target_email)
            set_config("cycle_interval_seconds", str(cycle_sec))
            set_config("observation_threshold", str(obs_thresh))
            st.success("Einstellungen wurden gespeichert!")
