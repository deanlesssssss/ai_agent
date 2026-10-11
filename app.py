import sqlite3
import time
import json
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pandas as pd
import streamlit as st
import yfinance as yf
from google import genai
from google.genai import types

DB_NAME = "stock_agent_v2.db"

# ==========================================
# 1. DATENBANK & BILDUNG DER TABELLEN
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            added_at TIMESTAMP,
            cycle_count INTEGER DEFAULT 0,
            historical_summary TEXT
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
# 2. HELFER-FUNKTIONEN (GEMINI & EMAIL)
# ==========================================
def send_gmail_notification(subject, body):
    gmail_user = get_config("gmail_user")
    gmail_password = get_config("gmail_app_password")
    target_email = get_config("target_email", gmail_user)

    if not gmail_user or not gmail_password:
        print("[E-Mail] Fehlende Gmail-Zugangsdaten. Überspringe E-Mail-Versand.")
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
        print(f"[E-Mail] Erfolgreich gesendet an {target_email}")
    except Exception as e:
        print(f"[E-Mail Fehler] {e}")

def get_gemini_client():
    api_key = get_config("gemini_api_key")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)

def analyze_20y_history_gemini(ticker: str) -> str:
    client = get_gemini_client()
    if not client:
        return "Kein Gemini API Key konfiguriert."

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="20y")
        
        if hist.empty:
            return "Keine historischen Daten verfügbar."

        start_price = float(hist['Close'].iloc[0])
        end_price = float(hist['Close'].iloc[-1])
        max_price = float(hist['High'].max())
        min_price = float(hist['Low'].min())
        total_return = ((end_price - start_price) / start_price) * 100

        prompt = f"""
        Analysiere die folgenden 20-Jahre-Eckdaten der Aktie {ticker}:
        - Startpreis (vor 20 J.): {start_price:.2f} USD
        - Aktueller Preis: {end_price:.2f} USD
        - 20-Jahres-Allzeithoch: {max_price:.2f} USD
        - 20-Jahres-Tief: {min_price:.2f} USD
        - Gesamtrendite 20 Jahre: {total_return:.2f}%

        Fasse die langfristige Struktur der Kursbewegungen (Wachstum, Krisen, Stagnationsphasen)
        in 3-4 prägnanten Sätzen zusammen.
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"Fehler bei der Historienanalyse: {e}"

def generate_forecast_gemini(ticker: str, hist_summary: str, recent_observations: list):
    client = get_gemini_client()
    if not client:
        return None

    obs_text = "\n".join([
        f"- {obs[0]}: Preis {obs[1]:.2f} USD ({obs[2]:+.2f}%), News: {obs[3]}"
        for obs in recent_observations[-20:]
    ])

    prompt = f"""
    Du bist ein quantitativer Börsen-Agent. Du hast die Aktie {ticker} über mindestens 250 Zyklen beobachtet.

    HISTORISCHES FUNDAMENT (20 JAHRE):
    {hist_summary}

    JÜNGSTE BEOBACHTUNGEN (KURS & NACHRICHTEN):
    {obs_text}

    Erstelle basierend auf dem Kontext eine Vorhersage und eine klare Handlungsempfehlung (KAUFEN, VERKAUFEN oder HALTEN).
    Antworte ausschließlich im folgenden JSON-Format:
    {{
        "kurzfristig_10_tage": "Deine Einschätzung für die nächsten 10 Tage",
        "mittelfristig_100_tage": "Deine Einschätzung für die nächsten 100 Tage",
        "langfristig_5_jahre": "Deine Einschätzung für die nächsten 5 Jahre",
        "handlungsempfehlung": "KAUFEN / VERKAUFEN / HALTEN",
        "begruendung": "Synthese aus Daten, Kursverlauf und Nachrichten"
    }}
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Prognosefehler für {ticker}: {e}")
        return None

# ==========================================
# 3. BACKGROUND WORKER (PERIODISCHER PROZESS)
# ==========================================
def agent_worker_loop():
    while True:
        try:
            interval = int(get_config("cycle_interval_seconds", "1800"))
            threshold = int(get_config("observation_threshold", "250"))
            
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT ticker, cycle_count, historical_summary FROM watchlist")
            stocks = cursor.fetchall()

            for ticker, cycle_count, hist_summary in stocks:
                new_cycle = cycle_count + 1
                
                # Kurs und News abrufen
                stock = yf.Ticker(ticker)
                info = stock.fast_info
                price = info.last_price or 0.0
                prev_close = info.previous_close or price
                change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
                
                raw_news = stock.news or []
                news_titles = " | ".join([item.get("title", "") for item in raw_news[:5]])
                now = datetime.now()

                # Speichern
                cursor.execute("""
                    INSERT INTO observations (ticker, timestamp, price, change_percent, news_titles, cycle_number)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ticker, now, price, change_pct, news_titles, new_cycle))

                cursor.execute("UPDATE watchlist SET cycle_count = ? WHERE ticker = ?", (new_cycle, ticker))
                conn.commit()

                # Prfün ob Schwellenwert für Prognose erreicht
                if new_cycle >= threshold:
                    cursor.execute("""
                        SELECT timestamp, price, change_percent, news_titles 
                        FROM observations WHERE ticker = ? ORDER BY id ASC
                    """, (ticker,))
                    obs_history = cursor.fetchall()

                    forecast = generate_forecast_gemini(ticker, hist_summary, obs_history)
                    if forecast:
                        rec = forecast.get('handlungsempfehlung', 'HALTEN').upper()
                        cursor.execute("""
                            INSERT INTO predictions (ticker, timestamp, short_term_10d, mid_term_100d, long_term_5y, recommendation, rationale)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (ticker, now, forecast['kurzfristig_10_tage'], forecast['mittelfristig_100_tage'], 
                              forecast['langfristig_5_jahre'], rec, forecast['begruendung']))
                        conn.commit()

                        # E-Mail Benachrichtigung senden
                        subject = f"[{rec}] Neue Prognose & Empfehlung für {ticker}"
                        body = f"""
Aktien-Update für {ticker} (Zyklus {new_cycle})

Handlungsempfehlung: {rec}
Begründung: {forecast['begruendung']}

PROGNOSEN:
- Kurzfristig (10 Tage): {forecast['kurzfristig_10_tage']}
- Mittelfristig (100 Tage): {forecast['mittelfristig_100_tage']}
- Langfristig (5 Jahre): {forecast['langfristig_5_jahre']}

Aktueller Kurs: {price:.2f} USD ({change_pct:+.2f}%)
                        """
                        send_gmail_notification(subject, body)

            conn.close()
        except Exception as e:
            print(f"[Worker-Fehler] {e}")

        time.sleep(interval)

# Startet den Worker in einem separaten Thread einmalig
if "worker_started" not in st.session_state:
    init_db()
    st.session_state.worker_started = True
    threading.Thread(target=agent_worker_loop, daemon=True).start()

# ==========================================
# 4. STREAMLIT INTERFACE
# ==========================================
st.set_page_config(page_title="Aktien-Agent mit Gemini", layout="wide")
st.title("📈 KI Aktien-Agent & Gemini Dashboard")

tab1, tab2, tab3 = st.tabs(["📊 Dashboard & Prognosen", "⚙️ Watchlist & Konfiguration", "💬 Chat mit Gemini"])

# --- TAB 1: DASHBOARD ---
with tab1:
    st.header("Überwachte Aktien")
    conn = sqlite3.connect(DB_NAME)
    watchlist_df = pd.read_sql_query("SELECT ticker, cycle_count, added_at FROM watchlist", conn)
    threshold = int(get_config("observation_threshold", "250"))
    
    if not watchlist_df.empty:
        watchlist_df["Status"] = watchlist_df["cycle_count"].apply(
            lambda c: f"Prognose-Aktiv ({c}/{threshold})" if c >= threshold else f"Beobachtungsphase ({c}/{threshold})"
        )
        st.dataframe(watchlist_df, use_container_width=True)

        selected_ticker = st.selectbox("Wähle eine Aktie für Details:", watchlist_df["ticker"].tolist())
        
        if selected_ticker:
            st.subheader(f"Letzte Beobachtungen für {selected_ticker}")
            obs_df = pd.read_sql_query(
                "SELECT timestamp, price, change_percent, news_titles, cycle_number FROM observations WHERE ticker = ? ORDER BY id DESC LIMIT 10",
                conn, params=(selected_ticker,)
            )
            st.dataframe(obs_df, use_container_width=True)

            st.subheader(f"Letzte Prognosen für {selected_ticker}")
            pred_df = pd.read_sql_query(
                "SELECT timestamp, recommendation, short_term_10d, mid_term_100d, long_term_5y, rationale FROM predictions WHERE ticker = ? ORDER BY id DESC LIMIT 5",
                conn, params=(selected_ticker,)
            )
            st.dataframe(pred_df, use_container_width=True)
    else:
        st.info("Noch keine Aktien auf der Watchlist. Füge unter 'Watchlist & Konfiguration' welche hinzu.")
    conn.close()

# --- TAB 2: WATCHLIST & KONFIGURATION ---
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
                    st.info(f"Analysiere 20-Jahre-Historie für {new_ticker} mit Gemini...")
                    hist_summary = analyze_20y_history_gemini(new_ticker)
                    cursor.execute(
                        "INSERT INTO watchlist (ticker, added_at, cycle_count, historical_summary) VALUES (?, ?, 0, ?)",
                        (new_ticker, datetime.now(), hist_summary)
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
        gemini_key = st.text_input("Gemini API Key:", value=get_config("gemini_api_key"), type="password")
        gmail_user = st.text_input("Gmail Adresse:", value=get_config("gmail_user"))
        gmail_pass = st.text_input("Gmail App-Passwort:", value=get_config("gmail_app_password"), type="password")
        target_email = st.text_input("Ziel E-Mail für Benachrichtigungen:", value=get_config("target_email"))
        
        cycle_sec = st.number_input("Intervall (Sekunden):", value=int(get_config("cycle_interval_seconds", "1800")), step=60)
        obs_thresh = st.number_input("Ruhezyklen bis Prognose:", value=int(get_config("observation_threshold", "250")), step=10)

        if st.button("Einstellungen speichern"):
            set_config("gemini_api_key", gemini_key)
            set_config("gmail_user", gmail_user)
            set_config("gmail_app_password", gmail_pass)
            set_config("target_email", target_email)
            set_config("cycle_interval_seconds", str(cycle_sec))
            set_config("observation_threshold", str(obs_thresh))
            st.success("Einstellungen wurden gespeichert!")

# --- TAB 3: CHAT MIT GEMINI ---
with tab3:
    st.header("Diskutiere mit deinem Aktien-Agenten")
    st.caption("Frag Gemini direkt über beobachtete Kurse, Signale oder frage nach einer aktuellen Markteinschätzung.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if user_prompt := st.chat_input("Z.B.: Wie schätzt du die Entwicklung von AAPL aktuell ein?"):
        st.session_state.messages.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        client = get_gemini_client()
        if not client:
            st.error("Bitte hinterlege zuerst deinen Gemini API Key in den Einstellungen.")
        else:
            # Datenbank-Kontext für Gemini zusammenstellen
            conn = sqlite3.connect(DB_NAME)
            watchlist_data = pd.read_sql_query("SELECT * FROM watchlist", conn).to_string()
            recent_predictions = pd.read_sql_query("SELECT * FROM predictions ORDER BY id DESC LIMIT 5", conn).to_string()
            conn.close()

            system_context = f"""
            Du bist der persönliche KI-Aktien-Agent des Nutzers.
            Hier ist der aktuelle Status deiner Datenbank:

            WATCHLIST:
            {watchlist_data}

            LETZTE PROGNOSEN:
            {recent_predictions}

            Beantworte die Frage des Nutzers präzise, fundiert und beziehe dich bei Bedarf auf die gespeicherten Daten.
            """

            with st.chat_message("assistant"):
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=f"{system_context}\n\nFrage des Nutzers: {user_prompt}"
                )
                st.markdown(response.text)
                st.session_state.messages.append({"role": "assistant", "content": response.text})
